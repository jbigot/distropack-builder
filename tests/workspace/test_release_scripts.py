# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The scripts of the release workflow, run on distributions built from a synthetic repository."""

import subprocess
from pathlib import Path

import pytest

from support import REPO_ROOT
from workspace_helpers import build, tag

pytestmark = pytest.mark.packaging

CHECK = REPO_ROOT / '.github' / 'scripts' / 'check-tag-version'


def check(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CHECK), *map(str, args)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )


def dist(repo: Path, package: str, out: Path) -> Path:
    """The sdist and the wheel of package, built from repo into out; out."""
    for kind in ('sdist', 'wheel'):
        build(repo, package, out, kind)
    return out


@pytest.mark.parametrize(
    ('name', 'package'),
    [
        ('distropack-builder-v1.2.3', 'distropack-builder'),
        ('distro-releases-v1.0.0', 'distro-releases'),
    ],
)
def test_tag_names_its_package(name: str, package: str) -> None:
    result = check(name)
    assert (result.returncode, result.stdout) == (0, package + '\n')


@pytest.mark.parametrize('name', ['v1.0.0', 'distropack-builder-1.0.0', 'other-v1.0.0'])
def test_tag_of_no_family_is_refused(name: str) -> None:
    result = check(name)
    assert result.returncode == 2, result.stdout
    assert name in result.stdout


def test_distributions_of_the_tag(repo: Path, tmp_path: Path) -> None:
    tag(repo, 'distropack-builder-v1.2.3')
    out = dist(repo, 'distropack-builder', tmp_path / 'dist')
    result = check('distropack-builder-v1.2.3', out)
    assert result.returncode == 0, result.stdout


def test_distributions_of_a_later_commit(repo: Path, tmp_path: Path) -> None:
    tag(repo, 'distropack-builder-v1.2.3', 'HEAD~1')
    out = dist(repo, 'distropack-builder', tmp_path / 'dist')
    result = check('distropack-builder-v1.2.3', out)
    assert result.returncode == 1
    assert 'distropack_builder-1.2.4.dev1' in result.stdout and '1.2.3' in result.stdout


def test_distributions_of_the_other_package(repo: Path, tmp_path: Path) -> None:
    tag(repo, 'distro-releases-v1.0.0')
    out = dist(repo, 'distropack-builder', tmp_path / 'dist')
    result = check('distro-releases-v1.0.0', out)
    assert result.returncode == 1
    assert 'distropack_builder' in result.stdout


def test_empty_directory_is_refused(tmp_path: Path) -> None:
    result = check('distropack-builder-v1.2.3', tmp_path)
    assert result.returncode == 1
    assert 'no distribution' in result.stdout
