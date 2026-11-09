# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""A GitHub watch file, as pdidev/pkgs writes them, read by uscan against GitHub itself."""

import hashlib
from pathlib import Path

import pytest

from distropack_builder.context import BuildContext
from distropack_builder.docker import Cancellation, Engine, run
from distropack_builder.uscan import OrigFiles, fetch_origs

pytestmark = [pytest.mark.live, pytest.mark.enable_socket, pytest.mark.usefixtures('docker_engine')]

# GitHub's tag list, through its API (its HTML tags page is slow to the point of timing out), each
# tag downloaded from github.com/<owner>/<repo>/archive/: the same tarball as the orig: URL of
# pdidev/pkgs' build.conf had.
WATCH = r"""version=4
opts="searchmode=plain, \
  downloadurlmangle=s%api.github.com/repos/([^/]+)/([^/]+)/tarball/refs/tags/%github.com/$1/$2/archive/refs/tags/%;s%$%.tar.gz%, \
  filenamemangle=s%.*/v?@ANY_VERSION@%@PACKAGE@-$1.tar.gz%" \
  https://api.github.com/repos/pdidev/paraconf/tags?per_page=100 \
  https://api.github.com/repos/pdidev/paraconf/tarball/refs/tags/v?@ANY_VERSION@
"""  # noqa: E501 (a watch file cannot break a regular expression)
# What github.com/pdidev/paraconf/archive/1.2.1.tar.gz served on 2026-11-09.
PARACONF_1_2_1 = '2ff116724fe11643684996fefeef9d02ef302956b92cbc3130756376d78736f6'
CHANGELOG = """paraconf (1.2.1-1) unstable; urgency=medium

  * Test.

 -- test <test@example.invalid>  Thu, 01 Jan 2026 00:00:00 +0000
"""


def test_github_tags_watch_file(tmp_path: Path) -> None:
    debian = tmp_path / 'project' / 'paraconf' / 'debian'
    (debian / 'source').mkdir(parents=True)
    (debian / 'source' / 'format').write_text('3.0 (quilt)\n')
    (debian / 'changelog').write_text(CHANGELOG)
    (debian / 'watch').write_text(WATCH)
    work = tmp_path / 'work'
    work.mkdir()
    context = BuildContext(Engine.connect(), run, Cancellation(), work, 1)
    found = fetch_origs(context, tmp_path / 'project', ['paraconf'], work / 'origs')['paraconf']
    assert isinstance(found, OrigFiles), found
    assert found.orig.name == 'paraconf_1.2.1.orig.tar.gz'
    assert found.upstream.name == 'paraconf-1.2.1.tar.gz'
    assert hashlib.sha256(found.upstream.read_bytes()).hexdigest() == PARACONF_1_2_1
