# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Distributions and their releases, as immutable values.

The status of a release is a function of a date, `on`, which is today when it is not given: nothing
is computed when the release is built.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from functools import total_ordering
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import httpx


class Source(Protocol):
    """Where the releases of a distribution come from."""

    def rows(self, distribution: Distribution, client: httpx.Client) -> tuple[Row, ...]:
        """The releases of distribution, as the source publishes them.

        Args:
            distribution: the distribution whose releases are fetched.
            client: the HTTP client to fetch them with.

        Returns:
            One row per release, in the source's order, oldest first.
        """
        ...


@dataclass(frozen=True, slots=True)
class Distribution:
    """A Linux distribution.

    Two distributions are equal when their ids are.

    Attributes:
        id: the distribution's identifier in this library, its os-release `ID`.
        name: its display name, os-release's `NAME`.
        os_release_ids: the os-release `ID` values of the systems it covers.
        id_like: its os-release `ID_LIKE`, which does not contain the distribution itself.
        cpe_prefix: the CPE of the distribution, to which a release appends its version.
        source: where its releases come from.
    """

    id: str
    name: str = field(compare=False)
    os_release_ids: tuple[str, ...] = field(compare=False)
    id_like: tuple[str, ...] = field(compare=False)
    cpe_prefix: str | None = field(compare=False)
    source: Source = field(compare=False, repr=False)

    def releases(self, on: date | None = None) -> tuple[Release, ...]:
        """The releases of the distribution as of on, today by default, oldest first.

        The source is fetched the first time, and kept for the rest of the process. A release
        created after on does not exist yet; the suites are the ones of that day.

        Raises:
            SourceError: the source could not be read.
        """
        from distro_releases import catalog  # catalog builds on this module

        return catalog.releases(self, on)

    def __str__(self) -> str:
        """The display name."""
        return self.name


@dataclass(frozen=True, slots=True)
class Row:
    """A release as its source publishes it, before the date-dependent parts are added.

    Attributes:
        version: the release's version number, `None` for a release without one (Debian's sid).
        codename: the release's codename, as used in the archive's paths, if it has one.
        name: the release's display name within its distribution, `13 (Trixie)` or `44`.
        order: the release's rank within its distribution, increasing with time.
        created: the day the release appeared, if the source says.
        release_date: the day of its release, `None` when not scheduled.
        eol_date: the first day of its end of life, `None` when not scheduled.
        cpe: the release's CPE, if its distribution has one.
        suite: the archive's alias of the release when the source states it (Fedora's `rawhide`),
            whatever the date; the aliases that move with time are the catalog's.
    """

    version: str | None
    codename: str | None
    name: str
    order: int
    created: date | None
    release_date: date | None
    eol_date: date | None
    cpe: str | None
    suite: str | None = None


@total_ordering
@dataclass(frozen=True, slots=True, eq=False)
class Release:
    """A release of a distribution.

    Releases are identified by their `uid`: the same release built as of two dates is equal to
    itself even when its suite differs. Releases of one distribution are ordered in time; comparing
    releases of two distributions raises `TypeError`.

    Attributes:
        distribution: the distribution the release belongs to.
        version: the release's version number, `None` for a release without one (Debian's sid).
        codename: the release's codename, as used in the archive's paths, if it has one.
        name: the release's display name within its distribution, `13 (Trixie)` or `44`.
        order: the release's rank within its distribution, increasing with time.
        suite: the archive's alias of the release (`stable`, `testing`, `rawhide`), if it has one
            on the date the release was built for.
        created: the day the release appeared, if its source says.
        release_date: the day of its release, `None` when not scheduled.
        eol_date: the first day of its end of life, `None` when not scheduled.
        cpe: the release's CPE, if its distribution has one.
    """

    distribution: Distribution
    version: str | None
    codename: str | None
    name: str
    order: int
    suite: str | None
    created: date | None
    release_date: date | None
    eol_date: date | None
    cpe: str | None

    @property
    def uid(self) -> str:
        """A string identifying the release among those of every distribution."""
        uid = f'{self.distribution.id}-{self.version or ""}'
        return uid if self.codename is None else f'{uid}-{self.codename}'

    def is_released(self, on: date | None = None) -> bool:
        """Whether the release is out on the date `on`, today by default."""
        return self.release_date is not None and self.release_date <= (on or date.today())

    def is_eol(self, on: date | None = None) -> bool:
        """Whether the release has reached its end of life on the date `on`, today by default."""
        return self.eol_date is not None and self.eol_date <= (on or date.today())

    def is_supported(self, on: date | None = None) -> bool:
        """Whether the release is out and not at its end of life on `on`, today by default."""
        on = on or date.today()
        return self.is_released(on) and not self.is_eol(on)

    def __eq__(self, other: object) -> bool:
        """Whether other is the same release."""
        if not isinstance(other, Release):
            return NotImplemented
        return self.uid == other.uid

    def __hash__(self) -> int:
        """The hash of the uid."""
        return hash(self.uid)

    def __lt__(self, other: object) -> bool:
        """Whether the release comes before other, a release of the same distribution."""
        if not isinstance(other, Release) or other.distribution != self.distribution:
            return NotImplemented
        return self.order < other.order

    def __str__(self) -> str:
        """The display name, the distribution's included."""
        return f'{self.distribution.name} {self.name}'
