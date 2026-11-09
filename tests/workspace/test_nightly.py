# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The releases the end-to-end tests build: one per family, two in the nightly run."""

from support import fixture_releases


def test_one_release_per_distribution_by_default() -> None:
    assert all(len(releases) >= 1 for releases in fixture_releases({}).values())
    assert fixture_releases({})['debian'] == ['trixie']


def test_nightly_builds_two_releases_of_every_distribution() -> None:
    nightly = fixture_releases({'DISTROPACK_NIGHTLY': '1'})
    assert set(nightly) == {'debian', 'ubuntu', 'fedora', 'el', 'opensuse'}
    assert all(len(releases) == 2 for releases in nightly.values())
    # the first release stays the default one, which the install tests use
    assert all(nightly[dist][0] == releases[0] for dist, releases in fixture_releases({}).items())
    assert 'bookworm' in nightly['debian']  # in extended support
