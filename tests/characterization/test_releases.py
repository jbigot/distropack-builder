# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The release data of distro-releases, compared with what distinfo gave on the same dates."""

import json
from datetime import date

import pytest
import respx

from distro_releases import Distribution, distribution, distributions
from support import DATA

DATES = [date(2025, 8, 8), date(2025, 8, 9), date(2026, 11, 5)]


def dump(on: date) -> dict[str, object]:
    """The releases as of on, in the format of distinfo's snapshots, docker_image aside."""

    def iso(day: date | None) -> str:
        return (day or date.max).isoformat()

    def one(dist: Distribution) -> dict[str, object]:
        return {
            'name': dist.name,
            'id_like': [dist.id, *dist.id_like],
            'releases': [
                {
                    'uid': rel.uid,
                    'str': str(rel),
                    'name': rel.name,
                    'id': rel.version or '',
                    'codename': rel.codename,
                    'cpe': rel.cpe,
                    'suite': rel.suite,
                    'release_date': iso(rel.release_date),
                    'eol_date': iso(rel.eol_date),
                    'released': rel.is_released(on),
                    'supported': rel.is_supported(on),
                    'eoled': rel.is_eol(on),
                }
                for rel in dist.releases(on)
            ],
        }

    return {dist.id: one(dist) for dist in distributions()}


@pytest.mark.parametrize('on', DATES, ids=str)
def test_distinfo_reproduced(recorded_http: respx.MockRouter, on: date) -> None:
    """Equal to distinfo's snapshot but for the intended differences.

    - docker_image is the builder's (distropack_builder.targets);
    - rhel is new.
    """
    expected = json.loads((DATA / f'distinfo-{on}.json').read_text())
    for dist in expected.values():
        del dist['docker_image']
    generated = dump(on)
    del generated['rhel']
    assert generated == expected


def releases_named(dist: str, codename: str, on: date) -> list[str]:
    return [r.uid for r in distribution(dist).releases(on) if r.codename == codename]


def test_trixie_unreleased_the_day_before(recorded_http: respx.MockRouter) -> None:
    on = date(2025, 8, 8)
    debian = {r.codename: r for r in distribution('debian').releases(on)}
    assert not debian['trixie'].is_released(on)
    assert debian['bookworm'].suite == 'stable'


def test_trixie_released_on_its_release_day(recorded_http: respx.MockRouter) -> None:
    on = date(2025, 8, 9)
    trixie = {r.codename: r for r in distribution('debian').releases(on)}['trixie']
    assert trixie.is_released(on)
    assert trixie.suite == 'stable'


def test_release_not_created_yet_is_left_out(recorded_http: respx.MockRouter) -> None:
    on = date(2026, 11, 5)
    assert releases_named('debian', 'duke', on) == []
    forky = {r.codename: r for r in distribution('debian').releases(on)}['forky']
    assert forky.suite == 'testing'


@pytest.mark.parametrize(
    ('on', 'present'), [(date(2025, 8, 8), False), (date(2025, 8, 9), True)], ids=str
)
def test_release_present_from_its_creation_day(
    recorded_http: respx.MockRouter, on: date, present: bool
) -> None:
    assert bool(releases_named('debian', 'forky', on)) is present
