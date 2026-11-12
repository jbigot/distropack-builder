# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""A release's state in an OCI registry: pushed, pulled, tagged back; absent versus unreachable."""

import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from distropack_builder import registry, state

UID = 'debian-13-trixie'


def write_state(states: Path, marker: str) -> None:
    out = states.parent / f'out-{marker}'
    out.mkdir(parents=True)
    (out / f'pdi_{marker}.deb').write_text(marker)
    writer = state.ReleaseStateWriter(states, 'stable', UID)
    writer.add(
        'pdi',
        static_key='s',
        env_key='e',
        base_version=marker,
        rebuild=0,
        version=marker,
        snapshot_commit=None,
        packaging_hash='p',
        outdir=out,
        source_files=[],
        arch_all_files=[],
    )
    writer.commit()


def files(states: Path) -> dict[str, bytes]:
    directory = states / 'stable' / UID
    return {
        str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()
    }


def test_annotations_from_github_env() -> None:
    environ = {
        'GITHUB_SERVER_URL': 'https://github.com',
        'GITHUB_REPOSITORY': 'pdidev/pkgs',
        'GITHUB_SHA': 'abc',
        'GITHUB_RUN_ID': '42',
    }
    assert registry.annotations(environ) == {
        'org.opencontainers.image.source': 'https://github.com/pdidev/pkgs',
        'org.opencontainers.image.revision': 'abc',
        'dev.distropack.run-id': '42',
    }
    assert registry.annotations({}) == {}


class Refusing(BaseHTTPRequestHandler):
    """A registry refusing every request, as one does a wrong token."""

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(401)
        self.send_header('Content-Length', '0')
        self.end_headers()

    do_HEAD = do_GET

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """oras-py waits minutes between its retries: not in the tests."""
    monkeypatch.setattr('oras.decorator.time.sleep', lambda seconds: None)


def test_refused_token_fails(tmp_path: Path, no_retry_wait: None) -> None:
    server = ThreadingHTTPServer(('127.0.0.1', 0), Refusing)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        states = tmp_path / 'states'
        write_state(states, '1.0')
        before = files(states)
        reg = registry.Registry(f'127.0.0.1:{server.server_port}/state', token='x', insecure=True)
        with pytest.raises(registry.RegistryError):
            reg.pull('stable', [UID], states)
        assert files(states) == before
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def local_registry() -> Iterator[str]:
    """A throw-away registry:2 on a free port, plain HTTP; its address."""
    name = f'distropack-test-registry-{time.monotonic_ns()}'
    subprocess.run(
        ['docker', 'run', '-d', '--name', name, '-p', '127.0.0.1::5000', 'registry:2'],
        check=True,
        capture_output=True,
    )
    try:
        port = (
            subprocess.run(
                ['docker', 'port', name, '5000'], check=True, capture_output=True, text=True
            )
            .stdout.split(':')[-1]
            .strip()
        )
        address = f'127.0.0.1:{port}'
        for _ in range(50):
            if (
                subprocess.run(
                    ['curl', '-fs', f'http://{address}/v2/'], capture_output=True
                ).returncode
                == 0
            ):
                break
            time.sleep(0.1)
        yield address
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True)


@pytest.mark.docker
def test_push_then_pull_gives_the_same_tree(local_registry: str, tmp_path: Path) -> None:
    states = tmp_path / 'states'
    write_state(states, '1.0')
    reg = registry.Registry(f'{local_registry}/pkgs/state', insecure=True)
    digest = reg.push('stable', UID, states, {'a': 'b'})
    assert digest.startswith('sha256:')
    other = tmp_path / 'other'
    assert reg.pull('stable', [UID], other) == []
    assert files(other) == files(states)


@pytest.mark.docker
def test_missing_tag_is_no_state(local_registry: str, tmp_path: Path) -> None:
    reg = registry.Registry(f'{local_registry}/pkgs/state', insecure=True)
    states = tmp_path / 'states'
    assert reg.pull('stable', [UID], states) == [f'stable.{UID}']
    assert not states.exists() or not any(states.rglob('*'))


@pytest.mark.docker
def test_unreachable_registry_fails(
    local_registry: str, tmp_path: Path, no_retry_wait: None
) -> None:
    states = tmp_path / 'states'
    write_state(states, '1.0')
    reg = registry.Registry(f'{local_registry}/pkgs/state', insecure=True)
    reg.push('stable', UID, states, {})
    before = files(states)
    for line in subprocess.run(
        ['docker', 'ps', '--format', '{{.Names}}'], capture_output=True, text=True, check=True
    ).stdout.split():
        if line.startswith('distropack-test-registry-'):
            subprocess.run(['docker', 'stop', line], capture_output=True, check=True)
    with pytest.raises(registry.RegistryError):
        reg.pull('stable', [UID], states)
    assert files(states) == before


@pytest.mark.docker
def test_interrupted_push_keeps_the_previous_content(
    local_registry: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    states = tmp_path / 'states'
    write_state(states, '1.0')
    reg = registry.Registry(f'{local_registry}/pkgs/state', insecure=True)
    reg.push('stable', UID, states, {})
    first = files(states)
    write_state(states, '2.0')

    def interrupted(*args: object, **kwargs: object) -> None:
        raise ConnectionError('closed after the blobs')

    monkeypatch.setattr(reg.client, 'upload_manifest', interrupted)
    with pytest.raises(registry.RegistryError):
        reg.push('stable', UID, states, {})
    monkeypatch.undo()
    other = tmp_path / 'other'
    registry.Registry(f'{local_registry}/pkgs/state', insecure=True).pull('stable', [UID], other)
    assert files(other) == first


@pytest.mark.docker
def test_tag_rolls_back(local_registry: str, tmp_path: Path) -> None:
    states = tmp_path / 'states'
    write_state(states, '1.0')
    reg = registry.Registry(f'{local_registry}/pkgs/state', insecure=True)
    first_digest = reg.push('stable', UID, states, {})
    first = files(states)
    write_state(states, '2.0')
    reg.push('stable', UID, states, {})
    reg.tag(first_digest, f'stable.{UID}')
    other = tmp_path / 'other'
    reg.pull('stable', [UID], other)
    assert files(other) == first
