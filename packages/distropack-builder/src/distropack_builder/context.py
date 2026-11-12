# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""What every container of a run shares: the engine, the runner, the cancellation, the work tree."""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from distropack_builder.docker import Cancellation, Engine, Mount, RunResult


class Runner(Protocol):
    """Runs one container: distropack_builder.docker.run, or a fake of it in the tests."""

    def __call__(
        self,
        engine: Engine,
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
        network: bool | str = True,
        commit_to: str | None = None,
        remove_image: bool = False,
        commit_unless: Path | None = None,
        hostname: str | None = None,
        networks: Sequence[str] = (),
    ) -> RunResult:
        """Run image with args in a new container, and remove it (see docker.run)."""
        ...


@dataclass(frozen=True)
class BuildContext:
    """What every container call of a run receives.

    Attributes:
        engine: the engine the containers run on.
        run: how a container is run.
        cancellation: what cancels the run: its containers, and its downloads.
        workdir: the directory of the run's temporary files, removed when the run ends.
        jobs: how many builds run at once, and the parallelism of each.
        check_reproducible: whether each build runs twice, varied, its packages compared.
    """

    engine: Engine
    run: Runner
    cancellation: Cancellation
    workdir: Path
    jobs: int
    check_reproducible: bool = False
