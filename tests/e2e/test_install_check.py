# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""check-install on the fixture's repositories: served over HTTP, installed in clean containers."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from distropack_builder.docker.runner import LABEL
from support import FIXTURE_RELEASES


def check_install(env: dict[str, str], site: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            '-m',
            'distropack_builder',
            'check-install',
            '--site',
            str(site),
            *args,
            'build.conf',
        ],
        cwd=site,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def ours() -> list[str]:
    containers = subprocess.run(
        ['docker', 'ps', '-aq', '--filter', f'label={LABEL}'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    networks = subprocess.run(
        ['docker', 'network', 'ls', '-q', '--filter', f'label={LABEL}'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return containers + networks


@pytest.mark.e2e
def test_fixture_site_installs(fixture_build: Path, pkgbuild_env: dict[str, str]) -> None:
    result = check_install(pkgbuild_env, fixture_build)
    assert result.returncode == 0, result.stdout
    passed = [
        line for line in result.stdout.splitlines() if line.startswith('install check passed')
    ]
    assert len(passed) == sum(len(releases) for releases in FIXTURE_RELEASES.values()), (
        result.stdout
    )
    assert ours() == []


@pytest.mark.e2e
def test_wrong_inrelease_signature_fails(
    fixture_build: Path, pkgbuild_env: dict[str, str], tmp_path: Path
) -> None:
    site = Path(shutil.copytree(fixture_build, tmp_path / 'site', symlinks=True))
    release = FIXTURE_RELEASES['debian'][0]
    signed = site / 'repositories' / 'debian' / 'dists' / release / 'InRelease'
    signed.write_text(signed.read_text().replace('Label: Hello test', 'Label: Hello tesT'))
    result = check_install(pkgbuild_env, site, '-D', 'debian')
    assert result.returncode == 1, result.stdout
    assert 'install check failed' in result.stdout
    assert ours() == []
