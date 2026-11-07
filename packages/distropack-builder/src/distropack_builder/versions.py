"""The version of a rebuilt package."""

from datetime import datetime
from email.utils import parsedate_to_datetime

from distro_releases import Release


def deb_version(version: str, changelog_date: str, release: Release, now: datetime) -> str:
    """The version of a deb rebuilt for release, from the version of its latest changelog entry.

    The rebuild sorts before the package of the release's next version (`~bpo<version>`, for a
    release that has one), and after any earlier rebuild of the same entry: its last part is the
    number of whole seconds from the entry's date (in RFC 2822 format) to now, 0 for an entry dated
    after now: a negative number would put a hyphen in the version, which splits it anew.
    """
    if release.version:
        version += f'~bpo{release.version}'
    elapsed = now - parsedate_to_datetime(changelog_date)
    return f'{version}.pdidev.{max(0, int(elapsed.total_seconds()))}'
