"""Characterize pkgbuild's release selectors, release filters and deb build order."""

from collections.abc import Callable
from datetime import date
from types import ModuleType

import pytest

from conftest import REPO_ROOT

RELEASE_LISTS = [
    {'debian': 'all'}, {'debian': ['supported']}, {'debian': ['bookworm', '+']},
    {'debian': ['trixie', '-']}, {'debian': ['stable']}, {'debian': ['13']}, {'debian': ['sid']},
    {'ubuntu': ['supported']}, {'fedora': ['supported']}, {'fedora': ['rawhide']},
    {'rocky': ['supported']}, {'almalinux': ['9', '+']},
]
FILTERS = ['debian', 'Debian GNU/Linux', 'debian:trixie', 'debian:13', 'debian:stable',
           'ubuntu:noble', 'fedora:44', 'Fedora:rawhide', 'rocky:10', 'nosuch']


def cfg_id(cfg: dict[str, object]) -> str:
    """A test id, and snapshot file name: 'debian-bookworm-+' for {'debian': ['bookworm', '+']}."""
    (dist, versions), = cfg.items()
    return '-'.join([dist, *([versions] if isinstance(versions, str) else versions)])


def filter_id(flt: str) -> str:
    """A test id, and snapshot file name, for a filter: 'debian:trixie' gives 'debian-trixie'."""
    return flt.replace(':', '-').replace('/', '').replace(' ', '_')


@pytest.mark.parametrize('cfg', RELEASE_LISTS, ids=cfg_id)
def test_release_list(load_script: Callable[[str], ModuleType], frozen: date,
                      cfg: dict[str, object], snapshot: object) -> None:
    assert sorted(r.uid() for r in load_script('pkgbuild').release_list(cfg)) == snapshot


@pytest.mark.parametrize('cfg', [{'debian': ['+']}, {'debian': ['-']}], ids=cfg_id)
def test_release_list_misplaced_sign(load_script: Callable[[str], ModuleType], frozen: date,
                                     cfg: dict[str, object]) -> None:
    with pytest.raises(Exception, match=r"'[+-]' must follow a release in debian versions"):
        load_script('pkgbuild').release_list(cfg)


@pytest.mark.parametrize('flt', FILTERS, ids=filter_id)
def test_release_filter(load_script: Callable[[str], ModuleType], frozen: date, flt: str,
                        snapshot: object) -> None:
    pkgbuild = load_script('pkgbuild')
    releases = {r for d in pkgbuild.distributions() for r in d.releases()}
    assert sorted(r.uid() for r in pkgbuild.release_filter(releases, [flt])) == snapshot


def test_deb_build_order(load_script: Callable[[str], ModuleType], frozen: date) -> None:
    pkgbuild = load_script('pkgbuild')
    trixie = next(pkgbuild.distributions(id='debian')).releases(codename='trixie')[0]
    lib, app = (pkgbuild.DebPackageBuilder(trixie, REPO_ROOT / 'tests' / 'e2e' / 'project', None,
                                           name, None, None, 1)
                for name in ('hello-lib', 'hello-app'))
    pkgbuild.order_packages([lib, app])
    assert app._depends == {lib}
    assert lib._depends == set()
