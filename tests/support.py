"""Constants shared by the test files and the fixtures of conftest.py."""

from datetime import date
from pathlib import Path

from distro_releases import Release, distribution

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / 'tests' / 'characterization' / 'data'
# The recorded release data, shared with distro-releases' own tests.
RELEASE_DATA = REPO_ROOT / 'packages' / 'distro-releases' / 'tests' / 'data'
# The modules load_script reloads, so that each test starts from fresh module state.
MODULES = (
    'distropack_builder.downloader',
    'distropack_builder.targets',
    'distropack_builder.bake',
    'distropack_builder.cli',
)
# The names the tests pass to load_script, from before the move, and the modules they load.
SCRIPT_MODULES = {
    'build': 'distropack_builder.bake',
    'pkgbuild': 'distropack_builder.cli',
}
FROZEN_DATE = date(2026, 11, 5)
FIXTURE_PROJECT = REPO_ROOT / 'tests' / 'e2e' / 'project'
FIXTURE_PASSPHRASE = 'test-passphrase'
# The releases the shared fixture build covers: one per family, and Rocky Linux for Phase 10's el.
FIXTURE_RELEASES = {'debian': ['trixie'], 'ubuntu': ['noble'], 'fedora': ['44'], 'rocky': ['10']}


def made_release(dist: str, version: str | None, codename: str | None, order: int) -> Release:
    """A release of dist with only its identity filled in, for tests needing no release data."""
    return Release(
        distribution(dist),
        version,
        codename,
        version or str(codename),
        order,
        None,
        None,
        None,
        None,
        None,
    )


TRIXIE = made_release('debian', '13', 'trixie', 13)
FORKY = made_release('debian', '14', 'forky', 14)
SID = made_release('debian', None, 'sid', 1000)
