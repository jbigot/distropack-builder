"""Packaging that relies on more than it declares must fail to build, in every family."""

import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

RELEASES = [('debian', 'trixie'), ('ubuntu', 'noble'), ('fedora', '44'), ('rocky', '10')]
Project = Callable[[dict[str, list[str]], list[str]], Path]
RunPkgbuild = Callable[[Path], subprocess.CompletedProcess[str]]


@pytest.mark.e2e
@pytest.mark.parametrize('dist,rel', RELEASES)
def test_undeclared_tool_fails(
    docker_engine: bool, project: Project, run_pkgbuild: RunPkgbuild, dist: str, rel: str
) -> None:
    assert shutil.which('cmake'), 'needs cmake on the host, to show that the build cannot see it'
    result = run_pkgbuild(project({dist: [rel]}, ['hello-undeclared']))
    assert result.returncode != 0
    # make echoes the command whatever happens: only the error of the shell (rpm, "cmake: command not
    # found") or of make running it directly (deb, "cmake: No such file or directory") proves cmake
    # was missing
    assert re.search(
        r'cmake: (command not found|not found|No such file or directory)', result.stdout
    ), result.stdout


@pytest.mark.e2e
@pytest.mark.xfail(strict=True, reason='builds run as root until Phase 6')
@pytest.mark.parametrize('dist,rel', RELEASES)
def test_needs_root_fails(
    docker_engine: bool, project: Project, run_pkgbuild: RunPkgbuild, dist: str, rel: str
) -> None:
    assert run_pkgbuild(project({dist: [rel]}, ['hello-needs-root'])).returncode != 0
