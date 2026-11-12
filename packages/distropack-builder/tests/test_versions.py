# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The versions of rebuilds, snapshots and binNMUs, and the orders they must keep."""

from debian.changelog import Changelog
from debian.debian_support import Version
from hypothesis import given
from hypothesis import strategies as st

from distropack_builder.versions import (
    Snapshot,
    binnmu_entry,
    deb_base_version,
    deb_version,
    next_rebuild,
    rpm_changelog_entry,
    rpm_release,
    rpm_version,
)

SNAP = Snapshot('abcdef0123456789abcdef0123456789abcdef01', '1.12.0', 37)


def test_deb_base_version() -> None:
    assert deb_base_version('1.12.0-1', '13') == '1.12.0-1~bpo13'
    assert deb_base_version('1.12.0-1', '') == '1.12.0-1'
    assert deb_base_version('1.12.0-1', None) == '1.12.0-1'


def test_deb_base_version_of_a_snapshot() -> None:
    """The upstream part comes from the snapshot, the epoch is raised above the changelog's."""
    assert deb_base_version('1.11.1-2', '13', SNAP) == '1:1.12.0+main.37.gabcdef0-2~bpo13'
    assert deb_base_version('2:1.11.1-2', '13', SNAP) == '3:1.12.0+main.37.gabcdef0-2~bpo13'
    assert deb_base_version('2026.01.01', '13', SNAP) == '1:1.12.0+main.37.gabcdef0~bpo13'


def test_deb_version() -> None:
    base = '1.12.0-1~bpo13'
    assert deb_version(base, 0) == base
    assert deb_version(base, 2) == '1.12.0-1~bpo13+b2'
    assert deb_version('1.12.0-1', 1) == '1.12.0-1+b1'


def test_rpm() -> None:
    assert rpm_release('1', 0) == '1'
    assert rpm_release('1', 2) == '1.2'
    assert rpm_version('1.11.1', SNAP) == '1.12.0+main.37.gabcdef0'
    assert rpm_version('1.11.1') == '1.11.1'


def test_next_rebuild() -> None:
    assert next_rebuild('b', None, None) == 0
    assert next_rebuild('b', 'b', 0) == 1
    assert next_rebuild('b', 'b', 4) == 5
    assert next_rebuild('b', 'other', 4) == 0


def ordered(*versions: str) -> bool:
    return all(Version(a) < Version(b) for a, b in zip(versions, versions[1:], strict=False))


def test_orders() -> None:
    b = '1.12.0-1~bpo13'
    assert ordered(b, f'{b}+b1', f'{b}+b9', f'{b}+b10', '1.12.0-2~bpo13', '1.12.1-1~bpo13')
    assert ordered('1.11.1-2~bpo13.pdidev.1274962', b)  # published stable
    assert ordered('2026.01.01~bpo13.pdidev.26411822', '2026.11.04~bpo13')  # published keyring
    snapshot = deb_base_version('1.11.1-2', '13', SNAP)
    assert ordered('1790170994main-1~bpo13.pdidev.1275000', snapshot)
    assert ordered(deb_version(b, 10), snapshot)
    assert ordered('1.12.0', SNAP.upstream(), '1.12.1')
    nine = Snapshot('f' * 40, '1.12.0', 9).upstream()
    ten = Snapshot('a' * 40, '1.12.0', 10).upstream()
    assert ordered(nine, ten)  # the commit count decides, not the sha
    assert ordered(
        Snapshot('a' * 40, '1.12.0', 99).upstream(), Snapshot('a' * 40, '1.12.1', 1).upstream()
    )


def test_binnmu_below_the_old_suffix_on_purpose() -> None:
    """Documented, not to be fixed: why the cut-over needs higher changelog versions."""
    assert ordered('1.11.1-2~bpo13+b1', '1.11.1-2~bpo13.pdidev.1274962')


def test_binnmu_entry() -> None:
    entry = binnmu_entry(
        'pdi',
        '1.12.0-1~bpo13+b2',
        'trixie',
        'PDI <pdi@example.org>',
        'Wed, 04 Nov 2026 10:00:00 +0000',
        'amd64',
    )
    assert entry == (
        'pdi (1.12.0-1~bpo13+b2) trixie; urgency=medium, binary-only=yes\n'
        '\n'
        '  * Binary-only non-maintainer upload for amd64; no source changes.\n'
        '  * Rebuild against updated build dependencies.\n'
        '\n'
        ' -- PDI <pdi@example.org>  Wed, 04 Nov 2026 10:00:00 +0000\n'
    )
    (block,) = Changelog(entry)
    assert str(block.version) == '1.12.0-1~bpo13+b2'


@given(
    base=st.sampled_from(
        ['1.12.0-1~bpo13', '1.12.0-1', '2026.11.04', '1:1.12.0+main.3.gabc-2~bpo13']
    ),
    m=st.integers(min_value=0, max_value=1000),
    n=st.integers(min_value=0, max_value=1000),
)
def test_rebuilds_increase(base: str, m: int, n: int) -> None:
    if m < n:
        assert Version(deb_version(base, m)) < Version(deb_version(base, n))


def test_binnmu_entry_names_its_architecture() -> None:
    entry = binnmu_entry('pdi', '1.0-1+b1', 'sid', 'PDI <pdi@example.org>', 'date', 'arm64')
    assert '  * Binary-only non-maintainer upload for arm64; no source changes.\n' in entry


def test_rpm_changelog_entry() -> None:
    assert rpm_changelog_entry('1.0', '1.2', 'PDI <pdi@example.org>', 'Thu Nov 05 2026') == (
        '* Thu Nov 05 2026 PDI <pdi@example.org> - 1.0-1.2\n'
        '- Rebuilt against updated build dependencies\n'
        '\n'
    )
