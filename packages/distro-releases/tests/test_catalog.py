# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The catalog: the six distributions, their releases loaded lazily, as of a date."""

from datetime import date

import httpx
import pytest
import respx

import distro_releases
from distro_releases import SourceError, UnknownDistribution, distribution, distributions

DEBIAN_CSV = 'https://debian.pages.debian.net/distro-info-data/debian.csv'
BODHI = 'https://bodhi.fedoraproject.org/releases/?exclude_archived=False&rows_per_page=100&page=1'
DAY = date(2026, 11, 5)


def test_the_distributions() -> None:
    assert [d.id for d in distributions()] == [
        'almalinux',
        'debian',
        'fedora',
        'rhel',
        'rocky',
        'ubuntu',
    ]
    ubuntu = distribution('ubuntu')
    assert (ubuntu.name, ubuntu.os_release_ids, ubuntu.id_like) == (
        'Ubuntu',
        ('ubuntu',),
        ('debian',),
    )


def test_no_request_at_import_or_listing() -> None:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        assert distro_releases.distributions()
        assert distribution('rhel').name == 'Red Hat Enterprise Linux'
        assert router.calls.call_count == 0


def test_one_request_per_source(recorded: respx.MockRouter) -> None:
    debian = distribution('debian')
    assert debian.releases(DAY) and debian.releases(date(2025, 8, 9))
    assert [str(call.request.url) for call in recorded.calls] == [DEBIAN_CSV]


def test_a_source_down_spares_the_others(recorded: respx.MockRouter) -> None:
    recorded.routes[BODHI].return_value = httpx.Response(503)
    with pytest.raises(SourceError, match='bodhi'):
        distribution('fedora').releases(DAY)
    assert distribution('debian').releases(DAY)


def test_unknown_distribution() -> None:
    with pytest.raises(UnknownDistribution, match='nosuch'):
        distribution('nosuch')


def test_eol_day(recorded: respx.MockRouter) -> None:
    def bookworm(on: date) -> distro_releases.Release:
        (release,) = (r for r in distribution('debian').releases(on) if r.codename == 'bookworm')
        return release

    assert bookworm(date(2026, 7, 10)).is_supported(date(2026, 7, 10))
    assert bookworm(date(2026, 7, 11)).is_eol(date(2026, 7, 11))


def test_rhel(recorded: respx.MockRouter) -> None:
    rhel = {r.version: r for r in distribution('rhel').releases(DAY)}
    assert [v for v, r in rhel.items() if r.is_supported(DAY)] == ['8', '9', '10']
    assert rhel['7'].is_eol(DAY)
    assert str(rhel['10']) == 'Red Hat Enterprise Linux 10'


def test_suites_follow_the_date(recorded: respx.MockRouter) -> None:
    def suites(on: date) -> dict[str, str]:
        return {str(r.codename): r.suite for r in distribution('debian').releases(on) if r.suite}

    assert suites(date(2025, 8, 8))['bookworm'] == 'stable'
    assert suites(date(2025, 8, 8))['trixie'] == 'testing'
    assert suites(date(2025, 8, 9))['trixie'] == 'stable'
    assert suites(date(2025, 8, 9))['forky'] == 'testing'
    assert 'forky' not in suites(date(2025, 8, 8))
