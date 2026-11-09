# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The versions of rebuilt packages: from their inputs alone, never from the clock.

A package built for a release gets its base version: the packaging's own, made release-specific
for the Debian family (`~bpo<release version>`, as the official backports), and taken from an
upstream snapshot for a snapshot channel. Rebuilding the same base, its build dependencies
having changed, adds a rebuild number (`+b<n>` for deb, as a binNMU, `.<n>` in the rpm release).
"""

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

    def upstream(self) -> str:
        """The upstream version: above base, below its next release, ordered by distance."""
        return f'{self.base}+main.{self.distance}.g{self.commit[:7]}'


def deb_base_version(
    changelog_version: str, release_id: str | None, snap: Snapshot | None = None
) -> str:
    """The base version of a deb built for a release, from the version of its changelog.

    For a release with a version, `~bpo<version>` sorts the package before that release's own
    next version. A snapshot replaces the upstream part with its own and raises the epoch, so that
    it sorts above every release of the changelog's upstream.
    """
    version = Version(changelog_version)
    if snap is not None:
        epoch = int(version.epoch or 0) + 1
        revision = f'-{version.debian_revision}' if version.debian_revision else ''
        version = Version(f'{epoch}:{snap.upstream()}{revision}')
    result = str(version)
    return f'{result}~bpo{release_id}' if release_id else result


def deb_version(base_version: str, rebuild: int) -> str:
    """The version of the rebuild-th rebuild of base_version, a binNMU's `+b<n>` (none for 0)."""
    return base_version if rebuild == 0 else f'{base_version}+b{rebuild}'


def rpm_version(spec_version: str, snap: Snapshot | None = None) -> str:
    """The Version: of an rpm, the spec's or a snapshot's upstream version."""
    return spec_version if snap is None else snap.upstream()


def rpm_release(spec_release: str, rebuild: int) -> str:
    """The Release: of the rebuild-th rebuild of an rpm, `.<n>` after the spec's (none for 0)."""
    return spec_release if rebuild == 0 else f'{spec_release}.{rebuild}'


def next_rebuild(base: str, stored_base: str | None, stored_rebuild: int | None) -> int:
    """The rebuild number of a new build of base: one more than what is stored, if it is base."""
    if stored_base != base or stored_rebuild is None:
        return 0
    return stored_rebuild + 1


def binnmu_entry(source: str, version: str, distribution: str, author: str, date: str) -> str:
    """The debian/changelog stanza of a binNMU, to prepend to the stored source's changelog."""
    return (
        f'{source} ({version}) {distribution}; urgency=medium, binary-only=yes\n'
        '\n'
        '  * Binary-only non-maintainer upload for amd64; no source changes.\n'
        '  * Rebuild against updated build dependencies.\n'
        '\n'
        f' -- {author}  {date}\n'
    )
