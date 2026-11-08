# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The engine's rootlessness, and the container user it implies."""

import json
import os
from pathlib import Path

import pytest

from distropack_builder.docker.engine import Engine, container_user, is_rootless

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


@pytest.mark.docker
@pytest.mark.enable_socket
def test_live_payload_matches() -> None:
    """In CI, the engine of the job classifies as the payload recorded for that kind of engine."""
    kind = os.environ.get('DISTROPACK_TEST_ENGINE')
    if kind is None:
        pytest.skip('DISTROPACK_TEST_ENGINE names the engine in CI only')
    recorded = json.loads((DATA / f'info-{kind}.json').read_text())
    assert Engine.connect().rootless is is_rootless(recorded)
