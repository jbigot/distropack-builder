"""Distribution releases and their support status, from the distributions' own data."""

from distro_releases.catalog import distribution, distributions
from distro_releases.errors import SourceError, UnknownDistribution
from distro_releases.model import Distribution, Release

__all__ = [
    'Distribution',
    'Release',
    'SourceError',
    'UnknownDistribution',
    'distribution',
    'distributions',
]
