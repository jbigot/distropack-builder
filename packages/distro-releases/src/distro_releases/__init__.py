"""Distribution releases and their support status, from the distributions' own data."""

from distro_releases.catalog import distribution, distributions
from distro_releases.errors import SelectorError, SourceError, UnknownDistribution, UnknownRelease
from distro_releases.model import Distribution, Release, Row, Source
from distro_releases.selectors import matches, select

__all__ = [
    'Distribution',
    'Release',
    'Row',
    'SelectorError',
    'Source',
    'SourceError',
    'UnknownDistribution',
    'UnknownRelease',
    'distribution',
    'distributions',
    'matches',
    'select',
]
