# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""action/run-cli runs the command line from an archive of the repository, building nothing.

A workflow pinning the action downloads `git archive` of that commit, without .git.
"""

import os
import subprocess
from pathlib import Path

import pytest

from support import REPO_ROOT
from workspace_helpers import git_archive

pytestmark = pytest.mark.packaging


def run_cli(archive: Path, venv: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """archive's action/run-cli with args, its environment in venv, the workspace's out of reach."""
    environ = {k: v for k, v in os.environ.items() if not k.startswith(('VIRTUAL_ENV', 'UV_'))}
    environ.pop('PYTHONPATH', None)
    environ['PATH'] = os.pathsep.join(
        p for p in environ['PATH'].split(os.pathsep) if not p.startswith(str(REPO_ROOT))
    )
    environ['DISTROPACK_ACTION_VENV'] = str(venv)
    return subprocess.run(
        [str(archive / 'action' / 'run-cli'), *args],
        env=environ,
        cwd=venv.parent,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def archive(repo: Path, tmp_path: Path) -> Path:
    """`git archive` of the synthetic repository, as GitHub serves an action."""
    return git_archive(repo, tmp_path / 'action')


def test_runs_from_an_archive(archive: Path, tmp_path: Path) -> None:
    result = run_cli(archive, tmp_path / 'venv', 'config', 'schema')
    assert result.returncode == 0, result.stderr
    expected = subprocess.run(
        ['uv', 'run', '-q', 'distropack-build', 'config', 'schema'],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert result.stdout == expected


def test_builds_nothing(archive: Path, tmp_path: Path) -> None:
    venv = tmp_path / 'venv'
    assert run_cli(archive, venv, '--help').returncode == 0
    (site,) = venv.glob('lib/python3.*/site-packages')
    installed = {path.name.split('-')[0].lower() for path in site.iterdir()}
    for name in ('hatchling', 'hatch_vcs', 'setuptools_scm', 'pytest'):
        assert name not in installed, name
    # the packages themselves come from the archive's sources
    for name in ('distropack_builder', 'distro_releases'):
        assert name not in installed, name


def test_imports_the_archive(archive: Path, tmp_path: Path) -> None:
    for package, marker in (('distropack-builder', 'builder'), ('distro-releases', 'releases')):
        (init,) = (archive / 'packages' / package / 'src').glob('*/__init__.py')
        init.write_text(init.read_text() + f'print("{marker} from the archive")\n')
    result = run_cli(archive, tmp_path / 'venv', '--help')
    assert result.returncode == 0, result.stderr
    assert 'builder from the archive' in result.stdout
    assert 'releases from the archive' in result.stdout
