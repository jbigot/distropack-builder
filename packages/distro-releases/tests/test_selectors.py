# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Selectors: build.conf's release tokens, and the distribution:release patterns."""

from datetime import date

import pytest
import respx

from distro_releases import SelectorError, UnknownRelease, distribution, matches, select

DAY = date(2026, 11, 5)


def uids(tokens: str | list[str], dist: str = 'debian', on: date = DAY) -> list[str]:
    return sorted(r.uid for r in select(distribution(dist), tokens, on))


def test_supported(recorded: respx.MockRouter) -> None:
    assert uids(['supported']) == ['debian-13-trixie']


def test_codename_suite_and_version(recorded: respx.MockRouter) -> None:
    assert uids(['trixie']) == uids(['stable']) == uids(['13']) == ['debian-13-trixie']
    assert uids(['rawhide'], 'fedora') == ['fedora-46']


def test_after_and_before(recorded: respx.MockRouter) -> None:
    assert uids(['bookworm', '+']) == [
        'debian--rc-buggy',
        'debian--sid',
        'debian-12-bookworm',
        'debian-13-trixie',
        'debian-14-forky',
    ]
    assert uids(['bullseye', '-'])[-1] == 'debian-9-stretch'
    assert 'debian-12-bookworm' not in uids(['bullseye', '-'])


def test_all(recorded: respx.MockRouter) -> None:
    assert (
        uids('all') == uids(['all']) == sorted(r.uid for r in distribution('debian').releases(DAY))
    )


@pytest.mark.parametrize('sign', ['+', '-'])
def test_misplaced_sign(recorded: respx.MockRouter, sign: str) -> None:
    with pytest.raises(SelectorError, match=f"'\\{sign}' must follow a release in debian versions"):
        uids([sign])


def test_unknown_token(recorded: respx.MockRouter) -> None:
    with pytest.raises(UnknownRelease, match='trixy') as error:
        uids(['trixy'])
    assert (error.value.distribution, error.value.token) == ('debian', 'trixy')


def test_known_suite_empty(recorded: respx.MockRouter) -> None:
    assert uids(['devel'], 'ubuntu', date(2026, 4, 23)) == []
    assert uids(['devel'], 'ubuntu', date(2026, 4, 24)) == ['ubuntu-26.10-stonking']


@pytest.mark.parametrize(
    ('pattern', 'expected'),
    [
        ('debian', True),
        ('Debian GNU/Linux', True),
        ('debian:trixie', True),
        ('debian:13', True),
        ('debian:stable', True),
        ('debian:13 (Trixie)', True),
        ('Debian GNU/Linux:trixie', True),
        ('debian:bookworm', False),
        ('ubuntu', False),
        ('ubuntu:trixie', False),
        ('debian:', False),
        ('debian:None', False),
    ],
)
def test_matches(recorded: respx.MockRouter, pattern: str, expected: bool) -> None:
    (trixie,) = (r for r in distribution('debian').releases(DAY) if r.codename == 'trixie')
    assert matches(trixie, pattern) is expected
