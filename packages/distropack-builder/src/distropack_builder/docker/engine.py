"""The connected engine, and the container user its rootlessness implies."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import docker

from distropack_builder.docker import endpoint


def is_rootless(info: Mapping[str, Any]) -> bool:
    """Whether the engine whose `info()` this is runs rootless (Docker and Podman alike)."""
    return 'name=rootless' in info.get('SecurityOptions', [])


def container_user(rootless: bool, as_root: bool, uid: int, gid: int) -> str | None:
    """The user a container runs as; `None` for the image's own, container root.

    Under a rootless engine, container root is the invoking user, and any other uid maps to a
    subordinate one that cannot write the mounts: no user. Under rootful Docker, a tool container
    runs as the invoking user, so that what it writes belongs to them; a container started `as_root`
    (a builder) hands its outputs back to the owner of its mounts itself.
    """
    return None if rootless or as_root else f'{uid}:{gid}'


@dataclass(frozen=True)
class Engine:
    """A container engine, connected.

    Attributes:
        client: docker-py's client of the engine.
        rootless: whether the engine runs rootless.
    """

    client: docker.DockerClient
    rootless: bool

    @classmethod
    def connect(cls, override: str | None = None) -> 'Engine':
        """The engine at the endpoint `endpoint.resolve` chooses.

        Raises:
            RemoteEndpoint: the endpoint is not a local socket.
            EngineNotFound: no endpoint was given and no socket exists.
            docker.errors.DockerException: the engine does not answer.
        """
        url = endpoint.resolve(
            override,
            os.environ,
            endpoint.current_context_host(os.environ),
            lambda p: Path(p).exists(),
        )
        client = docker.DockerClient(base_url=url)
        return cls(client, is_rootless(client.info()))

    def user(self, as_root: bool) -> str | None:
        """The user a container runs as on this engine (see `container_user`)."""
        return container_user(self.rootless, as_root, os.getuid(), os.getgid())
