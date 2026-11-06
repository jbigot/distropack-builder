"""The major releases of a distribution, from endoflife.date."""

from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from distro_releases import _http
from distro_releases.model import Distribution, Row
from distro_releases.sources._common import cpe

URL = 'https://endoflife.date/api/v1/products/{product}'


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


@dataclass(frozen=True, slots=True)
class Endoflife:
    """endoflife.date's product, which gathers the vendor's own dates.

    Attributes:
        product: the product's name on endoflife.date (`rocky-linux` for Rocky Linux).
    """

    product: str

    def rows(self, distribution: Distribution, client: httpx.Client) -> tuple[Row, ...]:
        """The product's releases, majors numbered as integers."""
        data: dict[str, Any] = _http.get(client, URL.format(product=self.product)).json()
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
