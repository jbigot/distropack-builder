# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Which releases a run publishes: those all of whose builds succeeded or were reused.

A release with a failed, skipped or interrupted build keeps the repository of its previous run, so
that its users never see a partial set of packages; the other releases publish regardless.
"""

from collections.abc import Hashable, Mapping
from typing import Protocol

from distro_releases import Release
from distropack_builder.scheduler import Outcome, Reused, Succeeded


class Build(Hashable, Protocol):
    """A build, as far as publication goes."""

    @property
    def dist_release(self) -> Release:
        """The release it is built for."""
        ...


def published[B: Build](outcomes: Mapping[B, Outcome]) -> set[Release]:
    """The releases all of whose builds succeeded or were reused."""
    releases = {build.dist_release for build in outcomes}
    withheld = {
        b.dist_release for b, o in outcomes.items() if not isinstance(o, Succeeded | Reused)
    }
    return releases - withheld
