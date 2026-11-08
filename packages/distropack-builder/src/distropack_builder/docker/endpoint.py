# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Which container engine to talk to.

In order: the builder's own option, `DOCKER_HOST`, the current Docker context, Docker's default
socket, then Podman's socket. The endpoint must be local, since the containers' bind mounts name
paths of this host.
"""

from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlparse

from docker.context import ContextAPI

DEFAULT_SOCKET = Path('/var/run/docker.sock')


class EngineNotFound(RuntimeError):
    """No endpoint was given and no engine socket exists.

    Attributes:
        tried: the endpoints looked for, in order.
    """

    def __init__(self, tried: list[str]) -> None:
        """An error listing the endpoints looked for."""
        super().__init__(
            f'no container engine found at {", ".join(tried)}; start one, or give its endpoint '
            'with DOCKER_HOST or --docker-host'
        )
        self.tried = tried


class RemoteEndpoint(ValueError):
    """An endpoint that is not a local socket.

    Attributes:
        url: the endpoint.
    """

    def __init__(self, url: str) -> None:
        """An error for url."""
        super().__init__(
            f'{url} is not a local engine: the containers bind mounts paths of this host, so the '
            'endpoint must be a unix:// socket'
        )
        self.url = url


def _local(url: str) -> str:
    """url, if it is a local socket."""
    if urlparse(url).scheme != 'unix':
        raise RemoteEndpoint(url)
    return url


def resolve(
    override: str | None,
    environ: Mapping[str, str],
    context_host: str | None,
    exists: Callable[[Path], bool],
) -> str:
    """The endpoint of the engine to use.

    Args:
        override: the endpoint the user gave the builder, if any.
        environ: the environment, for `DOCKER_HOST` and `XDG_RUNTIME_DIR`.
        context_host: the endpoint of the current Docker context, `None` for the default one.
        exists: whether a socket path exists.

    Raises:
        RemoteEndpoint: the chosen endpoint is not a unix socket.
        EngineNotFound: nothing was given and no socket exists.
    """
    for given in (override, environ.get('DOCKER_HOST'), context_host):
        if given:
            return _local(given)
    sockets = [DEFAULT_SOCKET]
    if environ.get('XDG_RUNTIME_DIR'):
        sockets.append(Path(environ['XDG_RUNTIME_DIR']) / 'podman' / 'podman.sock')
    for socket in sockets:
        if exists(socket):
            return f'unix://{socket}'
    raise EngineNotFound([f'unix://{socket}' for socket in sockets])


def current_context_host(environ: Mapping[str, str]) -> str | None:
    """The endpoint of the current Docker context, `None` when it is the default one.

    The current context is `DOCKER_CONTEXT`'s when it is set, the configuration's otherwise, as
    for the docker CLI (docker-py's `get_current_context` reads only the configuration).
    """
    context = ContextAPI.get_context(environ.get('DOCKER_CONTEXT') or None)
    if context is None or context.name == 'default':
        return None
    host: str | None = context.Host
    return host
