# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""What a repository publishes, read from its indexes, and the versions a publication may add."""

import gzip
import lzma
import shutil
from pathlib import Path

import httpx
import pytest
import respx
from hypothesis import given
from hypothesis import strategies as st

from distro_releases import Release, distribution
from distropack_builder import published, repodata
from distropack_builder.published import Offence, Version

DATA = Path(__file__).parent / 'data'
URL = 'https://repo.example.org/debian'
RPM_URL = 'https://repo.example.org/fedora'
TRIXIE = Release(
    distribution('debian'), '13', 'trixie', '13 (Trixie)', 13, 'stable', None, None, None, None
)
F44 = Release(distribution('fedora'), '44', None, '44', 44, None, None, None, None, None)

PACKAGES = """\
Package: pdi
Architecture: amd64
Version: 1:1.12.0+main.37.gabcdef0-1~bpo13
SHA256: 1111111111111111111111111111111111111111111111111111111111111111

Package: pdi-doc
Architecture: all
Version: 1.12.0-1~bpo13+b1
SHA256: 2222222222222222222222222222222222222222222222222222222222222222
"""
SOURCES = """\
Package: pdi
Version: 1:1.12.0+main.37.gabcdef0-1~bpo13
Checksums-Sha256:
 3333333333333333333333333333333333333333333333333333333333333333 1234 pdi_1.12.0-1.dsc
 4444444444444444444444444444444444444444444444444444444444444444 5678 pdi_1.12.0.orig.tar.gz
"""
EXPECTED_DEB = {
    ('pdi', 'amd64'): Version('1:1.12.0+main.37.gabcdef0-1~bpo13', '1' * 64),
    ('pdi-doc', 'all'): Version('1.12.0-1~bpo13+b1', '2' * 64),
    ('pdi', 'source'): Version('1:1.12.0+main.37.gabcdef0-1~bpo13', '3' * 64),
}


def deb_tree(root: Path) -> Path:
    """A deb repository's indexes for trixie/main: Packages as .xz, Sources as .gz."""
    dist = root / 'dists' / 'trixie' / 'main'
    (dist / 'binary-amd64').mkdir(parents=True)
    (dist / 'source').mkdir()
    (dist / 'binary-amd64' / 'Packages.xz').write_bytes(lzma.compress(PACKAGES.encode()))
    (dist / 'source' / 'Sources.gz').write_bytes(gzip.compress(SOURCES.encode()))
    return root


def rpm_tree(root: Path) -> Path:
    """An rpm repository of the test rpms for Fedora 44, its metadata by createrepo_c."""
    release = root / '44'
    release.mkdir(parents=True)
    for rpm in (DATA / 'rpms').glob('*.rpm'):
        shutil.copy(rpm, release / rpm.name)
    repodata.write(release)
    return root


def serve(router: respx.MockRouter, root: Path, base: str) -> None:
    """Serve root's files at base, 404 for any other path under it."""

    def answer(request: httpx.Request) -> httpx.Response:
        path = root / request.url.path.removeprefix(httpx.URL(base).path).lstrip('/')
        return (
            httpx.Response(200, content=path.read_bytes())
            if path.is_file()
            else httpx.Response(404)
        )

    router.get(url__startswith=base).mock(side_effect=answer)


def test_deb_indexes_read(respx_mock: respx.MockRouter, tmp_path: Path) -> None:
    serve(respx_mock, deb_tree(tmp_path), URL)
    with httpx.Client() as client:
        assert published.versions(URL, 'deb', TRIXIE, 'main', client) == EXPECTED_DEB


def test_local_directory_reads_alike(tmp_path: Path) -> None:
    with httpx.Client() as client:
        assert published.versions(deb_tree(tmp_path), 'deb', TRIXIE, 'main', client) == EXPECTED_DEB


def test_rpm_indexes_read(respx_mock: respx.MockRouter, tmp_path: Path) -> None:
    serve(respx_mock, rpm_tree(tmp_path), RPM_URL)
    with httpx.Client() as client:
        found = published.versions(RPM_URL, 'rpm', F44, 'main', client)
    assert set(found) == {('hello-app', 'noarch'), ('hello-lib', 'noarch')}
    assert found[('hello-lib', 'noarch')].version == '1.0-1.fc44'
    assert len(found[('hello-lib', 'noarch')].sha256) == 64


def test_missing_suite_is_empty(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__startswith=URL).respond(404)
    with httpx.Client() as client:
        assert published.versions(URL, 'deb', TRIXIE, 'main', client) == {}
        assert published.versions(URL, 'rpm', F44, 'main', client) == {}


def test_server_error_fails(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__startswith=URL).respond(500)
    with httpx.Client() as client, pytest.raises(published.PublishedError):
        published.versions(URL, 'deb', TRIXIE, 'main', client)


SHA = 'a' * 64
OTHER = 'b' * 64


@pytest.mark.parametrize(
    ('family', 'new', 'old', 'offends'),
    [
        ('deb', '1.0-2', '1.0-1', False),
        ('deb', '1.0-1', '1.0-1', False),  # same sha below
        ('deb', '1.0-1~bpo13+b1', '1.0-1~bpo13', False),
        ('deb', '1.0-1~bpo13', '1.0-1~bpo13+b1', True),
        ('deb', '1.0-1~bpo13', '1.0-1~bpo12', False),
        (
            'deb',
            '1:1.12.0+main.37.gabcdef0-1~bpo13',
            '1790170994main-1~bpo13.pdidev.1275000',
            False,
        ),
        ('deb', '1.12.0-1~bpo13', '1790170994main-1~bpo13.pdidev.1275000', True),
        ('deb', '2.0-1', '1:1.0-1', True),
        ('rpm', '1.0-2.fc44', '1.0-1.fc44', False),
        ('rpm', '1.0-2.fc44', '1:1.0-1.fc44', True),
        ('rpm', '1:1.0-1.fc44', '1.0-9.fc44', False),
        ('rpm', '1.0~rc1-1.fc44', '1.0-1.fc44', True),
    ],
)
def test_offences(family: str, new: str, old: str, offends: bool) -> None:
    key = ('pdi', 'amd64')
    found = published.offences({key: Version(new, SHA)}, {key: Version(old, SHA)}, family)
    assert bool(found) == offends


def test_equal_version_other_bytes_offends() -> None:
    key = ('pdi', 'amd64')
    (offence,) = published.offences(
        {key: Version('1.0-1', SHA)}, {key: Version('1.0-1', OTHER)}, 'deb'
    )
    assert offence == Offence('pdi', 'amd64', '1.0-1', '1.0-1')
    assert str(offence) == 'pdi 1.0-1: 1.0-1 is already published'


def test_new_and_dropped_packages_do_not_offend() -> None:
    new = {('pdi', 'amd64'): Version('1.0-1', SHA)}
    old = {('gone', 'amd64'): Version('9.0-1', SHA)}
    assert published.offences(new, old, 'deb') == []


VERSION = st.from_regex(
    r'([1-9]:)?[0-9]{1,3}(\.[0-9]{1,3}){0,2}(~rc[0-9])?-[0-9]{1,2}', fullmatch=True
)


@given(VERSION, VERSION)
def test_guard_never_accepts_a_lower_version(new: str, old: str) -> None:
    key = ('pdi', 'amd64')
    from debian.debian_support import Version as DebVersion

    from distropack_builder.versions import rpm_evr_compare

    for family, lower in (
        ('deb', DebVersion(new) < DebVersion(old)),
        ('rpm', rpm_evr_compare(new, old) < 0),
    ):
        found = published.offences({key: Version(new, SHA)}, {key: Version(old, SHA)}, family)
        if lower:
            assert found


def test_rpm_evr_compare() -> None:
    from distropack_builder.versions import rpm_evr_compare

    assert rpm_evr_compare('1:1.0-1', '2.0-1') == 1
    assert rpm_evr_compare('1.0-1', '0:1.0-1') == 0
    assert rpm_evr_compare('1.0-1.fc44', '1.0-1.fc44.1') == -1
