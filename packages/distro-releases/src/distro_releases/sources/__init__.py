"""The sources of release data, one module each."""

from distro_releases.sources.bodhi import Bodhi
from distro_releases.sources.debian_csv import DebianCsv
from distro_releases.sources.endoflife import Endoflife

__all__ = ['Bodhi', 'DebianCsv', 'Endoflife']
