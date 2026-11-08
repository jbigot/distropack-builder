# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Whether a registry publishes an image, asked anonymously with one manifest request.

The registry's answer must be clear: an error is never read as a missing image, which would
silently drop a release from the build.
"""

import re

import httpx
import stamina

ATTEMPTS = 5
# The manifest types of an image or of a multi-platform index, OCI's and Docker's.
ACCEPT = ', '.join(
    [
        'application/vnd.oci.image.index.v1+json',
        'application/vnd.oci.image.manifest.v1+json',
        'application/vnd.docker.distribution.manifest.list.v2+json',
        'application/vnd.docker.distribution.manifest.v2+json',
    ]
)
_PARAMETER = re.compile(r'(\w+)="([^"]*)"')


class AvailabilityError(Exception):
    """The registry did not tell whether it publishes an image."""

    def __init__(self, image: str, reason: str) -> None:
        """The failure to probe image, for reason."""
        super().__init__(f'cannot tell whether the registry publishes {image}: {reason}')
        self.image = image


def image_available(image: str, client: httpx.Client) -> bool:
    """Whether the registry publishes image, a reference such as `ghcr.io/o/r/name:tag`.

    A registry that asks for authentication gets an anonymous token from the realm its challenge
    names, for the scope it names.

    Raises:
        AvailabilityError: the registry answered neither that the image exists nor that it does
            not, after retrying transport and server errors.
    """
    registry, _, reference = image.partition('/')
    repository, _, tag = reference.rpartition(':')
    url = f'https://{registry}/v2/{repository}/manifests/{tag}'
    try:
        response = _head(client, url, {'Accept': ACCEPT})
        if response.status_code == httpx.codes.UNAUTHORIZED:
            token = _token(client, response.headers.get('www-authenticate', ''))
            response = _head(client, url, {'Accept': ACCEPT, 'Authorization': f'Bearer {token}'})
    except httpx.HTTPError as error:
        raise AvailabilityError(image, str(error)) from error
    except _Unsupported as error:
        raise AvailabilityError(image, str(error)) from error
    if response.status_code == httpx.codes.OK:
        return True
    if response.status_code == httpx.codes.NOT_FOUND:
        return False
    raise AvailabilityError(image, f'the registry answered {response.status_code}')


class _Unsupported(Exception):
    """A challenge that asks for something else than an anonymous bearer token."""


def _transient(error: Exception) -> bool:
    """Whether a new attempt may succeed: a server error, or a transport error but a bad URL."""
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.is_server_error
    return isinstance(error, httpx.TransportError) and not isinstance(
        error, httpx.UnsupportedProtocol
    )


@stamina.retry(on=_transient, attempts=ATTEMPTS)
def _head(client: httpx.Client, url: str, headers: dict[str, str]) -> httpx.Response:
    """The answer to a HEAD request; a server error raises, to be retried."""
    response = client.head(url, headers=headers)
    if response.is_server_error:
        response.raise_for_status()
    return response


@stamina.retry(on=_transient, attempts=ATTEMPTS)
def _token(client: httpx.Client, challenge: str) -> str:
    """An anonymous token, from the realm of a Bearer challenge, for its service and scope."""
    scheme, _, parameters = challenge.partition(' ')
    values = dict(_PARAMETER.findall(parameters))
    if scheme.lower() != 'bearer' or 'realm' not in values:
        raise _Unsupported(f'unsupported authentication challenge {challenge!r}')
    realm = values.pop('realm')
    response = client.get(realm, params=values)
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError:
        raise _Unsupported(f'{realm} did not answer JSON') from None
    token = body.get('token') or body.get('access_token') if isinstance(body, dict) else None
    if not isinstance(token, str):
        raise _Unsupported(f'{realm} gave no token')
    return token
