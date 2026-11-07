"""A runner that records the containers the builder asks for, and runs none."""

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from distropack_builder.docker import Cancellation, Mount, RunResult


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

    @property
    def targets(self) -> dict[str, Mount]:
        """The mounts, by path in the container."""
        return {m.target: m for m in self.mounts}

    @property
    def is_build(self) -> bool:
        """Whether this is a builder container building, not answering spec queries."""
        return '_builder:' in self.image and self.args[:1] != ('query',)

    def build_of(self, package: str) -> bool:
        """Whether this is the builder container of package's build."""
        return self.is_build and self.targets['/src'].source.parent.name.startswith(f'{package}.')


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
    ) -> RunResult:
        call = Call(image, tuple(args), tuple(mounts), dict(env or {}), as_root)
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
        (answers / 'parsed').write_text(spec)  # the spec, its macros expanded
        (answers / 'errors').write_text('')
        (answers / 'status').write_text('0\n')


def tools_respond(call: Call) -> RunResult | None:
    """The answers of the tool containers to a run of the fixture project; builders succeed.

    aptly's publish writes the Release file of its distribution, with a trailing space on its
    first line, as the signer then has to drop.
    """
    if 'publish' in call.args:
        (distribution,) = (a.split('=', 1)[1] for a in call.args if a.startswith('-distribution='))
        release = call.targets['/aptlyout'].source / 'dists' / distribution / 'Release'
        release.parent.mkdir(parents=True, exist_ok=True)
        release.write_text(f'Origin: hello \nCodename: {distribution}\n')
    if '_builder:' in call.image and call.args[:1] == ('query',):
        answer_queries(call)
    return None
