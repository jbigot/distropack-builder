# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The versions of rebuilt packages: from their inputs alone, never from the clock.

A package built for a release gets its base version: the packaging's own, made release-specific
for the Debian family (`~bpo<release version>`, as the official backports), and taken from an
upstream snapshot for a snapshot channel. Rebuilding the same base, its build dependencies
having changed, adds a rebuild number (`+b<n>` for deb, as a binNMU, `.<n>` in the rpm release).
"""

import re
from dataclasses import dataclass

from debian.debian_support import Version


@dataclass(frozen=True)
class Snapshot:
    """An upstream snapshot: a commit, the highest version tag below it, how many commits after.

    Attributes:
        commit: the commit's full hash.
        base: the version its highest reachable version tag names.
        distance: how many commits it is after that tag.
    """

    commit: str
    base: str
    distance: int

    def upstream(self, label: str) -> str:
        """The upstream version: above base, below its next release, ordered by distance.

        The label (`version_label` of the ref, or the channel's) tells snapshots of different
        branches apart.
        """
        return f'{self.base}+{label}.{self.distance}.g{self.commit[:7]}'


def version_label(ref: str) -> str:
    """The label of a ref in a snapshot version: every character outside `[A-Za-z0-9]` a `.`."""
    return re.sub('[^A-Za-z0-9]', '.', ref)


def tag_version(tag: str) -> str:
    """The version a release tag names: a leading `v` dropped, `-` made `~` (a pre-release)."""
    return tag.removeprefix('v').replace('-', '~')


def deb_base_version(
    changelog_version: str,
    release_id: str | None,
    upstream: str | None = None,
    epoch_bump: int = 0,
) -> str:
    """The base version of a deb built for a release, from the version of its changelog.

    For a release with a version, `~bpo<version>` sorts the package before that release's own
    next version. A snapshot's upstream version replaces the changelog's upstream part, and the
    channel's epoch_bump raises the epoch, so that the snapshot can sort above every release of the
    changelog's upstream.
    """
    version = Version(changelog_version)
    if upstream is not None:
        epoch = int(version.epoch or 0) + epoch_bump
        prefix = f'{epoch}:' if epoch else ''
        revision = f'-{version.debian_revision}' if version.debian_revision else ''
        version = Version(f'{prefix}{upstream}{revision}')
    result = str(version)
    return f'{result}~bpo{release_id}' if release_id else result


def deb_version(base_version: str, rebuild: int) -> str:
    """The version of the rebuild-th rebuild of base_version, a binNMU's `+b<n>` (none for 0)."""
    return base_version if rebuild == 0 else f'{base_version}+b{rebuild}'


def rpm_version(spec_version: str, upstream: str | None = None) -> str:
    """The Version: of an rpm, the spec's or a snapshot's upstream version."""
    return spec_version if upstream is None else upstream


def rpm_release(spec_release: str, rebuild: int) -> str:
    """The Release: of the rebuild-th rebuild of an rpm, `.<n>` after the spec's (none for 0)."""
    return spec_release if rebuild == 0 else f'{spec_release}.{rebuild}'


def next_rebuild(base: str, stored_base: str | None, stored_rebuild: int | None) -> int:
    """The rebuild number of a new build of base: one more than what is stored, if it is base."""
    if stored_base != base or stored_rebuild is None:
        return 0
    return stored_rebuild + 1


def binnmu_entry(
    source: str, version: str, distribution: str, author: str, date: str, architecture: str
) -> str:
    """The debian/changelog stanza of a binNMU, to prepend to the stored source's changelog."""
    return (
        f'{source} ({version}) {distribution}; urgency=medium, binary-only=yes\n'
        '\n'
        f'  * Binary-only non-maintainer upload for {architecture}; no source changes.\n'
        '  * Rebuild against updated build dependencies.\n'
        '\n'
        f' -- {author}  {date}\n'
    )


def rpm_changelog_entry(version: str, release: str, author: str, date: str) -> str:
    """The %changelog entry of an rpm rebuild, as Fedora's mass rebuilds write theirs."""
    return (
        f'* {date} {author} - {version}-{release}\n- Rebuilt against updated build dependencies\n\n'
    )


def _is_alnum(char: str) -> bool:
    return char.isascii() and char.isalnum()


def rpmvercmp(a: str, b: str) -> int:
    """-1, 0 or 1 as a sorts before, as or after b in rpm's order (rpm's rpmvercmp).

    Alphanumeric segments are compared, numbers as numbers, the rest separates them; `~` sorts
    before anything, even the end, and `^` after the end but before anything else.
    """
    if a == b:
        return 0
    i = j = 0
    while i < len(a) or j < len(b):
        while i < len(a) and not _is_alnum(a[i]) and a[i] not in '~^':
            i += 1
        while j < len(b) and not _is_alnum(b[j]) and b[j] not in '~^':
            j += 1
        one, two = a[i : i + 1], b[j : j + 1]
        if '~' in (one, two):
            if one != '~':
                return 1
            if two != '~':
                return -1
            i, j = i + 1, j + 1
            continue
        if '^' in (one, two):
            if not one:
                return -1
            if not two:
                return 1
            if one != '^':
                return 1
            if two != '^':
                return -1
            i, j = i + 1, j + 1
            continue
        if not (one and two):
            break
        numeric = one.isdigit()
        kind = str.isdigit if numeric else (lambda c: c.isascii() and c.isalpha())
        end_a, end_b = i, j
        while end_a < len(a) and kind(a[end_a]):
            end_a += 1
        while end_b < len(b) and kind(b[end_b]):
            end_b += 1
        first, second = a[i:end_a], b[j:end_b]
        if not second:
            return 1 if numeric else -1
        if numeric:
            first, second = first.lstrip('0'), second.lstrip('0')
            if len(first) != len(second):
                return 1 if len(first) > len(second) else -1
        if first != second:
            return 1 if first > second else -1
        i, j = end_a, end_b
    if i >= len(a) and j >= len(b):
        return 0
    return -1 if i >= len(a) else 1


def rpm_compare(a: str, b: str) -> int:
    """-1, 0 or 1 as the `<Version>-<Release>` a sorts before, as or after b, as rpm sorts them."""
    version_a, _, release_a = a.rpartition('-')
    version_b, _, release_b = b.rpartition('-')
    return rpmvercmp(version_a, version_b) or rpmvercmp(release_a, release_b)
