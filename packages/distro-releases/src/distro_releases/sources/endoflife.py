# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The major releases of a distribution, from endoflife.date."""

from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from distro_releases import _http
from distro_releases.model import Distribution, Row
from distro_releases.sources._common import cpe, parsing

URL = 'https://endoflife.date/api/v1/products/{product}'


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _rows(data: dict[str, Any], distribution: Distribution) -> tuple[Row, ...]:
    """The releases of an endoflife.date product, majors numbered as integers."""
    result = [
        Row(
            version=release['name'],
            codename=None,
            name=release['name'],
            order=int(release['name']),
            created=None,
            release_date=_date(release.get('releaseDate')),
            eol_date=_date(release.get('eolFrom')),
            cpe=cpe(distribution.cpe_prefix, release['name']),
        )
        for release in data['result']['releases']
    ]
    return tuple(sorted(result, key=lambda row: row.order))


@dataclass(frozen=True, slots=True)
class Endoflife:
    """endoflife.date's product, which gathers the vendor's own dates.

    Attributes:
        product: the product's name on endoflife.date (`rocky-linux` for Rocky Linux).
    """

    product: str

    def rows(self, distribution: Distribution, client: httpx.Client) -> tuple[Row, ...]:
        """The product's releases."""
        url = URL.format(product=self.product)
        response = _http.get(client, url)
        with parsing(url):
            return _rows(response.json(), distribution)
