"""The repositories built for the fixture project must stay those master's pkgbuild built.

Recorded on a rootful-Docker GitHub runner (master's pkgbuild fails under rootless engines), and
retired at the end of Phase 5 of the modernization.
"""

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from normalize import normalize_repository

ALL_RELEASES = {'debian': ['trixie'], 'ubuntu': ['noble'], 'fedora': ['44'], 'rocky': ['10']}


@pytest.mark.golden
def test_repositories_match_master(
        rootful_docker: None,
        project: Callable[[dict[str, list[str]], list[str]], Path],
        run_pkgbuild: Callable[[Path], subprocess.CompletedProcess[str]],
        snapshot: object) -> None:
    project_dir = project(ALL_RELEASES, ['hello-lib', 'hello-app'])
    result = run_pkgbuild(project_dir)
    assert result.returncode == 0, result.stdout
    repositories = sorted((project_dir / 'repositories').iterdir())
    assert {d.name: normalize_repository(d) for d in repositories} == snapshot
