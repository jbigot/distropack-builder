# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The releases a build configuration selects, and the available filter."""

from datetime import date
from pathlib import Path

import pytest
import respx

from distro_releases import Release, SelectorError
from distropack_builder.config import BuildConfig, Distrib, Package
from distropack_builder.selection import configured_releases, disabled_releases


def build_config(**versions: tuple[str, ...]) -> BuildConfig:
    return BuildConfig(
        distribs={dist: Distrib(versions=tokens) for dist, tokens in versions.items()},
        packages={'p': Package(disable={'debian': ('bookworm',)})},
        root=Path('/project'),
    )


def uids(selected: dict[str, frozenset[Release]]) -> dict[str, list[str]]:
    return {dist: sorted(r.uid for r in releases) for dist, releases in selected.items()}


def never(release: Release) -> bool:
    raise AssertionError(f'asked about {release}')


def test_available_filters(recorded_http: respx.MockRouter, frozen: date) -> None:
    asked: list[str] = []

    def is_available(release: Release) -> bool:
        asked.append(release.uid)
        return release.codename != 'bookworm'

    selected = configured_releases(
        build_config(debian=('bookworm', 'trixie', 'available'), ubuntu=('noble',)),
        (),
        frozen,
        is_available,
    )

    assert uids(selected) == {'debian': ['debian-13-trixie'], 'ubuntu': ['ubuntu-24.04-noble']}
    assert sorted(asked) == ['debian-12-bookworm', 'debian-13-trixie']


def test_no_available_no_request(recorded_http: respx.MockRouter, frozen: date) -> None:
    selected = configured_releases(build_config(debian=('trixie',)), (), frozen, never)

    assert uids(selected) == {'debian': ['debian-13-trixie']}


def test_available_after_patterns(recorded_http: respx.MockRouter, frozen: date) -> None:
    """Only the releases the -D patterns keep are asked about."""
    asked: list[str] = []

    def is_available(release: Release) -> bool:
        asked.append(release.uid)
        return True

    configured_releases(
        build_config(debian=('bookworm', 'trixie', 'available'), fedora=('supported', 'available')),
        ('debian:trixie',),
        frozen,
        is_available,
    )

    assert asked == ['debian-13-trixie']


@pytest.mark.parametrize('pattern', ['fedra', 'fedora', 'fedora:44'])
def test_unknown_pattern(recorded_http: respx.MockRouter, frozen: date, pattern: str) -> None:
    with pytest.raises(SelectorError, match=f'-D {pattern} names no distribution of build.conf'):
        configured_releases(build_config(debian=('trixie',)), (pattern,), frozen, never)


@pytest.mark.parametrize('pattern', ['debian:bookwrm', 'debian:bookworm'])
def test_pattern_naming_no_selected_release(
    recorded_http: respx.MockRouter, frozen: date, pattern: str
) -> None:
    """A release build.conf does not select, misspelt or not, is an error, not an empty run."""
    with pytest.raises(SelectorError, match=f'-D {pattern} names no release build.conf selects'):
        configured_releases(
            build_config(debian=('trixie',)), ('debian:trixie', pattern), frozen, never
        )


def test_pattern_release_filtered_as_unavailable(
    recorded_http: respx.MockRouter, frozen: date
) -> None:
    """A release the pattern names but available leaves out is no error: its image is late."""
    selected = configured_releases(
        build_config(debian=('trixie', 'available')), ('debian:trixie',), frozen, lambda r: False
    )
    assert uids(selected) == {'debian': []}


def test_pattern_by_name(recorded_http: respx.MockRouter, frozen: date) -> None:
    selected = configured_releases(
        build_config(debian=('trixie',)), ('Debian GNU/Linux',), frozen, never
    )

    assert uids(selected) == {'debian': ['debian-13-trixie']}


def test_disabled(recorded_http: respx.MockRouter, frozen: date) -> None:
    config = build_config(debian=('bookworm', 'trixie'))
    selected = configured_releases(config, (), frozen, never)

    assert uids(disabled_releases(config, selected, frozen)) == {'p': ['debian-12-bookworm']}
