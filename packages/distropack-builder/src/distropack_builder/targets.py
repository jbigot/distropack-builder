"""The distributions the builder builds for, and the base image of their builders.

distro-releases knows more distributions than these (Red Hat Enterprise Linux): a distribution is a
target only when it has a base image here.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from distro_releases import Distribution, distribution

# The repository of each target's base image; the tag is the release's (bake.py).
BASE_IMAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        'almalinux': 'almalinux',
        'debian': 'debian',
        'fedora': 'fedora',
        'rocky': 'rockylinux/rockylinux',
        'ubuntu': 'ubuntu',
    }
)


class NotATarget(LookupError):
    """A distribution that the builder does not build for."""

    def __init__(self, id: str) -> None:
        """An error for the distribution id."""
        super().__init__(
            f'{id} is not a build target; the targets are {", ".join(sorted(BASE_IMAGES))}'
        )
        self.id = id


def target(id: str) -> Distribution:
    """The target distribution of that id.

    Raises:
        UnknownDistribution: distro-releases has no distribution of that id.
        NotATarget: it has one, but the builder does not build for it.
    """
    found = distribution(id)
    if id not in BASE_IMAGES:
        raise NotATarget(id)
    return found


def targets() -> tuple[Distribution, ...]:
    """Every target distribution, sorted by id."""
    return tuple(distribution(id) for id in sorted(BASE_IMAGES))


def is_deb(distribution: Distribution) -> bool:
    """Whether the distribution belongs to the Debian family, built with dpkg."""
    return 'debian' in (distribution.id, *distribution.id_like)


def is_rpm(distribution: Distribution) -> bool:
    """Whether the distribution belongs to the Fedora family, built with rpmbuild."""
    return 'fedora' in (distribution.id, *distribution.id_like)
