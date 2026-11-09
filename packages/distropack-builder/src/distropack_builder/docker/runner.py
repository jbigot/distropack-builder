# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Running one container: its output streamed to the log, its end awaited, the container removed.

Every container is created with the engine quirks handled once: SELinux labelling disabled, so
that bind mounts work on enforcing hosts; no limit on its processes, so that Podman's default does
not throttle parallel builds; a fully qualified image reference, so that Podman never asks which
registry a short name means.
"""

import logging
import subprocess
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import docker
from docker.models.containers import Container
from docker.utils import parse_repository_tag

from distropack_builder.docker.engine import Engine

LOGGER = logging.getLogger('distropack_builder.docker')
# The label of every container the runner creates, to find them all.
LABEL = 'distropack-builder=1'


@dataclass(frozen=True)
class Mount:
    """A host directory bind-mounted into the container.

    Attributes:
        target: the path in the container.
        source: the host directory.
        read_only: whether the container may not write it.
    """

    target: str
    source: Path
    read_only: bool = False


@dataclass(frozen=True)
class RunResult:
    """How a container ended.

    Attributes:
        status: its exit status.
        output: its standard output and error, interleaved as the engine received them.
        stdout: its standard output alone.
    """

    status: int
    output: str
    stdout: str


class ContainerFailed(subprocess.CalledProcessError):
    """A container that exited with a non-zero status: `cmd` is the image and its arguments."""


class Cancelled(Exception):
    """The run was cancelled."""


class Cancellation:
    """Cancels runs: the ones in progress have their container killed, later ones do not start."""

    def __init__(self) -> None:
        """A cancellation not requested yet."""
        self._lock = threading.Lock()
        self._cancelled = False
        self._containers: set[Container] = set()

    @property
    def cancelled(self) -> bool:
        """Whether cancel was called."""
        return self._cancelled

    def cancel(self) -> None:
        """Kill the containers of the runs in progress, and refuse to start any other."""
        with self._lock:
            self._cancelled = True
            containers = list(self._containers)
        for container in containers:
            _kill(container)

    @contextmanager
    def tracking(self, container: Container) -> Iterator[None]:
        """Track container while the block runs.

        Raises:
            Cancelled: the cancellation was already requested.
        """
        with self._lock:
            if self._cancelled:
                raise Cancelled()
            self._containers.add(container)
        try:
            yield
        finally:
            with self._lock:
                self._containers.discard(container)


def _kill(container: Container) -> None:
    """Kill container, whatever its state: one not running (yet, or any more) is left as it is."""
    with suppress(docker.errors.APIError):
        container.kill()


def qualified(image: str) -> str:
    """The image reference with an explicit tag, `latest` when it has none (nor a digest)."""
    repository, tag = parse_repository_tag(image)
    return image if tag else f'{repository}:latest'


def _create(engine: Engine, image: str, cancellation: Cancellation, **options: Any) -> Container:
    """The container, created after pulling its image if the engine does not have it.

    Raises:
        Cancelled: the cancellation came while the image was pulled.
    """
    try:
        return engine.client.containers.create(image, **options)
    except docker.errors.ImageNotFound:
        repository, tag = parse_repository_tag(image)
        # Streamed, so that a cancellation is seen at the next progress event; the engine finishes
        # the pull on its own.
        for _ in engine.client.api.pull(repository, tag=tag, stream=True, decode=True):
            if cancellation.cancelled:
                raise Cancelled() from None
        return engine.client.containers.create(image, **options)


def _stream(container: Container, logger: logging.Logger) -> str:
    """The container's output, logged to logger line by line as it comes."""
    output = bytearray()
    pending = b''
    for chunk in container.logs(stream=True, follow=True):
        output += chunk
        *lines, pending = (pending + chunk).split(b'\n')
        for line in lines:
            logger.debug('%s', line.decode(errors='replace'))
    if pending:
        logger.debug('%s', pending.decode(errors='replace'))
    return output.decode(errors='replace')


def run(
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
    network: bool = True,
    commit_to: str | None = None,
    remove_image: bool = False,
    commit_unless: Path | None = None,
) -> RunResult:
    """Run image with args in a new container, and remove it.

    Args:
        engine: the engine to run it on.
        image: the image; a reference without tag means `latest`.
        args: the command, passed to the image's entrypoint.
        mounts: the bind mounts.
        env: the environment variables.
        workdir: the working directory, the image's by default.
        shm_size: the size of `/dev/shm`, the engine's default otherwise.
        as_root: whether the container starts as container root and gives its outputs back to the
            owner of its mounts itself (the builders); otherwise the engine decides the user.
        check: whether a non-zero status raises.
        cancellation: what cancels this run, among others.
        logger: where the container's output goes, line by line, at the debug level; the
            module's LOGGER by default.
        network: whether the container has the engine's default network; none otherwise.
        commit_to: the reference of an image to commit the container to, once it exited 0.
        commit_unless: a host path whose existence, once the container exited, cancels that
            commit (a container saying there is nothing more to do with it).
        remove_image: whether to remove image once the container is removed, whatever happened:
            a temporary image, such as one commit_to made.

    Raises:
        ContainerFailed: check is set and the container exited with a non-zero status.
        Cancelled: the run was cancelled before or while it ran.
    """
    cancellation = cancellation or Cancellation()
    try:
        return _run(
            engine,
            qualified(image),
            args,
            mounts=mounts,
            env=env,
            workdir=workdir,
            shm_size=shm_size,
            as_root=as_root,
            check=check,
            cancellation=cancellation,
            logger=logger,
            network=network,
            commit_to=commit_to,
            commit_unless=commit_unless,
        )
    finally:
        if remove_image:
            with suppress(docker.errors.ImageNotFound):
                engine.client.images.remove(qualified(image), force=True)


def _run(
    engine: Engine,
    image: str,
    args: Sequence[str],
    *,
    mounts: Sequence[Mount],
    env: Mapping[str, str] | None,
    workdir: str | None,
    shm_size: str | None,
    as_root: bool,
    check: bool,
    cancellation: Cancellation,
    logger: logging.Logger | None,
    network: bool,
    commit_to: str | None,
    commit_unless: Path | None,
) -> RunResult:
    """run, but for the removal of its image."""
    if cancellation.cancelled:
        raise Cancelled()
    container = _create(
        engine,
        image,
        cancellation,
        command=list(args),
        mounts=[
            docker.types.Mount(m.target, str(m.source), type='bind', read_only=m.read_only)
            for m in mounts
        ],
        environment=dict(env or {}),
        working_dir=workdir,
        shm_size=shm_size,
        user=engine.user(as_root),
        security_opt=['label=disable'],
        pids_limit=-1,
        network_mode=None if network else 'none',
        labels={LABEL.split('=')[0]: LABEL.split('=')[1]},
    )
    try:
        with cancellation.tracking(container):
            container.start()
            if cancellation.cancelled:  # cancel came between tracking and start
                _kill(container)
            output = _stream(container, logger or LOGGER)
            status: int = container.wait()['StatusCode']
            if cancellation.cancelled:
                raise Cancelled()
            stdout = container.logs(stdout=True, stderr=False).decode(errors='replace')
            skip = commit_unless is not None and commit_unless.exists()
            if commit_to is not None and status == 0 and not skip:
                repository, tag = parse_repository_tag(commit_to)
                container.commit(repository=repository, tag=tag or 'latest')
    finally:
        container.remove(force=True)
    if check and status != 0:
        raise ContainerFailed(status, [image, *args], output=output, stderr=None)
    return RunResult(status, output, stdout)
