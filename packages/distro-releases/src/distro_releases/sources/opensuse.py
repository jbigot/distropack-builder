# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""openSUSE: Leap's releases from endoflife.date, and Tumbleweed, the rolling release."""

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from distro_releases.model import Distribution, Row
from distro_releases.sources._common import cpe
from distro_releases.sources.endoflife import Endoflife

if TYPE_CHECKING:
    import httpx

# Leap 16.0 is the first release of its new base (SLES 16); 15.6 ended in April 2026. Leap's
# numbers are not chronological: 42.1 to 42.3 (2015 to 2017) came before 15.0.
FIRST_LEAP = (16, 0)
OLD_NUMBERING = (42,)
# Tumbleweed's first snapshot: it has been released, and never ends, ever since.
TUMBLEWEED_START = date(2014, 11, 4)


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split('.'))


@dataclass(frozen=True, slots=True)
class OpenSuse:
    """Leap's releases from 16.0 on, ordered by version, then Tumbleweed."""

    def rows(self, distribution: Distribution, client: 'httpx.Client') -> tuple[Row, ...]:
        """Leap's rows from endoflife.date's openSUSE product, and Tumbleweed's."""
        leaps = sorted(
            (row for row in Endoflife('opensuse').rows(distribution, client)),
            key=lambda row: _key(str(row.version)),
        )
        leaps = [row for row in leaps if FIRST_LEAP <= _key(str(row.version)) < OLD_NUMBERING]
        result = [
            Row(
                version=row.version,
                codename=None,
                name=row.name,
                order=order,
                created=None,
                release_date=row.release_date,
                eol_date=row.eol_date,
                cpe=cpe('cpe:/o:opensuse:leap', row.version),
            )
            for order, row in enumerate(leaps)
        ]
        result.append(
            Row(
                version='tumbleweed',
                codename=None,
                name='Tumbleweed',
                order=len(leaps),
                created=None,
                release_date=TUMBLEWEED_START,
                eol_date=None,
                cpe='cpe:/o:opensuse:tumbleweed',
                suite='tumbleweed',
            )
        )
        return tuple(result)
