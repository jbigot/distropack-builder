"""A runner that records the containers the builder asks for, and runs none."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from distropack_builder.docker import Mount, RunResult


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


@dataclass
class FakeRunner:
    """Stands for distropack_builder.docker.run: records each call, answers with respond.

    respond returns the call's result, or None for a success without output; it may raise
    StopBuild.
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
        as_root: bool = False,
        **options: Any,
    ) -> RunResult:
        call = Call(image, tuple(args), tuple(mounts), dict(env or {}), as_root)
        self.calls.append(call)
        return self.respond(call) or RunResult(0, '', '')
