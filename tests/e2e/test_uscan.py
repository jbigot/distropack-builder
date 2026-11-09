# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""uscan and mk-origtargz in the Debian tools image, on the fixture project's watch file."""

import shutil
import tarfile
from pathlib import Path

import pytest

from distropack_builder.context import BuildContext
from distropack_builder.docker import Cancellation, Engine, run
from distropack_builder.uscan import OrigFiles, fetch_origs
from support import make_project

pytestmark = [pytest.mark.e2e, pytest.mark.enable_socket, pytest.mark.usefixtures('docker_engine')]

# A machine-readable debian/copyright excluding hello.sh from the upstream tarball.
COPYRIGHT = """\
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Files-Excluded: hello.sh

Files: *
Copyright: 2026 upstream
License: MIT
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """The fixture project, hello-lib and a copy of it, hello-lib2, excluding hello.sh."""
    project = make_project(tmp_path / 'project', 'http://unused', {}, ['hello-lib'])
    other = Path(shutil.copytree(project / 'hello-lib', project / 'hello-lib2'))
    changelog = other / 'debian' / 'changelog'
    changelog.write_text(changelog.read_text().replace('hello-lib', 'hello-lib2'))
    (other / 'debian' / 'copyright').write_text(COPYRIGHT)
    return project


def members(tarball: Path) -> set[str]:
    with tarfile.open(tarball) as tar:
        return {Path(name).name for name in tar.getnames()}


def test_shared_download_honours_each_files_excluded(project: Path, tmp_path: Path) -> None:
    work = tmp_path / 'work'
    work.mkdir()
    context = BuildContext(Engine.connect(), run, Cancellation(), work, 2)
    origs = fetch_origs(context, project, ['hello-lib', 'hello-lib2'], work / 'origs')
    lib, lib2 = origs['hello-lib'], origs['hello-lib2']
    assert isinstance(lib, OrigFiles), lib
    assert isinstance(lib2, OrigFiles), lib2
    assert lib.upstream == lib2.upstream  # one download
    assert lib.orig.name.startswith('hello-lib_1.0.orig.tar.')
    assert lib2.orig.name.startswith('hello-lib2_1.0.orig.tar.')
    assert 'hello.sh' in members(lib.orig)
    assert 'hello.sh' not in members(lib2.orig)
    assert not lib.verified
