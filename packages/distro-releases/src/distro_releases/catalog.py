"""The distributions this library knows, and their releases as of a date.

Each distribution's source is fetched the first time its releases are asked for, then kept for the
rest of the process: importing the package or listing the distributions reaches no network.
"""

import functools
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import date

from distro_releases import _http
from distro_releases.errors import UnknownDistribution
from distro_releases.model import Distribution, Release, Row
from distro_releases.sources import Bodhi, DebianCsv, Endoflife

RHEL_LIKE = ('rhel', 'centos', 'fedora')

# What no source carries: the identity of each distribution.
_DISTRIBUTIONS = {
    d.id: d
    for d in (
        Distribution(
            'almalinux',
            'AlmaLinux OS',
            ('almalinux',),
            RHEL_LIKE,
            'cpe:/o:almalinux:almalinux',
            Endoflife('almalinux'),
        ),
        Distribution('debian', 'Debian GNU/Linux', ('debian',), (), None, DebianCsv()),
        Distribution('fedora', 'Fedora', ('fedora',), (), 'cpe:/o:fedoraproject:fedora', Bodhi()),
        Distribution(
            'rhel',
            'Red Hat Enterprise Linux',
            ('rhel',),
            ('fedora',),
            'cpe:/o:redhat:enterprise_linux',
            Endoflife('rhel'),
        ),
        Distribution(
            'rocky',
            'Rocky Linux',
            ('rocky',),
            RHEL_LIKE,
            'cpe:/o:rocky:rocky',
            Endoflife('rocky-linux'),
        ),
        Distribution('ubuntu', 'Ubuntu', ('ubuntu',), ('debian',), None, DebianCsv()),
    )
}

_lock = threading.Lock()


@functools.cache
def _fetch(distribution: Distribution) -> tuple[Row, ...]:
    """The rows of distribution's source, fetched once per process."""
    with _http.new_client() as client:
        return distribution.source.rows(distribution, client)


def _rows(distribution: Distribution) -> tuple[Row, ...]:
    """_fetch, one thread at a time, so that two threads do not fetch the same source."""
    with _lock:
        return _fetch(distribution)


def distributions() -> tuple[Distribution, ...]:
    """Every distribution, sorted by id."""
    return tuple(_DISTRIBUTIONS[id] for id in sorted(_DISTRIBUTIONS))


def distribution(id: str) -> Distribution:
    """The distribution of that id.

    Raises:
        UnknownDistribution: no distribution has that id.
    """
    try:
        return _DISTRIBUTIONS[id]
    except KeyError:
        raise UnknownDistribution(id, sorted(_DISTRIBUTIONS)) from None


def _debian_suites(releases: list[Release], on: date) -> dict[str, str]:
    """The archive's aliases: the three latest released, the next one, sid and rc-buggy.

    The archive keeps stable, oldstable and oldoldstable as aliases of the three latest releases,
    whether they are still under regular security support or only under LTS.
    """
    released = [r for r in releases if r.is_released(on)]
    latest = [r.uid for r in reversed(released)]
    # Fewer than three released releases leave the older aliases unassigned.
    suites = dict(zip(latest, ('stable', 'oldstable', 'oldoldstable'), strict=False))
    unreleased = [r for r in releases if not r.is_released(on)]
    if unreleased:
        suites[unreleased[0].uid] = 'testing'
    for release in releases:
        if release.codename == 'sid':
            suites[release.uid] = 'unstable'
        elif release.codename == 'rc-buggy':
            suites[release.uid] = 'experimental'
    return suites


def _ubuntu_suites(releases: list[Release], on: date) -> dict[str, str]:
    """The release in development, if any, is devel."""
    unreleased = [r for r in releases if not r.is_released(on)]
    return {unreleased[0].uid: 'devel'} if unreleased else {}


_SUITES: dict[str, Callable[[list[Release], date], dict[str, str]]] = {
    'debian': _debian_suites,
    'ubuntu': _ubuntu_suites,
}


def releases(distribution: Distribution, on: date | None = None) -> tuple[Release, ...]:
    """The releases of distribution as of on, today by default, oldest first.

    A release created after on does not exist yet; the suites are the ones of that day.
    """
    on = on or date.today()
    result = [
        Release(
            distribution=distribution,
            version=row.version,
            codename=row.codename,
            name=row.name,
            order=row.order,
            suite=row.suite,
            created=row.created,
            release_date=row.release_date,
            eol_date=row.eol_date,
            cpe=row.cpe,
        )
        for row in _rows(distribution)
        if row.created is None or row.created <= on
    ]
    result.sort()
    if distribution.id in _SUITES:
        suites = _SUITES[distribution.id](result, on)
        result = [replace(r, suite=suites.get(r.uid, r.suite)) for r in result]
    return tuple(result)
