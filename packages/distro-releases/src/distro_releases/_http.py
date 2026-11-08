# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The only module of distro-releases that reaches the network."""

import importlib.metadata

import httpx
import stamina

from distro_releases.errors import SourceError

ATTEMPTS = 5


def _version() -> str:
    """The installed version of distro-releases, or `unknown` when it runs from its sources."""
    try:
        return importlib.metadata.version('distro-releases')
    except importlib.metadata.PackageNotFoundError:
        return 'unknown'


def new_client() -> httpx.Client:
    """An HTTP client for the sources: 30 s timeout, redirects followed, a User-Agent of ours."""
    return httpx.Client(
        timeout=30,
        follow_redirects=True,
        headers={'User-Agent': f'distro-releases/{_version()}'},
    )


def _transient(error: Exception) -> bool:
    """Whether a new attempt may succeed: a transport error or a server error."""
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.is_server_error
    return isinstance(error, httpx.TransportError)


@stamina.retry(on=_transient, attempts=ATTEMPTS)
def _get(client: httpx.Client, url: str) -> httpx.Response:
    return client.get(url).raise_for_status()


def get(client: httpx.Client, url: str) -> httpx.Response:
    """The successful response to a GET of url.

    Transport errors and server errors are retried, ATTEMPTS times in all.

    Raises:
        SourceError: any other failure, or the last attempt's.
    """
    try:
        return _get(client, url)
    except httpx.HTTPError as error:
        raise SourceError(url, str(error)) from error
