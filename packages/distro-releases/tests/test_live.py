# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The live canary: the real sources still answer, and still parse into sensible releases.

Run nightly by the `Live canary` workflow, never by the default test run (marker `live`).
"""

from datetime import date

import pytest

from distro_releases import Release, distribution

pytestmark = [pytest.mark.live, pytest.mark.enable_socket]


def releases(dist: str) -> tuple[Release, ...]:
    return distribution(dist).releases(date.today())


def suites(dist: str) -> set[str]:
    return {r.suite for r in releases(dist) if r.suite}


def test_debian() -> None:
    assert {'stable', 'testing', 'unstable'} <= suites('debian')


def test_ubuntu() -> None:
    assert [r for r in releases('ubuntu') if 'LTS' in r.name and r.is_supported()]


def test_fedora() -> None:
    assert 'rawhide' in suites('fedora')
    assert [r for r in releases('fedora') if r.is_supported()]


@pytest.mark.parametrize('dist', ['almalinux', 'rhel', 'rocky'])
def test_enterprise_linux(dist: str) -> None:
    assert [r for r in releases(dist) if r.is_supported()]
