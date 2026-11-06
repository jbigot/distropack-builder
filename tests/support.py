"""Constants shared by the test files and the fixtures of conftest.py."""

from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / 'tests' / 'characterization' / 'data'
SCRIPTS = ('distinfo', 'downloader', 'pkgbuild', 'build')
FROZEN_DATE = date(2026, 11, 5)
FIXTURE_PROJECT = REPO_ROOT / 'tests' / 'e2e' / 'project'
FIXTURE_PASSPHRASE = 'test-passphrase'
# The releases the shared fixture build covers: one per family, and Rocky Linux for Phase 10's el.
FIXTURE_RELEASES = {'debian': ['trixie'], 'ubuntu': ['noble'], 'fedora': ['44'], 'rocky': ['10']}
DOCKER_SHIM_DIR = REPO_ROOT / 'tests' / 'e2e' / 'docker-shim'
