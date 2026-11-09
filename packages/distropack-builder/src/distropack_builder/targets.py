# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The distributions the builder builds for: their family, and the base image of their builders.

distro-releases knows more distributions than these (Red Hat Enterprise Linux): a distribution is a
target only when it is listed here. What a target is decides nothing elsewhere: its family does,
which selects the builder image variant, the repository format and the install instructions.

Enterprise Linux is one target, not one per clone: RHEL's majors, built in Rocky Linux's image of
that major, which rebuilds RHEL's own sources, and installable on RHEL, AlmaLinux, Rocky Linux and
Oracle Linux, which share its ABI. A major is released for the builder once Rocky Linux has
released it too, so that its image exists.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from distro_releases import Distribution, distribution
from distro_releases.model import Row

if TYPE_CHECKING:
    import httpx

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
        build_args: what its builder image's Dockerfile is given beyond the release.
        prerequisites: what a host must enable before installing from its repository, per host
            family, as (host family, shell commands) pairs, from the release's version.
    """

    id: str
    family: Family
    repository: str
    tag_suffix: str = ''
    unsuffixed: frozenset[str] = field(default_factory=frozenset)
    build_args: Mapping[str, str] = field(default_factory=dict)
    prerequisites: Callable[[str], tuple[tuple[str, str], ...]] = lambda version: ()

    def base_image(self, release_tag: str) -> str:
        """The base image of the builder of the release that release_tag names."""
        suffix = '' if release_tag in self.unsuffixed else self.tag_suffix
        return f'{self.repository}:{release_tag}{suffix}'


TARGETS: Final[Mapping[str, Target]] = MappingProxyType(
    {
        t.id: t
        for t in (
            # debian:rc-buggy has no slim variant
            Target('debian', 'deb', 'debian', '-slim', frozenset({'rc-buggy'})),
            # built in Rocky Linux's image of the major, its latest minor
            Target(
                'el',
                'rpm-dnf',
                'rockylinux/rockylinux',
                build_args={'DIST_EL': '1'},
                prerequisites=lambda major: _el_prerequisites(major),
            ),
            Target('fedora', 'rpm-dnf', 'fedora'),
            Target('ubuntu', 'deb', 'ubuntu'),
        )
    }
)


def _el_prerequisites(major: str) -> tuple[tuple[str, str], ...]:
    """What each Enterprise Linux host enables first: CRB (PowerTools on 8) and EPEL."""
    crb = 'powertools' if major == '8' else 'crb'
    return (
        (
            'Rocky Linux and AlmaLinux',
            f'sudo dnf config-manager --set-enabled {crb}\nsudo dnf install -y epel-release',
        ),
        (
            'Red Hat Enterprise Linux',
            'sudo subscription-manager repos --enable'
            f' codeready-builder-for-rhel-{major}-$(arch)-rpms\nsudo dnf install -y'
            f' https://dl.fedoraproject.org/pub/epel/epel-release-latest-{major}.noarch.rpm',
        ),
        (
            'Oracle Linux',
            f'sudo dnf config-manager --set-enabled ol{major}_codeready_builder'
            f'\nsudo dnf install -y oracle-epel-release-el{major}',
        ),
    )


@dataclass(frozen=True, slots=True)
class EnterpriseLinux:
    """The releases of Enterprise Linux: RHEL's majors that Rocky Linux has too.

    Each is released on the later of both release dates, and reaches its end of life with RHEL's.
    """

    def rows(self, distribution: Distribution, client: httpx.Client) -> tuple[Row, ...]:
        """RHEL's rows, restricted to Rocky Linux's majors, released when both are."""
        rhel, rocky = (_distribution(id) for id in ('rhel', 'rocky'))
        clone = {row.version: row for row in rocky.source.rows(rocky, client)}
        result = []
        for row in rhel.source.rows(rhel, client):
            twin = clone.get(row.version)
            if twin is None:
                continue
            dates = (row.release_date, twin.release_date)
            released = None if None in dates else max(d for d in dates if d is not None)
            result.append(replace(row, release_date=released))
        return tuple(result)


EL: Final = Distribution(
    'el',
    'Enterprise Linux',
    ('rhel', 'almalinux', 'rocky', 'ol'),
    ('rhel', 'fedora'),
    'cpe:/o:redhat:enterprise_linux',
    EnterpriseLinux(),
)


def _distribution(id: str) -> Distribution:
    """The distribution of that id: distro-releases', or the builder's own Enterprise Linux."""
    return EL if id == EL.id else distribution(id)


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
    found = _distribution(id)
    if id not in TARGETS:
        raise NotATarget(id)
    return found


def targets() -> tuple[Distribution, ...]:
    """Every target distribution, sorted by id."""
    return tuple(_distribution(id) for id in sorted(TARGETS))


def family(distribution: Distribution) -> Family:
    """The packaging family of a target distribution."""
    return TARGETS[distribution.id].family


def is_deb(distribution: Distribution) -> bool:
    """Whether the distribution belongs to the Debian family, built with dpkg."""
    return family(distribution) == 'deb'


def is_rpm(distribution: Distribution) -> bool:
    """Whether the distribution is built with rpmbuild, whichever its package manager."""
    return family(distribution) != 'deb'
