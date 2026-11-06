"""The engine's rootlessness, and the container user it implies."""

import json
from pathlib import Path

import pytest

from distropack_builder.docker.engine import container_user, is_rootless

DATA = Path(__file__).parent / 'data'


@pytest.mark.parametrize(
    ('engine', 'rootless'), [('docker', False), ('docker-rootless', True), ('podman', True)]
)
def test_rootless_payloads(engine: str, rootless: bool) -> None:
    assert is_rootless(json.loads((DATA / f'info-{engine}.json').read_text())) is rootless


def test_payload_without_security_options() -> None:
    assert is_rootless({}) is False


def test_container_user() -> None:
    assert container_user(rootless=False, as_root=False, uid=1000, gid=100) == '1000:100'
    assert container_user(rootless=False, as_root=True, uid=1000, gid=100) is None
    assert container_user(rootless=True, as_root=False, uid=1000, gid=100) is None
    assert container_user(rootless=True, as_root=True, uid=1000, gid=100) is None
