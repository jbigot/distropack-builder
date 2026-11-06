"""Which engine the builder talks to."""

from collections.abc import Callable
from pathlib import Path

import pytest

from distropack_builder.docker.endpoint import EngineNotFound, RemoteEndpoint, resolve

DEFAULT = 'unix:///var/run/docker.sock'
PODMAN = 'unix:///run/user/1000/podman/podman.sock'
ENV = {'XDG_RUNTIME_DIR': '/run/user/1000'}


def exists(*paths: str) -> Callable[[Path], bool]:
    return lambda path: str(path) in paths


def test_override_wins() -> None:
    environ = ENV | {'DOCKER_HOST': 'unix:///a.sock'}
    assert resolve('unix:///o.sock', environ, 'unix:///c.sock', exists()) == 'unix:///o.sock'


def test_docker_host() -> None:
    environ = ENV | {'DOCKER_HOST': 'unix:///a.sock'}
    assert resolve(None, environ, 'unix:///c.sock', exists()) == 'unix:///a.sock'


def test_context() -> None:
    assert resolve(None, ENV, 'unix:///c.sock', exists()) == 'unix:///c.sock'


def test_default_socket() -> None:
    paths = exists('/var/run/docker.sock', '/run/user/1000/podman/podman.sock')
    assert resolve(None, ENV, None, paths) == DEFAULT


def test_podman_socket() -> None:
    assert resolve(None, ENV, None, exists('/run/user/1000/podman/podman.sock')) == PODMAN


def test_nothing_found() -> None:
    with pytest.raises(EngineNotFound) as error:
        resolve(None, ENV, None, exists())
    assert DEFAULT in str(error.value) and PODMAN in str(error.value)
    assert error.value.tried == [DEFAULT, PODMAN]


def test_nothing_found_without_runtime_dir() -> None:
    with pytest.raises(EngineNotFound) as error:
        resolve(None, {}, None, exists())
    assert error.value.tried == [DEFAULT]


@pytest.mark.parametrize('url', ['tcp://host:2375', 'ssh://user@host', 'http://host'])
@pytest.mark.parametrize('where', ['override', 'DOCKER_HOST', 'context'])
def test_remote_refused(url: str, where: str) -> None:
    override = url if where == 'override' else None
    environ = ENV | ({'DOCKER_HOST': url} if where == 'DOCKER_HOST' else {})
    context = url if where == 'context' else None
    with pytest.raises(RemoteEndpoint, match='bind mounts') as error:
        resolve(override, environ, context, exists())
    assert error.value.url == url


def test_path_check_uses_paths() -> None:
    seen: list[Path] = []

    def record(path: Path) -> bool:
        seen.append(path)
        return False

    with pytest.raises(EngineNotFound):
        resolve(None, ENV, None, record)
    assert seen == [Path('/var/run/docker.sock'), Path('/run/user/1000/podman/podman.sock')]
