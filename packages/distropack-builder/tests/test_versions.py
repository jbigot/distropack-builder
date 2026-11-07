from datetime import UTC, datetime

from distropack_builder.versions import deb_version
from support import SID, TRIXIE

NOW = datetime(2026, 11, 2, 11, tzinfo=UTC)


def test_versioned_release() -> None:
    assert (
        deb_version('1.0-1', 'Mon, 02 Nov 2026 10:00:00 +0000', TRIXIE, NOW)
        == '1.0-1~bpo13.pdidev.3600'
    )


def test_release_without_version() -> None:
    assert deb_version('1.0-1', 'Mon, 02 Nov 2026 10:00:00 +0000', SID, NOW) == '1.0-1.pdidev.3600'


def test_other_timezone() -> None:
    assert (
        deb_version('1.0-1', 'Mon, 02 Nov 2026 12:00:00 +0200', TRIXIE, NOW)
        == '1.0-1~bpo13.pdidev.3600'
    )


def test_future_changelog_date() -> None:
    """A changelog entry dated after the build counts as just made: no negative, no hyphen."""
    assert (
        deb_version('1.0-1', 'Mon, 02 Nov 2026 12:00:00 +0000', TRIXIE, NOW)
        == '1.0-1~bpo13.pdidev.0'
    )
