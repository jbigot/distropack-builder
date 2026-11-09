# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The distributions the builder builds for: their family, and the base image of their builders.

distro-releases knows more distributions than these (Red Hat Enterprise Linux): a distribution is a
target only when it is listed here. What a target is decides nothing elsewhere: its family does,
which selects the builder image variant, the repository format and the install instructions.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal

from distro_releases import Distribution, distribution

# deb: dpkg and apt; rpm-dnf: rpm and dnf (the Fedora family); rpm-zypper: rpm and zypper.
Family = Literal['deb', 'rpm-dnf', 'rpm-zypper']


@dataclass(frozen=True)
class Target:
    """A distribution the builder builds for.

    Attributes:
        id: its distro-releases id.
        family: its packaging family.
        repository: the repository of its base images; the tag is the release's.
        tag_suffix: what the tag of a base image adds to the release (`-slim`), but for the
            releases of `unsuffixed`, whose images have no such variant.
        unsuffixed: the releases whose base image tag is the release alone.
    """

    id: str
    family: Family
    repository: str
    tag_suffix: str = ''
    unsuffixed: frozenset[str] = field(default_factory=frozenset)

    def base_image(self, release_tag: str) -> str:
        """The base image of the builder of the release that release_tag names."""
        suffix = '' if release_tag in self.unsuffixed else self.tag_suffix
        return f'{self.repository}:{release_tag}{suffix}'


TARGETS: Final[Mapping[str, Target]] = MappingProxyType(
    {
        t.id: t
        for t in (
            Target('almalinux', 'rpm-dnf', 'almalinux'),
            # debian:rc-buggy has no slim variant
            Target('debian', 'deb', 'debian', '-slim', frozenset({'rc-buggy'})),
            Target('fedora', 'rpm-dnf', 'fedora'),
            Target('rocky', 'rpm-dnf', 'rockylinux/rockylinux'),
            Target('ubuntu', 'deb', 'ubuntu'),
        )
    }
)


class NotATarget(LookupError):
    """A distribution that the builder does not build for."""

    def __init__(self, id: str) -> None:
        """An error for the distribution id."""
        super().__init__(
            f'{id} is not a build target; the targets are {", ".join(sorted(TARGETS))}'
        )
        self.id = id


def target(id: str) -> Distribution:
    """The target distribution of that id.

    Raises:
        UnknownDistribution: distro-releases has no distribution of that id.
        NotATarget: it has one, but the builder does not build for it.
    """
    found = distribution(id)
    if id not in TARGETS:
        raise NotATarget(id)
    return found


def targets() -> tuple[Distribution, ...]:
    """Every target distribution, sorted by id."""
    return tuple(distribution(id) for id in sorted(TARGETS))


def family(distribution: Distribution) -> Family:
    """The packaging family of a target distribution."""
    return TARGETS[distribution.id].family


def is_deb(distribution: Distribution) -> bool:
    """Whether the distribution belongs to the Debian family, built with dpkg."""
    return family(distribution) == 'deb'


def is_rpm(distribution: Distribution) -> bool:
    """Whether the distribution is built with rpmbuild, whichever its package manager."""
    return family(distribution) != 'deb'
