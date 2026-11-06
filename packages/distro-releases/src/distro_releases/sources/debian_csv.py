"""Debian's and Ubuntu's releases, from distro-info-data's CSV files."""

import csv
import io
import re
from dataclasses import dataclass
from datetime import date

import httpx

from distro_releases import _http
from distro_releases.model import Distribution, Row
from distro_releases.sources._common import cpe

URL = 'https://debian.pages.debian.net/distro-info-data/{id}.csv'


def _date(columns: list[str], index: int) -> date | None:
    """The date in a column, `None` when the column is absent or empty."""
    if len(columns) <= index or not columns[index].strip():
        return None
    return date.fromisoformat(columns[index].strip())


@dataclass(frozen=True, slots=True)
class DebianCsv:
    """distro-info-data's `<distribution id>.csv`, which Debian maintains for both."""

    def rows(self, distribution: Distribution, client: httpx.Client) -> tuple[Row, ...]:
        """The releases of the file: version, codename, series, created, release, eol columns."""
        text = _http.get(client, URL.format(id=distribution.id)).text
        result = []
        # The first line holds the titles.
        for order, columns in enumerate(list(csv.reader(io.StringIO(text), dialect='unix'))[1:]):
            number, codename, series = columns[0], columns[1], columns[2]
            # `26.04 LTS` is version 26.04; sid has none.
            version = re.sub(r'[^0-9.].*', '', number) or None
            result.append(
                Row(
                    version=version,
                    codename=series,
                    name=codename if number == '' else f'{number} ({codename})',
                    order=order,
                    created=_date(columns, 3),
                    release_date=_date(columns, 4),
                    eol_date=_date(columns, 5),
                    cpe=cpe(distribution.cpe_prefix, version),
                )
            )
        return tuple(result)
