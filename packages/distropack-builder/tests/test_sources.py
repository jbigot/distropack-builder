# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The sources file of a package: the SHA-512 of each file its build downloads."""

import hashlib
from pathlib import Path

import pytest

from distropack_builder import sources
from distropack_builder.downloads import DownloadFailed


@pytest.fixture
def tarball(tmp_path: Path) -> Path:
    path = tmp_path / 'download'
    path.write_bytes(b'upstream\n')
    return path


def test_line_format(tarball: Path) -> None:
    """Fedora's format."""
    digest = hashlib.sha512(b'upstream\n').hexdigest()
    assert sources.line('hello-1.0.tar.gz', tarball) == f'SHA512 (hello-1.0.tar.gz) = {digest}'


def test_read(tmp_path: Path, tarball: Path) -> None:
    assert sources.read(tmp_path) == {}
    (tmp_path / 'sources').write_text(sources.line('a.tar.gz', tarball) + '\n\n')
    assert sources.read(tmp_path) == {'a.tar.gz': hashlib.sha512(b'upstream\n').hexdigest()}


def test_read_rejects_a_malformed_line(tmp_path: Path, tarball: Path) -> None:
    (tmp_path / 'sources').write_text(sources.line('a.tar.gz', tarball) + '\nMD5 (b) = 00\n')
    with pytest.raises(
        sources.SourcesError, match=r'sources:2: not a SHA512 \(<file>\) = <digest> line'
    ):
        sources.read(tmp_path)


def test_check(tarball: Path) -> None:
    expected = {'a.tar.gz': hashlib.sha512(b'upstream\n').hexdigest()}
    assert sources.check('a.tar.gz', tarball, expected) is None
    missing = sources.check('b.tar.gz', tarball, expected)
    assert isinstance(missing, DownloadFailed)
    assert str(missing) == 'while downloading b.tar.gz: no sources line for b.tar.gz'
    wrong = sources.check('a.tar.gz', tarball, {'a.tar.gz': '00'})
    assert isinstance(wrong, DownloadFailed)
    assert str(wrong) == 'while downloading a.tar.gz: a.tar.gz does not match its sources line'
