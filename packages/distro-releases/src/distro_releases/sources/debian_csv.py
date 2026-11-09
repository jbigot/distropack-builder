# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Debian's and Ubuntu's releases, from distro-info-data's CSV files."""

import csv
import io
import re
from dataclasses import dataclass
from datetime import date

import httpx

from distro_releases import _http
from distro_releases.model import Distribution, Row
from distro_releases.sources._common import cpe, parsing

URL = 'https://debian.pages.debian.net/distro-info-data/{id}.csv'
# The first columns, in this order.
TITLES = ('version', 'codename', 'series', 'created', 'release', 'eol')
# The column of the extended support the archive gives for free, whichever the CSV has: Debian's
# LTS, Ubuntu's server support. Not Debian's ELTS nor Ubuntu's ESM, which need a subscription.
EXTENDED = ('eol-lts', 'eol-server')


def _date(columns: list[str], index: int) -> date | None:
    """The date in a column, `None` when the column is absent or empty."""
    if len(columns) <= index or not columns[index].strip():
        return None
    return date.fromisoformat(columns[index].strip())


def _rows(text: str, distribution: Distribution) -> tuple[Row, ...]:
    """The rows of the CSV text, from its first six columns and its extended support's."""
    titles, *lines = csv.reader(io.StringIO(text), dialect='unix')
    if titles[: len(TITLES)] != list(TITLES):
        raise ValueError(f'the titles are {titles}, not {TITLES}...')
    extended = next((titles.index(title) for title in EXTENDED if title in titles), None)
    result = []
    for order, columns in enumerate(lines):
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
                extended_eol_date=None if extended is None else _date(columns, extended),
            )
        )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class DebianCsv:
    """distro-info-data's `<distribution id>.csv`, which Debian maintains for both."""

    def rows(self, distribution: Distribution, client: httpx.Client) -> tuple[Row, ...]:
        """The releases of the distribution's CSV file."""
        url = URL.format(id=distribution.id)
        text = _http.get(client, url).text
        with parsing(url):
            return _rows(text, distribution)
