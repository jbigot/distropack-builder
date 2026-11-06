"""Characterize distro_releases.distinfo: the releases it builds from recorded data, at frozen dates."""

from collections.abc import Callable
from datetime import date
from types import ModuleType

import pytest


def dump(distinfo: ModuleType) -> dict[str, object]:
    """Everything distinfo exposes about each distribution and release, as plain data."""
    return {
        dist.id: {
            'name': dist.name,
            'id_like': dist.id_like,
            'docker_image': dist.docker_image,
            'releases': [{
                'uid': rel.uid(),
                'str': str(rel),
                'name': rel.name,
                'id': rel.id,
                'codename': rel.codename,
                'cpe': rel.cpe,
                'suite': rel.suite,
                'release_date': rel.release_date.isoformat(),
                'eol_date': rel.eol_date.isoformat(),
                'released': rel.released(),
                'supported': rel.supported(),
                'eoled': rel.eoled(),
            } for rel in dist.releases()],
        } for dist in sorted(distinfo.distributions(), key=lambda d: d.id)
    }


@pytest.mark.parametrize('frozen', [date(2025, 8, 8), date(2025, 8, 9), date(2026, 11, 5)],
                         indirect=True, ids=str)
def test_distributions_snapshot(load_script: Callable[[str], ModuleType], frozen: date,
                                snapshot: object) -> None:
    assert dump(load_script('distinfo.py')) == snapshot


@pytest.mark.parametrize('frozen', [date(2025, 8, 8)], indirect=True, ids=str)
def test_trixie_unreleased_the_day_before(load_script: Callable[[str], ModuleType],
                                          frozen: date) -> None:
    debian = next(load_script('distinfo.py').distributions(id='debian'))
    assert not debian.releases(codename='trixie')[0].released()
    assert debian.releases(codename='bookworm')[0].suite == 'stable'


@pytest.mark.parametrize('frozen', [date(2025, 8, 9)], indirect=True, ids=str)
def test_trixie_released_on_its_release_day(load_script: Callable[[str], ModuleType],
                                            frozen: date) -> None:
    debian = next(load_script('distinfo.py').distributions(id='debian'))
    trixie = debian.releases(codename='trixie')[0]
    assert trixie.released()
    assert trixie.suite == 'stable'


def test_unrecorded_url_fails(recordings: dict[str, str],
                              load_script: Callable[[str], ModuleType]) -> None:
    del recordings['https://endoflife.date/api/v1/products/rocky-linux']
    with pytest.raises(KeyError, match='rocky-linux'):
        load_script('distinfo.py')
