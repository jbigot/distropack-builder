"""Unit tests of the golden-master normalization, on synthetic aptly and createrepo trees."""

import gzip
from pathlib import Path

import zstandard

from normalize import normalize_repository

SHA_A = 'a' * 64
SHA_B = 'b' * 64


def deb_tree(root: Path, *, seconds: int = 3600, depends: str = 'hello-lib',
             date: str = 'Thu, 05 Nov 2026 12:00:00 UTC', sha: str = SHA_A,
             packages: tuple[str, ...] = ('hello-lib', 'hello-app'),
             codenames: tuple[str, ...] = ('trixie', 'noble')) -> Path:
    """A repository as aptly publishes it, reduced to what normalize_repository reads."""
    for codename in codenames:
        dist = root / 'dists' / codename
        (dist / 'main' / 'binary-amd64').mkdir(parents=True)
        (dist / 'main' / 'source').mkdir(parents=True)
        (dist / 'Release').write_text(
            f'Origin: hello\nLabel: Hello test\nSuite: stable\nCodename: {codename}\n'
            f'Date: {date}\nArchitectures: amd64\nComponents: main\n'
            f'SHA256:\n {sha} 123 main/binary-amd64/Packages\n')
        (dist / 'InRelease').write_text(f'signed {date}\n')
        (dist / 'Release.gpg').write_text(f'signature {date}\n')
        version = f'1.0-1~bpo13.pdidev.{seconds}'
        stanzas = [f'Package: {p}\nVersion: {version}\nArchitecture: all\n'
                   + (f'Depends: {depends}\n' if p == 'hello-app' else '')
                   + f'Filename: pool/main/h/{p}/{p}_{version}_all.deb\nSize: {seconds}\n'
                   f'SHA256: {sha}\n' for p in packages]
        (dist / 'main' / 'binary-amd64' / 'Packages').write_text('\n'.join(stanzas))
        (dist / 'main' / 'source' / 'Sources').write_text(
            f'Package: hello-lib\nVersion: {version}\nFiles:\n {sha[:32]} 12 hello-lib.dsc\n'
            f'Checksums-Sha256:\n {sha} 12 hello-lib.dsc\n')
        with gzip.open(dist / 'main' / 'Contents-amd64.gz', 'wt') as contents:
            contents.write('usr/share/hello-lib/hello.sh misc/hello-lib\n')
        pool = root / 'pool' / 'main' / 'h' / 'hello-lib'
        pool.mkdir(parents=True, exist_ok=True)
        (pool / f'hello-lib_{version}_all.deb').write_bytes(sha.encode())
    (root / 'hello-archive-keyring.gpg').write_bytes(sha.encode())
    (root / 'index.html').write_text(f'<p>hello-lib {version}</p>\n')
    return root


def rpm_tree(root: Path, *, sha: str = SHA_A, requires: str = 'hello-lib') -> Path:
    """A repository as createrepo_c writes it, reduced to what normalize_repository reads."""
    repodata = root / '44' / 'repodata'
    repodata.mkdir(parents=True)
    primary = (
        '<metadata xmlns="http://linux.duke.edu/metadata/common" '
        'xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="1">'
        '<package type="rpm"><name>hello-app</name><arch>noarch</arch>'
        '<version epoch="0" ver="1.0" rel="1.fc44"/>'
        f'<checksum type="sha256" pkgid="YES">{sha}</checksum><format>'
        '<rpm:provides><rpm:entry name="hello-app" flags="EQ" epoch="0" ver="1.0" rel="1.fc44"/>'
        f'</rpm:provides><rpm:requires><rpm:entry name="{requires}"/></rpm:requires>'
        '</format></package></metadata>')
    filelists = (
        '<filelists xmlns="http://linux.duke.edu/metadata/filelists" packages="1">'
        f'<package pkgid="{sha}" name="hello-app" arch="noarch">'
        '<version epoch="0" ver="1.0" rel="1.fc44"/>'
        '<file>/usr/share/hello-app/greeting</file></package></filelists>')
    for kind, content in (('primary', primary), ('filelists', filelists)):
        (repodata / f'{sha}-{kind}.xml.zst').write_bytes(
            zstandard.ZstdCompressor().compress(content.encode()))
    (repodata / 'repomd.xml').write_text(
        '<repomd xmlns="http://linux.duke.edu/metadata/repo"><revision>{}</revision>'.format(
            1 if sha == SHA_A else 2)
        + ''.join(f'<data type="{kind}"><location href="repodata/{sha}-{kind}.xml.zst"/></data>'
                  for kind in ('primary', 'filelists'))
        + '</repomd>')
    (repodata / 'repomd.xml.asc').write_bytes(sha.encode())
    (root / '44' / 'hello.repo').write_text('[hello]\nbaseurl=http://repo.example.invalid/44\n')
    (root / 'hello.key').write_bytes(sha.encode())
    return root


def test_normalize_ignores_volatile_fields(tmp_path: Path) -> None:
    a = deb_tree(tmp_path / 'a')
    b = deb_tree(tmp_path / 'b', seconds=7200, date='Thu, 05 Nov 2026 13:00:00 UTC', sha=SHA_B)
    assert normalize_repository(a) == normalize_repository(b)


def test_normalize_ignores_volatile_rpm_fields(tmp_path: Path) -> None:
    assert (normalize_repository(rpm_tree(tmp_path / 'a'))
            == normalize_repository(rpm_tree(tmp_path / 'b', sha=SHA_B)))


def test_normalize_keeps_dependencies(tmp_path: Path) -> None:
    assert (normalize_repository(deb_tree(tmp_path / 'a'))
            != normalize_repository(deb_tree(tmp_path / 'b', depends='other')))


def test_normalize_keeps_package_set(tmp_path: Path) -> None:
    assert (normalize_repository(deb_tree(tmp_path / 'a'))
            != normalize_repository(deb_tree(tmp_path / 'b', packages=('hello-lib',))))


def test_normalize_keeps_release_set(tmp_path: Path) -> None:
    assert (normalize_repository(deb_tree(tmp_path / 'a'))
            != normalize_repository(deb_tree(tmp_path / 'b', codenames=('trixie',))))


def test_normalize_keeps_rpm_requires(tmp_path: Path) -> None:
    assert (normalize_repository(rpm_tree(tmp_path / 'a'))
            != normalize_repository(rpm_tree(tmp_path / 'b', requires='other')))
