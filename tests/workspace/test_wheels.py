# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The wheels carry what the code needs at run time, and the scripts run from them."""

import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from support import REPO_ROOT
from workspace_helpers import build, build_wheel, git, tag, wheel_version

pytestmark = pytest.mark.packaging

IMAGES = 'packages/distropack-builder/src/distropack_builder/images'


def test_wheel_ships_the_images(tmp_path: Path) -> None:
    wheel = build_wheel(REPO_ROOT, 'distropack-builder', tmp_path)
    tracked = git(REPO_ROOT, 'ls-files', IMAGES).split()
    assert tracked, 'no image file is tracked'
    prefix = 'packages/distropack-builder/src/'
    with zipfile.ZipFile(wheel) as archive:
        modes = {info.filename: info.external_attr >> 16 for info in archive.infolist()}
    executable = set()
    for name in tracked:
        member = name.removeprefix(prefix)
        assert member in modes, f'{member} is missing from the wheel'
        if modes[member] & stat.S_IXUSR:
            executable.add(Path(member).name + ' in ' + Path(member).parent.name)
    assert executable == {
        'entrypoint in deb_builder',
        'entrypoint in rpm_builder',
        'entrypoint in zypper_builder',
        'sign in rpm_sign',
    }


def test_wheel_ships_the_templates(tmp_path: Path) -> None:
    wheel = build_wheel(REPO_ROOT, 'distropack-builder', tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        assert 'distropack_builder/templates/README.html.j2' in archive.namelist()


@pytest.mark.parametrize('package', ['distro-releases', 'distropack-builder'])
def test_wheel_declares_and_ships_the_license(tmp_path: Path, package: str) -> None:
    wheel = build_wheel(REPO_ROOT, package, tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata = archive.read(next(n for n in names if n.endswith('.dist-info/METADATA')))
    assert b'License-Expression: MIT' in metadata
    assert any(n.endswith('.dist-info/licenses/LICENSES/MIT.txt') for n in names)


@pytest.mark.parametrize('package', ['distro-releases', 'distropack-builder'])
def test_distributions_pass_twine_check(tmp_path: Path, package: str) -> None:
    files = [build(REPO_ROOT, package, tmp_path / kind, kind) for kind in ('sdist', 'wheel')]
    result = subprocess.run(
        ['uvx', 'twine', 'check', '--strict', *map(str, files)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_wheel_from_unpacked_sdist_keeps_version_and_license(repo: Path, tmp_path: Path) -> None:
    tag(repo, 'distropack-builder-v1.2.3')
    sdist = build(repo, 'distropack-builder', tmp_path / 'sdist', 'sdist')
    with tarfile.open(sdist) as archive:
        archive.extractall(tmp_path / 'unpacked', filter='data')
    (tree,) = (tmp_path / 'unpacked').iterdir()
    wheel = build(tree, None, tmp_path / 'wheel', 'wheel')
    assert wheel_version(wheel) == '1.2.3'
    with zipfile.ZipFile(wheel) as archive:
        assert any(n.endswith('.dist-info/licenses/LICENSES/MIT.txt') for n in archive.namelist())


@pytest.fixture(scope='module')
def installed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fresh venv with both wheels installed, outside the workspace; its bin directory."""
    base = tmp_path_factory.mktemp('installed')
    wheels = [
        build_wheel(REPO_ROOT, package, base / 'wheels')
        for package in ('distro-releases', 'distropack-builder')
    ]
    venv = base / 'venv'
    subprocess.run(['uv', 'venv', '-q', '--python', '3.13', str(venv)], check=True)
    subprocess.run(
        ['uv', 'pip', 'install', '-q', '--python', str(venv / 'bin' / 'python'), *map(str, wheels)],
        check=True,
    )
    return venv / 'bin'


def run(cmd: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """cmd's result, output merged, run with the recorded release data of pkgbuild_env."""
    return subprocess.run(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )


def test_console_script_from_wheel(
    installed: Path, tmp_path: Path, pkgbuild_env: dict[str, str]
) -> None:
    for cmd in [
        [str(installed / 'distropack-build'), '--help'],
        [str(installed / 'python'), '-m', 'distropack_builder', '--help'],
    ]:
        result = run(cmd, tmp_path, pkgbuild_env)
        assert result.returncode == 0, result.stdout
        assert 'run' in result.stdout and 'images' in result.stdout
    result = run([str(installed / 'distropack-build'), 'run', '--help'], tmp_path, pkgbuild_env)
    assert result.returncode == 0, result.stdout
    assert '-D' in result.stdout and 'build.conf' in result.stdout


def test_arguments_reach_the_configuration(
    installed: Path, tmp_path: Path, pkgbuild_env: dict[str, str]
) -> None:
    result = run(
        [str(installed / 'distropack-build'), 'run', '-D', 'debian', '-j', '3', 'nosuch.conf'],
        tmp_path,
        pkgbuild_env,
    )
    assert result.returncode == 2, result.stdout
    assert result.stdout == 'distropack-build: error: nosuch.conf: No such file or directory\n'


def test_distro_releases_script_from_wheel(
    installed: Path, tmp_path: Path, pkgbuild_env: dict[str, str]
) -> None:
    for cmd in [
        [str(installed / 'distro-releases'), '--help'],
        [str(installed / 'python'), '-m', 'distro_releases', '--help'],
    ]:
        result = run(cmd, tmp_path, pkgbuild_env)
        assert result.returncode == 0, result.stdout
        assert 'list' in result.stdout
