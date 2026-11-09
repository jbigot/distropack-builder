# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The upstream tarballs of the 3.0 (quilt) deb sources, downloaded with uscan.

uscan reads each package's debian/watch in the Debian tools image, as Debian's own tooling does:
its version mangling, its upstream signature checks and its Files-Excluded repacking. Packages
whose watch file and upstream version are the same share one download, which mk-origtargz then
names (and repacks, after its own Files-Excluded) for each of the others.
"""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import stamina
from debian.changelog import Changelog

from distropack_builder.bake import DEB_TOOLS, REGISTRY, tool_image
from distropack_builder.context import BuildContext
from distropack_builder.docker import ContainerFailed, Mount
from distropack_builder.downloads import ATTEMPTS, DownloadFailed

PROJECT = '/project'
DOWNLOADS = '/downloads'
# The watch options that make uscan check an upstream signature, and fail on a bad one.
_CHECKED = re.compile(r'pgpsigurlmangle|pgpmode\s*=\s*(next|previous|self|gittag)')
_SIGNATURES = ('.asc', '.sig', '.sign', '.gpg')
# What a new attempt may not meet: a server error, a timeout, a connection refused or reset.
_TRANSIENT = re.compile(r'failed: 5\d\d|timed? ?out|Connection (refused|reset)', re.IGNORECASE)


@dataclass(frozen=True)
class Origin:
    """What decides an upstream tarball: the watch file, and the upstream version to download."""

    watch: bytes
    upstream_version: str


@dataclass(frozen=True)
class OrigFiles:
    """The upstream tarball of a package.

    Attributes:
        upstream: the file as downloaded, before any renaming or repacking.
        orig: `<package>_<upstream version>.orig.tar.<ext>`, which the source package holds.
        verified: whether uscan checked its upstream signature (debian/watch asks for it, and
            debian/upstream/signing-key.asc exists).
    """

    upstream: Path
    orig: Path
    verified: bool


def origin(package_dir: Path) -> Origin:
    """The origin of the upstream tarball of the deb source in package_dir."""
    debian = package_dir / 'debian'
    with (debian / 'changelog').open() as changelog:
        upstream_version = Changelog(changelog, max_blocks=1).upstream_version
    assert upstream_version is not None  # a quilt source's version has a revision
    return Origin((debian / 'watch').read_bytes(), upstream_version)


def verified(package_dir: Path) -> bool:
    """Whether uscan checks the upstream signature of the package's tarball."""
    debian = package_dir / 'debian'
    # a line commented out checks nothing
    lines = (debian / 'watch').read_text().splitlines()
    watch = '\n'.join(line for line in lines if not line.lstrip().startswith('#'))
    return bool(_CHECKED.search(watch)) and (debian / 'upstream' / 'signing-key.asc').is_file()


def fetch_origs(
    context: BuildContext, project: Path, names: list[str], directory: Path
) -> dict[str, OrigFiles | DownloadFailed]:
    """The upstream tarball of each package named, downloaded once per distinct origin.

    Args:
        context: where the tool containers run.
        project: the directory of build.conf, mounted read-only.
        names: the packages, each a directory of project with a debian/watch.
        directory: where the tarballs are written, one subdirectory per origin.

    Returns:
        Each package's tarball, or why it could not be had: uscan's failure, named by the watch
        file, fails every package of that origin.
    """
    groups: dict[Origin, list[str]] = {}
    for name in names:
        groups.setdefault(origin(project / name), []).append(name)
    run = _Tools(context, project, directory)
    results: dict[str, OrigFiles | DownloadFailed] = {}
    for index, (found, members) in enumerate(groups.items()):
        first, *others = members
        destdir = directory / str(index)
        try:
            _uscan(run, first, destdir, index)
            upstream = _upstream(destdir, first, found.upstream_version)
            for name in others:
                # in the package's directory, mk-origtargz reads its name, version and
                # Files-Excluded from its debian/, as uscan does
                run(name, 'mk-origtargz', '--directory', _in(destdir, index), _in(upstream, index))
        except (ContainerFailed, DownloadFailed) as error:
            failure = _failure(first, error)
            results |= dict.fromkeys(members, failure)
            continue
        for name in members:
            results[name] = OrigFiles(
                upstream,
                _orig(destdir, name, found.upstream_version),
                verified(project / name),
            )
    return results


def _transient(error: Exception) -> bool:
    """Whether uscan failed on what a new attempt may not meet: a server error, a timeout."""
    return isinstance(error, ContainerFailed) and bool(_TRANSIENT.search(_reason(error)))


@stamina.retry(on=_transient, attempts=ATTEMPTS)
def _uscan(run: '_Tools', name: str, destdir: Path, index: int) -> None:
    """Run uscan in package name's directory, writing to destdir, emptied first."""
    shutil.rmtree(destdir, ignore_errors=True)
    destdir.mkdir(parents=True)
    run(name, 'uscan', '--download-current-version', '--destdir', _in(destdir, index))


def _in(path: Path, index: int) -> str:
    """The path in the container of a file of the origin index's directory."""
    name = '' if path.name == str(index) else f'/{path.name}'
    return f'{DOWNLOADS}/{index}{name}'


class _Tools:
    """The Debian tools image, run in a package's directory of the project."""

    def __init__(self, context: BuildContext, project: Path, directory: Path) -> None:
        self._context = context
        self._mounts = [Mount(PROJECT, project, read_only=True), Mount(DOWNLOADS, directory)]

    def __call__(self, name: str, *args: str) -> None:
        self._context.run(
            self._context.engine,
            REGISTRY + tool_image(DEB_TOOLS),
            args,
            mounts=self._mounts,
            env={'HOME': '/tmp'},
            workdir=f'{PROJECT}/{name}',
            cancellation=self._context.cancellation,
        )


def _orig(destdir: Path, name: str, version: str) -> Path:
    """The orig tarball of package name in destdir."""
    found = sorted(destdir.glob(f'{name}_{version}.orig.tar.*'))
    found = [path for path in found if not path.name.endswith(_SIGNATURES)]
    if not found:
        raise DownloadFailed(str(destdir), f'no {name}_{version}.orig.tar.* written')
    return found[0]


def _upstream(destdir: Path, name: str, version: str) -> Path:
    """The tarball as uscan downloaded it: what the orig links to, or the other tarball."""
    orig = _orig(destdir, name, version)
    if orig.is_symlink():
        return orig.resolve()
    others = [
        path
        for path in sorted(destdir.iterdir())
        if path.is_file() and path != orig and not path.name.endswith(_SIGNATURES)
    ]
    return others[0] if others else orig


def _reason(error: Exception) -> str:
    """The last error or warning of uscan, with the indented lines continuing it, on one line."""
    output = str(getattr(error, 'output', None) or error)
    messages: list[list[str]] = []
    for line in output.splitlines():
        if line.startswith(('uscan error', 'uscan warn')):
            messages.append([line.strip()])
        elif messages and line[:1] in (' ', '\t') and line.strip():
            messages[-1].append(line.strip())
    if messages:
        return ' '.join(messages[-1])
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else 'uscan failed'


def _failure(name: str, error: Exception) -> DownloadFailed:
    """The failure of uscan, named by the watch file, with its reason."""
    return DownloadFailed(f'with {name}/debian/watch', _reason(error))
