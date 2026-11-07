"""A runner that records the containers the builder asks for, and runs none."""

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
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

    def build_of(self, package: str) -> bool:
        """Whether this is the builder container of package's build."""
        return '_builder:' in self.image and self.targets['/src'].source.parent.name.startswith(
            f'{package}.'
        )


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


# What gpg --list-secret-keys --with-colons says of the fixture key, as far as GpgInfo reads it.
FIXTURE_KEY_LISTING = (
    'sec:u:255:22:ABCDEF0123456789:1:::u:::scESC:::+:::23::0:\n'
    'fpr:::::::::0123456789ABCDEF0123456789ABCDEF01234567:\n'
    'uid:u::::1::0::Test <test@example.invalid>::::::::::0:\n'
)


def _spec_query(call: Call) -> str:
    """What rpmspec says of the spec the call mounts, for the queries the builder makes."""
    spec = (call.targets['/data'].source / call.args[-1]).read_text()
    if '--buildrequires' in call.args:
        return '\n'.join(re.findall(r'^BuildRequires:\s*(\S+)', spec, re.M)) + '\n'
    if '--provides' in call.args:
        (name,) = re.findall(r'^Name:\s*(\S+)', spec, re.M)
        return f'{name} = 1.0-1\n'
    return spec  # -P: the spec, its macros expanded


def tools_respond(call: Call) -> RunResult | None:
    """The answers of the tool containers to a run of the fixture project; builders succeed."""
    if '--list-secret-keys' in call.args:
        return RunResult(0, FIXTURE_KEY_LISTING, FIXTURE_KEY_LISTING)
    if call.args[:1] == ('rpmspec',):
        out = _spec_query(call)
        return RunResult(0, out, out)
    return None
