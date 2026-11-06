"""The sources: one HTTP payload turned into rows, retried on server errors."""

from datetime import date

import httpx
import pytest
import respx

from distro_releases._http import new_client
from distro_releases.errors import SourceError
from distro_releases.model import Distribution, Row
from distro_releases.sources import Bodhi, DebianCsv, Endoflife

DEBIAN_CSV = 'https://debian.pages.debian.net/distro-info-data/debian.csv'
DEBIAN = Distribution('debian', 'Debian GNU/Linux', ('debian',), (), None, DebianCsv())
FEDORA = Distribution('fedora', 'Fedora', ('fedora',), (), 'cpe:/o:fedoraproject:fedora', Bodhi())
RHEL = Distribution(
    'rhel',
    'Red Hat Enterprise Linux',
    ('rhel',),
    ('fedora',),
    'cpe:/o:redhat:enterprise_linux',
    Endoflife('rhel'),
)


def rows(distribution: Distribution) -> tuple[Row, ...]:
    with new_client() as client:
        return distribution.source.rows(distribution, client)


def by_codename(rows: tuple[Row, ...], codename: str) -> Row:
    (row,) = (row for row in rows if row.codename == codename)
    return row


def test_debian_csv_rows(recorded: respx.MockRouter) -> None:
    debian = rows(DEBIAN)
    trixie = by_codename(debian, 'trixie')
    assert (trixie.version, trixie.name) == ('13', '13 (Trixie)')
    assert trixie.created == date(2023, 6, 10)
    assert (trixie.release_date, trixie.eol_date) == (date(2025, 8, 9), date(2028, 8, 9))
    assert trixie.cpe is None
    sid = by_codename(debian, 'sid')
    assert (sid.version, sid.name, sid.release_date, sid.eol_date) == (None, 'Sid', None, None)
    forky = by_codename(debian, 'forky')
    assert (forky.release_date, forky.eol_date) == (None, None)
    assert [row.order for row in debian] == list(range(len(debian)))


def test_bodhi_rows(recorded: respx.MockRouter) -> None:
    fedora = {row.version: row for row in rows(FEDORA)}
    assert fedora['44'].cpe == 'cpe:/o:fedoraproject:fedora:44'
    assert fedora['44'].order == 44
    assert [row.suite for row in fedora.values() if row.suite] == ['rawhide']


def test_endoflife_rows(recorded: respx.MockRouter) -> None:
    rhel = {str(row.version): row for row in rows(RHEL)}
    assert sorted(rhel, key=int) == ['4', '5', '6', '7', '8', '9', '10']
    assert (rhel['10'].release_date, rhel['10'].eol_date) == (date(2025, 5, 20), date(2035, 5, 31))
    assert rhel['10'].cpe == 'cpe:/o:redhat:enterprise_linux:10'
    assert (rhel['10'].name, rhel['10'].codename, rhel['10'].order) == ('10', None, 10)


def test_retries_server_errors(recorded: respx.MockRouter) -> None:
    route = recorded.routes[DEBIAN_CSV]
    content = route.return_value.content  # type: ignore[union-attr]
    route.side_effect = [httpx.Response(503), httpx.Response(200, content=content)]
    assert by_codename(rows(DEBIAN), 'trixie').version == '13'
    assert route.call_count == 2


def test_client_error_is_not_retried(recorded: respx.MockRouter) -> None:
    route = recorded.routes[DEBIAN_CSV]
    route.side_effect = [httpx.Response(404)]
    with pytest.raises(SourceError, match='debian.csv') as error:
        rows(DEBIAN)
    assert error.value.url == DEBIAN_CSV
    assert route.call_count == 1


@pytest.mark.parametrize(
    'failure', [httpx.Response(503), httpx.ConnectError('down')], ids=['503', 'connect']
)
def test_retries_exhausted(
    recorded: respx.MockRouter, failure: httpx.Response | httpx.ConnectError
) -> None:
    route = recorded.routes[DEBIAN_CSV]
    if isinstance(failure, httpx.Response):
        route.return_value = failure
    else:
        route.side_effect = failure
    with pytest.raises(SourceError, match='debian.csv'):
        rows(DEBIAN)
    assert route.call_count == 5
