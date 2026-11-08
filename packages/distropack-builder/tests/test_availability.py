# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Whether the registry publishes an image, asked anonymously."""

from collections.abc import Iterator

import httpx
import pytest
import respx
import stamina

from distropack_builder.availability import AvailabilityError, image_available

IMAGE = 'ghcr.io/jbigot/distropack-builder/debian_builder:trixie-p2'
MANIFEST = 'https://ghcr.io/v2/jbigot/distropack-builder/debian_builder/manifests/trixie-p2'
SCOPE = 'repository:jbigot/distropack-builder/debian_builder:pull'
CHALLENGE = f'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="{SCOPE}"'


@pytest.fixture(autouse=True)
def no_wait() -> Iterator[None]:
    """Retries happen without waiting."""
    with stamina.set_testing(True, attempts=3):
        yield


@pytest.fixture
def registry() -> Iterator[respx.MockRouter]:
    """A registry that asks for a token, and gives one for the image's pull scope."""
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.get(
            'https://ghcr.io/token', params={'service': 'ghcr.io', 'scope': SCOPE}, name='token'
        ).respond(200, json={'token': 'tok'})
        yield router


def test_available(registry: respx.MockRouter) -> None:
    authorized = registry.head(
        MANIFEST, headers={'Authorization': 'Bearer tok'}, name='authorized'
    ).respond(200)
    registry.head(MANIFEST).respond(401, headers={'www-authenticate': CHALLENGE})

    with httpx.Client() as client:
        assert image_available(IMAGE, client) is True

    assert registry['token'].call_count == 1
    assert authorized.call_count == 1
    assert 'manifest' in authorized.calls.last.request.headers['Accept']


def test_unavailable(registry: respx.MockRouter) -> None:
    registry.head(MANIFEST, headers={'Authorization': 'Bearer tok'}).respond(404)
    registry.head(MANIFEST).respond(401, headers={'www-authenticate': CHALLENGE})

    with httpx.Client() as client:
        assert image_available(IMAGE, client) is False


def test_no_challenge(registry: respx.MockRouter) -> None:
    registry.head(MANIFEST).respond(200)

    with httpx.Client() as client:
        assert image_available(IMAGE, client) is True
    assert registry['token'].call_count == 0


@pytest.mark.parametrize(
    'answer',
    [
        pytest.param(httpx.Response(429), id='rate-limited'),
        pytest.param(httpx.Response(503), id='server-error'),
        pytest.param(httpx.ConnectError('down'), id='transport-error'),
        pytest.param(httpx.Response(401, headers={'www-authenticate': 'Basic'}), id='basic'),
    ],
)
def test_available_registry_error(
    registry: respx.MockRouter, answer: httpx.Response | Exception
) -> None:
    route = registry.head(MANIFEST)
    if isinstance(answer, Exception):
        route.side_effect = answer
    else:
        route.return_value = answer

    with httpx.Client() as client, pytest.raises(AvailabilityError, match=IMAGE):
        image_available(IMAGE, client)


def test_token_refused(registry: respx.MockRouter) -> None:
    registry['token'].return_value = httpx.Response(403)
    registry.head(MANIFEST).respond(401, headers={'www-authenticate': CHALLENGE})

    with httpx.Client() as client, pytest.raises(AvailabilityError, match='403'):
        image_available(IMAGE, client)


def test_token_not_json(registry: respx.MockRouter) -> None:
    """A realm that answers a page, as a captive portal does, is an error, not a traceback."""
    registry['token'].return_value = httpx.Response(200, text='<html>sign in</html>')
    registry.head(MANIFEST).respond(401, headers={'www-authenticate': CHALLENGE})

    with httpx.Client() as client, pytest.raises(AvailabilityError, match='JSON'):
        image_available(IMAGE, client)
