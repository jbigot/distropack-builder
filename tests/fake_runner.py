# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""A runner that records the containers the builder asks for, and runs none."""

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from distropack_builder.docker import Cancellation, Mount, RunResult
from support import write_tarball


class StopBuild(Exception):
    """Raised by a response to end the build where the test has seen enough."""


@dataclass(frozen=True)
class Call:
    """One container the builder asked for."""

    image: str
    args: tuple[str, ...]
    mounts: tuple[Mount, ...]
    env: Mapping[str, str]
    as_root: bool
    workdir: str | None = None
    network: bool = True
    commit_to: str | None = None
    remove_image: bool = False
    commit_unless: Path | None = None

    @property
    def targets(self) -> dict[str, Mount]:
        """The mounts, by path in the container."""
        return {m.target: m for m in self.mounts}

    @property
    def is_build(self) -> bool:
        """Whether this is a builder container building: its build mode, after its deps one."""
        return self.args[:1] == ('build',)

    @property
    def is_deps(self) -> bool:
        """Whether this is a builder container installing a build's dependencies."""
        return '_builder:' in self.image and self.args[:1] == ('deps',)

    @property
    def is_query(self) -> bool:
        """Whether this is a builder container answering the rpm spec queries."""
        return self.args[:1] == ('query',)

    @property
    def is_tool(self) -> bool:
        """Whether this is a tool container: no builder image, nor a commit of one."""
        return '_builder' not in self.image

    def build_of(self, package: str) -> bool:
        """Whether this is the builder container of package's build."""
        return self.is_build and self.targets['/src'].source.parent.name.startswith(f'{package}.')

    def deps_of(self, package: str) -> bool:
        """Whether this is the container installing the build dependencies of package."""
        return self.is_deps and self.targets['/src'].source.parent.name.startswith(f'{package}.')


@dataclass
class FakeRunner:
    """Stands for distropack_builder.docker.run: records each call, answers with respond.

    respond returns the call's result, or None for a success without output; it may raise.
    """

    respond: Callable[[Call], RunResult | None] = lambda call: None
    calls: list[Call] = field(default_factory=list)

    def __call__(
        self,
        engine: Any,
        image: str,
        args: Sequence[str] = (),
        *,
        mounts: Sequence[Mount] = (),
        env: Mapping[str, str] | None = None,
        workdir: str | None = None,
        shm_size: str | None = None,
        as_root: bool = False,
        check: bool = True,
        cancellation: Cancellation | None = None,
        logger: logging.Logger | None = None,
        network: bool = True,
        commit_to: str | None = None,
        remove_image: bool = False,
        commit_unless: Path | None = None,
    ) -> RunResult:
        call = Call(
            image,
            tuple(args),
            tuple(mounts),
            dict(env or {}),
            as_root,
            workdir,
            network,
            commit_to,
            remove_image,
            commit_unless,
        )
        self.calls.append(call)
        return self.respond(call) or RunResult(0, '', '')


def answer_queries(call: Call) -> None:
    """Write what rpmspec says of each spec the query container mounts, as its entrypoint does."""
    out = call.targets['/out'].source
    for target, mount in call.targets.items():
        if not target.startswith('/specs/'):
            continue
        spec = mount.source.read_text()
        answers = out / Path(target).stem
        answers.mkdir()
        requires = re.findall(r'^BuildRequires:\s*(\S+)', spec, re.M)
        (answers / 'buildrequires').write_text(''.join(f'{r}\n' for r in requires))
        (name,) = re.findall(r'^Name:\s*(\S+)', spec, re.M)
        (answers / 'provides').write_text(f'{name} = 1.0-1\n')
        # the spec, its macros expanded: the fixture's specs use %{version} alone
        (version,) = re.findall(r'^Version:\s*(\S+)', spec, re.M)
        (answers / 'parsed').write_text(spec.replace('%{version}', version))
        (answers / 'errors').write_text('')
        (answers / 'status').write_text('0\n')


def host_path(call: Call, path: str) -> Path:
    """The host path of a path in the container, under one of its mounts."""
    for target, mount in sorted(call.targets.items(), key=lambda item: -len(item[0])):
        if path == target or path.startswith(target + '/'):
            return mount.source / path[len(target) :].lstrip('/')
    raise KeyError(path)


def answer_uscan(call: Call) -> None:
    """Download the upstream tarball as uscan does: the file, and the orig symlink to it.

    The file is named after the package running uscan, as the GitHub watch files of the docs name
    it (@PACKAGE@), and holds what the watch file's upstream, upstream/<name>.git, has at the
    changelog's version: the fixture's tarball of <name>-<version> if there is one, a placeholder
    `<name> <version>` otherwise.
    """
    package, version = changelog_version(call)
    assert call.workdir is not None
    watch = (host_path(call, call.workdir) / 'debian' / 'watch').read_text()
    (upstream_name,) = re.findall(r'upstream/([^/\s]+)\.git', watch)
    destdir = host_path(call, call.args[call.args.index('--destdir') + 1])
    destdir.mkdir(parents=True, exist_ok=True)
    upstream = destdir / f'{package}-{version}.tar.gz'
    if upstream.exists():  # kept from an earlier run: uscan does not download it again
        return
    tree = Path(__file__).parent / 'e2e' / 'project' / 'upstream' / f'{upstream_name}-{version}'
    if tree.is_dir():
        write_tarball(tree, upstream)
    else:
        upstream.write_bytes(f'{upstream_name} {version}\n'.encode())
    (destdir / f'{package}_{version}.orig.tar.gz').symlink_to(upstream.name)


def changelog_version(call: Call) -> tuple[str, str]:
    """The package and upstream version of the changelog of the call's working directory."""
    assert call.workdir is not None
    changelog = host_path(call, call.workdir) / 'debian' / 'changelog'
    package, version = re.findall(r'^(\S+) \(([^-)]+)', changelog.read_text(), re.M)[0]
    return package, version


def answer_mk_origtargz(call: Call) -> None:
    """Name the downloaded tarball for another package, as mk-origtargz does: a symlink."""
    args = call.args
    package, version = changelog_version(call)
    directory = host_path(call, args[args.index('--directory') + 1])
    upstream = Path(args[-1]).name
    orig = directory / f'{package}_{version}.orig.tar.gz'
    if not orig.is_symlink():
        orig.symlink_to(upstream)


def tools_respond(call: Call) -> RunResult | None:
    """The answers of the tool containers to a run of the fixture project; builders succeed.

    aptly's publish writes the Release file of its distribution, with a trailing space on its
    first line, as the signer then has to drop. uscan and mk-origtargz write the upstream tarballs.
    """
    if call.args[:1] == ('uscan',):
        answer_uscan(call)
    if call.args[:1] == ('mk-origtargz',):
        answer_mk_origtargz(call)
    if 'publish' in call.args:
        (distribution,) = (a.split('=', 1)[1] for a in call.args if a.startswith('-distribution='))
        release = call.targets['/aptlyout'].source / 'dists' / distribution / 'Release'
        release.parent.mkdir(parents=True, exist_ok=True)
        release.write_text(f'Origin: hello \nCodename: {distribution}\n')
    if '_builder:' in call.image and call.args[:1] == ('query',):
        answer_queries(call)
    return None
