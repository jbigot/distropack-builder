# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Fixtures of the unit tests of distro-releases."""

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
import stamina

from distro_releases import catalog

DATA = Path(__file__).parent / 'data'


@pytest.fixture(autouse=True)
def no_retry_wait() -> Iterator[None]:
    """Retries happen without waiting, up to five attempts as in production."""
    with stamina.set_testing(True, attempts=5):
        yield


@pytest.fixture
def recorded() -> Iterator[respx.MockRouter]:
    """Serve the recorded release data; any other URL fails."""
    urls: dict[str, str] = json.loads((DATA / 'urls.json').read_text())
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        for url, name in urls.items():
            router.get(url, name=url).mock(
                return_value=httpx.Response(200, content=(DATA / name).read_bytes())
            )
        yield router


@pytest.fixture(autouse=True)
def fresh_catalog() -> Iterator[None]:
    """Each test fetches the release data again, as a new process would."""
    catalog._fetch.cache_clear()
    yield
    catalog._fetch.cache_clear()
