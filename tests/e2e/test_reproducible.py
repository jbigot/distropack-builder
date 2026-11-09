# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""run --check-reproducible: each build twice, varied, its packages compared."""

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from support import FIXTURE_PASSPHRASE

Project = Callable[[dict[str, list[str]], list[str]], Path]
REPRODUCIBLE_RPM = (
    '%global clamp_mtime_to_source_epoch 1\n%global use_source_date_epoch_as_buildtime 1\n'
)


def builder(env: dict[str, str], project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, '-m', 'distropack_builder', *args],
        cwd=project,
        env={**env, 'DISTROPACK_GPG_PASSPHRASE': FIXTURE_PASSPHRASE},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


@pytest.mark.e2e
@pytest.mark.parametrize(('dist', 'release'), [('debian', 'trixie'), ('fedora', '44')])
def test_fixture_is_reproducible(
    docker_engine: None, project: Project, pkgbuild_env: dict[str, str], dist: str, release: str
) -> None:
    built = project({dist: [release]}, ['hello-lib', 'hello-app'])
    if dist == 'fedora':
        # Fedora records the build time and the files' mtimes unless the spec asks otherwise, as
        # a package wanting to be reproducible does: the builder sets nothing of its own
        for spec in built.glob('hello-*/hello-*.spec'):
            spec.write_text(REPRODUCIBLE_RPM + spec.read_text())
    result = builder(pkgbuild_env, built, 'sources', 'build.conf', 'hello-lib')
    assert result.returncode == 0, result.stdout
    result = builder(pkgbuild_env, built, 'run', '-j', '3', '--check-reproducible', 'build.conf')
    assert result.returncode == 0, result.stdout


@pytest.mark.e2e
@pytest.mark.parametrize(('dist', 'release'), [('debian', 'trixie'), ('fedora', '44')])
def test_unreproducible_package_fails(
    docker_engine: None, project: Project, pkgbuild_env: dict[str, str], dist: str, release: str
) -> None:
    built = project({dist: [release]}, ['hello-unreproducible', 'hello-undeclared'])
    # hello-undeclared fails its build: the check spares the builds it does not concern
    result = builder(pkgbuild_env, built, 'run', '-j', '3', '--check-reproducible', 'build.conf')
    assert result.returncode == 1, result.stdout
    assert 'unreproducible hello-unreproducible on' in result.stdout, result.stdout
    assert 'stamp' in result.stdout  # diffoscope names the differing file
