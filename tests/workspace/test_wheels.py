"""The wheels carry what the code needs at run time, and the scripts run from them."""

import stat
import zipfile
from pathlib import Path

import pytest
from support import REPO_ROOT
from workspace_helpers import build_wheel, git

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
    assert executable == {'entrypoint in deb_builder', 'entrypoint in rpm_builder'}
