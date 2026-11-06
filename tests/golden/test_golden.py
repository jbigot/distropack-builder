"""The repositories built for the fixture project must stay those master's pkgbuild built.

Recorded on a rootful-Docker GitHub runner, the reference; under a rootless engine the test runs
through the docker shim, which drops the --user master's pkgbuild passes (note 164). One snapshot
per distribution, so that Phase 10 can compare its el repository with the Rocky one. Retired at the
end of Phase 7 of the modernization.
"""

from pathlib import Path

import pytest

from support import FIXTURE_RELEASES
from normalize import normalize_repository


@pytest.mark.golden
def test_repository_set(fixture_build: Path) -> None:
    assert sorted(d.name for d in (fixture_build / 'repositories').iterdir()) == sorted(
        FIXTURE_RELEASES)


@pytest.mark.golden
@pytest.mark.parametrize('dist', sorted(FIXTURE_RELEASES))
def test_repositories_match_master(fixture_build: Path, dist: str, snapshot: object) -> None:
    assert normalize_repository(fixture_build / 'repositories' / dist) == snapshot
