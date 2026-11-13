# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

""".github/scripts/ci-state-cleanup: the Action workflow's states, deleted after its run."""

import importlib.machinery
import importlib.util
from types import ModuleType

import httpx
import pytest
import respx

from support import REPO_ROOT

API = 'https://api.github.com'
VERSIONS = f'{API}/orgs/jbigot/packages/container/distropack-builder%2Fci-state/versions'
USER_VERSIONS = f'{API}/users/jbigot/packages/container/distropack-builder%2Fci-state/versions'
PACKAGE = f'{API}/users/jbigot/packages/container/distropack-builder%2Fci-state'


def script() -> ModuleType:
    path = REPO_ROOT / '.github' / 'scripts' / 'ci-state-cleanup'
    loader = importlib.machinery.SourceFileLoader('ci_state_cleanup', str(path))
    spec = importlib.util.spec_from_loader('ci_state_cleanup', loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def version(number: int, *tags: str) -> dict[str, object]:
    return {'id': number, 'metadata': {'container': {'tags': list(tags)}}}


def test_deletes_this_run_s_tags_only(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(VERSIONS).respond(404)
    respx_mock.get(USER_VERSIONS, params={'per_page': '100'}).respond(
        200,
        json=[version(1, 'stable-7-1.debian-13-trixie'), version(2, 'stable-6-1.fedora-44')],
        headers={'Link': f'<{USER_VERSIONS}?page=2>; rel="next"'},
    )
    respx_mock.get(USER_VERSIONS, params={'page': '2'}).respond(
        200, json=[version(3, 'weekly-7-1.fedora-44'), version(4)]
    )
    one = respx_mock.delete(f'{USER_VERSIONS}/1').respond(204)
    three = respx_mock.delete(f'{USER_VERSIONS}/3').respond(204)
    with httpx.Client() as client:
        deleted = script().cleanup(
            'ghcr.io/jbigot/distropack-builder/ci-state',
            ['stable-7-1.', 'weekly-7-1.'],
            'x',
            client,
        )
    assert deleted == [1, 3]
    assert one.called and three.called


def test_the_last_version_takes_the_package(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(VERSIONS).respond(404)
    respx_mock.get(USER_VERSIONS).respond(200, json=[version(1, 'stable-7-1.fedora-44')])
    respx_mock.delete(f'{USER_VERSIONS}/1').respond(
        400, json={'message': 'You cannot delete the last tagged version of a package.'}
    )
    package = respx_mock.delete(PACKAGE).respond(204)
    with httpx.Client() as client:
        assert script().cleanup(
            'ghcr.io/jbigot/distropack-builder/ci-state', ['stable-7-1.'], 'x', client
        ) == [1]
    assert package.called


def test_api_failure_fails(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(VERSIONS).respond(403)
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        script().cleanup('ghcr.io/jbigot/distropack-builder/ci-state', ['x.'], 'x', client)
