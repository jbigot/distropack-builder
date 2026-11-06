"""Fedora's releases, from Bodhi, Fedora's update system."""

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from distro_releases import _http
from distro_releases.model import Distribution, Row
from distro_releases.sources._common import cpe, parsing

URL = (
    'https://bodhi.fedoraproject.org/releases/?exclude_archived=False&rows_per_page=100&page={page}'
)


def _date(value: str | None, happened: bool) -> date | None:
    """A Bodhi date; when it has none, `date.min` if the event happened, else `None`."""
    if value:
        return date.fromisoformat(value[:10])
    return date.min if happened else None


def _rows(data: dict[str, Any], distribution: Distribution) -> list[Row]:
    """The Fedora releases (`F<N>`) of one page of Bodhi's list."""
    result = []
    for release in data['releases']:
        fedora = release['id_prefix'] == 'FEDORA'
        if not fedora or not re.fullmatch(r'F[0-9]+', release['name']):
            continue
        version = release['version']
        result.append(
            Row(
                version=version,
                codename=None,
                name=version,
                order=int(version),
                created=None,
                release_date=_date(
                    release.get('released_on'), release['state'] in ('current', 'archived')
                ),
                eol_date=_date(release.get('eol'), release['state'] == 'archived'),
                cpe=cpe(distribution.cpe_prefix, version),
                suite='rawhide' if release['branch'] == 'rawhide' else None,
            )
        )
    return result


@dataclass(frozen=True, slots=True)
class Bodhi:
    """Bodhi's list of releases, every page of it."""

    def rows(self, distribution: Distribution, client: httpx.Client) -> tuple[Row, ...]:
        """The Fedora releases Bodhi lists, with their states and dates."""
        result = []
        page, pages = 1, 1
        while page <= pages:
            url = URL.format(page=page)
            response = _http.get(client, url)
            with parsing(url):
                data: dict[str, Any] = response.json()
                pages = data['pages']
                result.extend(_rows(data, distribution))
            page += 1
        return tuple(sorted(result, key=lambda row: row.order))
