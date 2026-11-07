"""Characterize pkgbuild's release selectors, release filters and deb build order."""

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
import respx

from distro_releases import (
    UnknownDistribution,
    UnknownRelease,
    distribution,
    distributions,
    matches,
    select,
)
from distropack_builder.context import BuildContext
from distropack_builder.docker import Cancellation
from distropack_builder.graph import dependencies
from fake_runner import FakeRunner
from support import FROZEN_DATE, REPO_ROOT

RELEASE_LISTS = [
    {'debian': 'all'},
    {'debian': ['supported']},
    {'debian': ['bookworm', '+']},
    {'debian': ['trixie', '-']},
    {'debian': ['stable']},
    {'debian': ['13']},
    {'debian': ['sid']},
    {'ubuntu': ['supported']},
    {'fedora': ['supported']},
    {'fedora': ['rawhide']},
    {'rocky': ['supported']},
    {'almalinux': ['9', '+']},
]
FILTERS = [
    'debian',
    'Debian GNU/Linux',
    'debian:trixie',
    'debian:13',
    'debian:stable',
    'ubuntu:noble',
    'fedora:44',
    'Fedora:rawhide',
    'rocky:10',
    'nosuch',
]


def cfg_id(cfg: dict[str, object]) -> str:
    """A test id, and snapshot file name: 'debian-bookworm-+' for {'debian': ['bookworm', '+']}."""
    ((dist, versions),) = cfg.items()
    return '-'.join(
        [dist, *([versions] if isinstance(versions, str) else cast(list[str], versions))]
    )


def filter_id(flt: str) -> str:
    """A test id, and snapshot file name, for a filter: 'debian:trixie' gives 'debian-trixie'."""
    return flt.replace(':', '-').replace('/', '').replace(' ', '_')


@pytest.mark.parametrize('cfg', RELEASE_LISTS, ids=cfg_id)
def test_release_list(
    load_script: Callable[[str], ModuleType], frozen: date, cfg: dict[str, object], snapshot: object
) -> None:
    assert sorted(r.uid for r in load_script('pkgbuild').release_list(cfg)) == snapshot


@pytest.mark.parametrize('cfg', [{'debian': ['+']}, {'debian': ['-']}], ids=cfg_id)
def test_release_list_misplaced_sign(
    load_script: Callable[[str], ModuleType], frozen: date, cfg: dict[str, object]
) -> None:
    with pytest.raises(Exception, match=r"'[+-]' must follow a release in debian versions"):
        load_script('pkgbuild').release_list(cfg)


def test_release_list_unknown_release(
    load_script: Callable[[str], ModuleType], frozen: date
) -> None:
    # Intended difference: master selected nothing, silently.
    with pytest.raises(UnknownRelease, match='nosuch'):
        load_script('pkgbuild').release_list({'debian': ['nosuch']})


def test_release_list_unknown_distribution(
    load_script: Callable[[str], ModuleType], frozen: date
) -> None:
    # Intended difference: master failed with the bare StopIteration of next(distributions(id=...)).
    with pytest.raises(UnknownDistribution, match='nosuch'):
        load_script('pkgbuild').release_list({'nosuch': ['1']})


@pytest.mark.parametrize('flt', FILTERS, ids=filter_id)
def test_release_filter(
    load_script: Callable[[str], ModuleType], frozen: date, flt: str, snapshot: object
) -> None:
    pkgbuild = load_script('pkgbuild')
    releases = {r for d in pkgbuild.targets.targets() for r in d.releases()}
    assert sorted(r.uid for r in pkgbuild.release_filter(releases, [flt])) == snapshot


def test_deb_build_order(
    load_script: Callable[[str], ModuleType], frozen: date, tmp_path: Path
) -> None:
    pkgbuild = load_script('pkgbuild')
    (trixie,) = (r for r in distribution('debian').releases() if r.codename == 'trixie')
    context = BuildContext(cast(Any, None), FakeRunner(), Cancellation(), tmp_path, 1)
    lib, app = (
        pkgbuild.DebPackageBuilder(
            context, trixie, REPO_ROOT / 'tests' / 'e2e' / 'project', None, name
        )
        for name in ('hello-lib', 'hello-app')
    )
    assert dependencies([lib, app]) == {lib: frozenset(), app: frozenset({lib})}


SNAPSHOTS = Path(__file__).parent / '__snapshots__' / 'test_selectors'


@pytest.mark.parametrize('cfg', RELEASE_LISTS, ids=cfg_id)
def test_select_equals_the_builder(recorded_http: respx.MockRouter, cfg: dict[str, object]) -> None:
    """distro_releases.select gives the releases the builder's release_list gave."""
    ((dist, tokens),) = cfg.items()
    expected = json.loads((SNAPSHOTS / f'test_release_list[{cfg_id(cfg)}].json').read_text())
    selected = select(distribution(dist), cast(str | list[str], tokens), FROZEN_DATE)
    assert sorted(r.uid for r in selected) == expected


@pytest.mark.parametrize('flt', FILTERS, ids=filter_id)
def test_matches_equals_the_builder(recorded_http: respx.MockRouter, flt: str) -> None:
    """distro_releases.matches keeps what the builder's release_filter kept (rhel is new)."""
    expected = json.loads((SNAPSHOTS / f'test_release_filter[{filter_id(flt)}].json').read_text())
    kept = [
        r.uid
        for d in distributions()
        if d.id != 'rhel'
        for r in d.releases(FROZEN_DATE)
        if matches(r, flt)
    ]
    assert sorted(kept) == expected
