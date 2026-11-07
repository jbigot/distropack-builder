"""Which engine the builder talks to."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from distropack_builder.docker.endpoint import (
    EngineNotFound,
    RemoteEndpoint,
    current_context_host,
    resolve,
)

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


def docker_config(tmp_path: Path, current: str | None, **contexts: str) -> Path:
    """A docker CLI configuration directory: its current context, and each context's host."""
    # Always written: without it, docker-py falls back to the invoking user's configuration.
    (tmp_path / 'config.json').write_text(
        json.dumps({'currentContext': current} if current else {})
    )
    for name, host in contexts.items():
        meta = tmp_path / 'contexts' / 'meta' / hashlib.sha256(name.encode()).hexdigest()
        meta.mkdir(parents=True)
        endpoints = {'docker': {'Host': host, 'SkipTLSVerify': False}}
        (meta / 'meta.json').write_text(
            json.dumps({'Name': name, 'Metadata': {}, 'Endpoints': endpoints})
        )
    return tmp_path


@pytest.mark.parametrize(
    ('current', 'variable', 'expected'),
    [
        (None, None, None),
        ('used', None, 'unix:///used.sock'),
        ('used', 'chosen', 'unix:///chosen.sock'),
        (None, 'chosen', 'unix:///chosen.sock'),
        ('used', 'default', None),
    ],
)
def test_current_context_as_the_cli_reads_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current: str | None,
    variable: str | None,
    expected: str | None,
) -> None:
    """DOCKER_CONTEXT wins over the configuration's current context, as for the docker CLI."""
    config = docker_config(
        tmp_path, current, used='unix:///used.sock', chosen='unix:///chosen.sock'
    )
    monkeypatch.setenv('DOCKER_CONFIG', str(config))
    environ = {} if variable is None else {'DOCKER_CONTEXT': variable}
    assert current_context_host(environ) == expected
