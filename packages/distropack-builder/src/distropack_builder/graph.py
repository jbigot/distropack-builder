# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Which builds depend on which: the build dependencies one package's builds provide to another's.

A build depends on another of the same release when the other's binary packages provide one of
its build dependencies; builds of different releases never depend on each other.
"""

from collections.abc import Hashable, Iterable, Mapping, Set
from typing import Protocol

from distro_releases import Release


class Package(Hashable, Protocol):
    """A build, as far as its dependencies go."""

    @property
    def dist_release(self) -> Release:
        """The release it is built for."""
        ...

    @property
    def bin_provides(self) -> Set[str]:
        """The binary packages it builds."""
        ...

    @property
    def bin_depends(self) -> Set[str]:
        """The binary packages it needs to build."""
        ...


def dependencies[P: Package](packages: Iterable[P]) -> dict[P, frozenset[P]]:
    """For each package, the packages of its release that provide one of its build dependencies."""
    packages = list(packages)
    providers: dict[tuple[str, Release], set[P]] = {}
    for package in packages:
        for provided in package.bin_provides:
            providers.setdefault((provided, package.dist_release), set()).add(package)
    return {
        package: frozenset(
            provider
            for needed in package.bin_depends
            for provider in providers.get((needed, package.dist_release), ())
            if provider != package
        )
        for package in packages
    }


def closure[P: Hashable](graph: Mapping[P, Set[P]], package: P) -> frozenset[P]:
    """Every package that package depends on, directly or not, package itself excluded."""
    found: set[P] = set()
    pending = list(graph[package])
    while pending:
        dependency = pending.pop()
        if dependency not in found:
            found.add(dependency)
            pending.extend(graph[dependency])
    found.discard(package)
    return frozenset(found)
