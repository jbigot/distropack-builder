# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The model: identity, display, order and status of the releases."""

from dataclasses import replace
from datetime import date

import httpx
import pytest

from distro_releases.model import Distribution, Release, Row


class NoSource:
    """A source that has no release, for the distributions built by hand here."""

    def rows(self, distribution: Distribution, client: httpx.Client) -> tuple[Row, ...]:
        return ()


DEBIAN = Distribution('debian', 'Debian GNU/Linux', ('debian',), (), None, NoSource())
FEDORA = Distribution(
    'fedora', 'Fedora', ('fedora',), (), 'cpe:/o:fedoraproject:fedora', NoSource()
)


def release(distribution: Distribution = DEBIAN, **fields: object) -> Release:
    values: dict[str, object] = {
        'version': '13',
        'codename': 'trixie',
        'name': '13 (Trixie)',
        'order': 13,
        'suite': None,
        'created': date(2023, 6, 10),
        'release_date': date(2025, 8, 9),
        'eol_date': date(2028, 8, 9),
        'cpe': None,
    }
    return Release(distribution, **(values | fields))  # type: ignore[arg-type]


def test_uid_and_str() -> None:
    trixie = release()
    assert trixie.uid == 'debian-13-trixie'
    assert str(trixie) == 'Debian GNU/Linux 13 (Trixie)'
    assert release(version=None, codename='sid', name='Sid').uid == 'debian--sid'
    assert release(FEDORA, version='44', codename=None, name='44').uid == 'fedora-44'
    assert str(DEBIAN) == 'Debian GNU/Linux'


def test_status_on_boundaries() -> None:
    trixie = release()
    assert not trixie.is_released(date(2025, 8, 8))
    assert not trixie.is_supported(date(2025, 8, 8))
    assert trixie.is_released(date(2025, 8, 9))
    assert trixie.is_supported(date(2025, 8, 9))
    assert not trixie.is_eol(date(2028, 8, 8))
    assert trixie.is_eol(date(2028, 8, 9))
    assert not trixie.is_supported(date(2028, 8, 9))


def test_status_defaults_to_today() -> None:
    assert release(release_date=date.min, eol_date=None).is_supported()


def test_unscheduled_dates() -> None:
    sid = release(version=None, codename='sid', name='Sid', release_date=None, eol_date=None)
    assert not sid.is_released(date.max)
    assert not sid.is_eol(date.max)
    assert not sid.is_supported(date.max)


def test_is_maintained() -> None:
    bookworm = release(
        version='12',
        codename='bookworm',
        name='12 (Bookworm)',
        order=12,
        release_date=date(2023, 6, 10),
        eol_date=date(2026, 7, 11),
        extended_eol_date=date(2028, 6, 30),
    )
    assert bookworm.is_eol(date(2026, 11, 9))
    assert bookworm.is_maintained(date(2026, 11, 9))
    assert bookworm.is_maintained(date(2028, 6, 29))
    assert not bookworm.is_maintained(date(2028, 6, 30))
    assert not bookworm.is_maintained(date(2023, 6, 9))  # not released yet
    trixie = release()
    assert trixie.extended_eol_date is None
    assert trixie.is_maintained(date(2026, 11, 9))
    assert not trixie.is_maintained(date(2028, 8, 9))
    sid = release(version=None, codename='sid', name='Sid', release_date=None, eol_date=None)
    assert not sid.is_maintained(date.max)
    # the later of both dates, whichever it is
    assert release(extended_eol_date=date(2027, 1, 1)).is_maintained(date(2028, 1, 1))


def test_order_within_a_distribution() -> None:
    bookworm = release(version='12', codename='bookworm', name='12 (Bookworm)', order=12)
    trixie = release()
    assert bookworm < trixie
    assert trixie >= bookworm
    assert sorted([trixie, bookworm]) == [bookworm, trixie]


def test_comparing_distributions_raises() -> None:
    with pytest.raises(TypeError):
        _ = release() < release(FEDORA, version='44', codename=None, name='44')


def test_identity_is_uid() -> None:
    trixie = release()
    stable = replace(trixie, suite='stable')
    assert trixie == stable
    assert hash(trixie) == hash(stable)
    assert trixie != release(version='12', codename='bookworm', name='12 (Bookworm)', order=12)


def test_distribution_identity_is_id() -> None:
    assert replace(DEBIAN, source=NoSource()) == DEBIAN
    assert DEBIAN != FEDORA
