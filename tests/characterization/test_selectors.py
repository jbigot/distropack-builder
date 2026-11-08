# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Characterize pkgbuild's release selectors, release filters and deb build order."""

import importlib
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
import respx
from pydantic import ValidationError

from distro_releases import (
    Release,
    SelectorError,
    UnknownRelease,
    distribution,
    distributions,
    matches,
    select,
)
from distropack_builder import targets
from distropack_builder.config import BuildConfig, Distrib
from distropack_builder.context import BuildContext
from distropack_builder.docker import Cancellation
from distropack_builder.graph import dependencies
from distropack_builder.selection import configured_releases, matching
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


def release_list(cfg: dict[str, object], on: date) -> frozenset[Release]:
    """The releases a {distribution id: tokens} mapping names, as build.conf's versions."""
    config = BuildConfig(
        distribs={
            dist: Distrib.model_validate({'versions': tokens}) for dist, tokens in cfg.items()
        },
        packages={},
        root=Path('/project'),
    )
    selected = configured_releases(config, (), on, lambda release: True)
    return frozenset(release for releases in selected.values() for release in releases)


@pytest.mark.parametrize('cfg', RELEASE_LISTS, ids=cfg_id)
def test_release_list(
    recorded_http: respx.MockRouter, frozen: date, cfg: dict[str, object], snapshot: object
) -> None:
    assert sorted(r.uid for r in release_list(cfg, frozen)) == snapshot


@pytest.mark.parametrize('cfg', [{'debian': ['+']}, {'debian': ['-']}], ids=cfg_id)
def test_release_list_misplaced_sign(
    recorded_http: respx.MockRouter, frozen: date, cfg: dict[str, object]
) -> None:
    with pytest.raises(SelectorError, match=r"'[+-]' must follow a release in debian versions"):
        release_list(cfg, frozen)


def test_release_list_unknown_release(recorded_http: respx.MockRouter, frozen: date) -> None:
    # Intended difference: master selected nothing, silently.
    with pytest.raises(UnknownRelease, match='nosuch'):
        release_list({'debian': ['nosuch']}, frozen)


def test_release_list_unknown_distribution(recorded_http: respx.MockRouter, frozen: date) -> None:
    # Intended difference: master failed with the bare StopIteration of next(distributions(id=...)).
    # The configuration refuses it now, before any release is selected.
    with pytest.raises(ValidationError, match='unknown distribution nosuch'):
        release_list({'nosuch': ['1']}, frozen)


@pytest.mark.parametrize('flt', FILTERS, ids=filter_id)
def test_release_filter(
    recorded_http: respx.MockRouter, frozen: date, flt: str, snapshot: object
) -> None:
    releases = {r for d in targets.targets() for r in d.releases()}
    assert sorted(r.uid for r in matching(releases, [flt])) == snapshot


def test_deb_build_order(
    load_script: Callable[[str], ModuleType], frozen: date, tmp_path: Path
) -> None:
    load_script('pkgbuild')
    builders = importlib.import_module('distropack_builder.builders')
    (trixie,) = (r for r in distribution('debian').releases() if r.codename == 'trixie')
    context = BuildContext(cast(Any, None), FakeRunner(), Cancellation(), tmp_path, 1)
    lib, app = (
        builders.DebPackageBuilder(
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
