# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Unit tests of the normalization kept from the golden master, on synthetic repositories."""

import gzip
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import zstandard

from normalize import normalize_repository


def part(result: dict[str, object], key: str) -> dict[str, Any]:
    """One part of a normalized repository, typed for indexing."""
    return cast(dict[str, Any], result[key])


SHA_A = 'a' * 64
SHA_B = 'b' * 64


def deb_tree(
    root: Path,
    *,
    seconds: int = 3600,
    depends: str = 'hello-lib',
    date: str = 'Thu, 05 Nov 2026 12:00:00 UTC',
    sha: str = SHA_A,
    packages: tuple[str, ...] = ('hello-lib', 'hello-app'),
    codenames: tuple[str, ...] = ('trixie', 'noble'),
) -> Path:
    """A repository as aptly publishes it, reduced to what normalize_repository reads."""
    for codename in codenames:
        dist = root / 'dists' / codename
        (dist / 'main' / 'binary-amd64').mkdir(parents=True)
        (dist / 'main' / 'source').mkdir(parents=True)
        (dist / 'Release').write_text(
            f'Origin: hello\nLabel: Hello test\nSuite: stable\nCodename: {codename}\n'
            f'Date: {date}\nArchitectures: amd64\nComponents: main\n'
            f'SHA256:\n {sha} 123 main/binary-amd64/Packages\n'
        )
        (dist / 'InRelease').write_text(f'signed {date}\n')
        (dist / 'Release.gpg').write_text(f'signature {date}\n')
        version = f'1.0-1~bpo13.pdidev.{seconds}'
        stanzas = [
            f'Package: {p}\nVersion: {version}\nArchitecture: all\n'
            + (f'Depends: {depends}\n' if p == 'hello-app' else '')
            + f'Filename: pool/main/h/{p}/{p}_{version}_all.deb\nSize: {seconds}\n'
            f'SHA256: {sha}\n'
            for p in packages
        ]
        (dist / 'main' / 'binary-amd64' / 'Packages').write_text('\n'.join(stanzas))
        (dist / 'main' / 'source' / 'Sources').write_text(
            f'Package: hello-lib\nVersion: {version}\nFiles:\n {sha[:32]} 12 hello-lib.dsc\n'
            f'Checksums-Sha256:\n {sha} 12 hello-lib.dsc\n'
        )
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
        '</format></package></metadata>'
    )
    filelists = (
        '<filelists xmlns="http://linux.duke.edu/metadata/filelists" packages="1">'
        f'<package pkgid="{sha}" name="hello-app" arch="noarch">'
        '<version epoch="0" ver="1.0" rel="1.fc44"/>'
        '<file>/usr/share/hello-app/greeting</file></package></filelists>'
    )
    for kind, content in (('primary', primary), ('filelists', filelists)):
        (repodata / f'{sha}-{kind}.xml.zst').write_bytes(
            zstandard.ZstdCompressor().compress(content.encode())
        )
    (repodata / 'repomd.xml').write_text(
        '<repomd xmlns="http://linux.duke.edu/metadata/repo">'
        f'<revision>{1 if sha == SHA_A else 2}</revision>'
        + ''.join(
            f'<data type="{kind}"><location href="repodata/{sha}-{kind}.xml.zst"/></data>'
            for kind in ('primary', 'filelists')
        )
        + '</repomd>'
    )
    (repodata / 'repomd.xml.asc').write_bytes(sha.encode())
    (root / '44' / 'hello.repo').write_text('[hello]\nbaseurl=http://repo.example.invalid/44\n')
    (root / 'hello.key').write_bytes(sha.encode())
    return root


def test_normalize_ignores_volatile_fields(tmp_path: Path) -> None:
    a = deb_tree(tmp_path / 'a')
    b = deb_tree(tmp_path / 'b', seconds=7200, date='Thu, 05 Nov 2026 13:00:00 UTC', sha=SHA_B)
    assert normalize_repository(a) == normalize_repository(b)


def test_normalize_ignores_volatile_rpm_fields(tmp_path: Path) -> None:
    assert normalize_repository(rpm_tree(tmp_path / 'a')) == normalize_repository(
        rpm_tree(tmp_path / 'b', sha=SHA_B)
    )


def test_normalize_keeps_dependencies(tmp_path: Path) -> None:
    assert normalize_repository(deb_tree(tmp_path / 'a')) != normalize_repository(
        deb_tree(tmp_path / 'b', depends='other')
    )


def test_normalize_keeps_package_set(tmp_path: Path) -> None:
    assert normalize_repository(deb_tree(tmp_path / 'a')) != normalize_repository(
        deb_tree(tmp_path / 'b', packages=('hello-lib',))
    )


def test_normalize_keeps_release_set(tmp_path: Path) -> None:
    assert normalize_repository(deb_tree(tmp_path / 'a')) != normalize_repository(
        deb_tree(tmp_path / 'b', codenames=('trixie',))
    )


def test_normalize_keeps_rpm_requires(tmp_path: Path) -> None:
    assert normalize_repository(rpm_tree(tmp_path / 'a')) != normalize_repository(
        rpm_tree(tmp_path / 'b', requires='other')
    )


class Signer:
    """A throwaway key in its own GNUPGHOME, signing as the repository builders do."""

    def __init__(self, home: Path, uid: str) -> None:
        self.home = home
        home.mkdir(mode=0o700)
        self.gpg('--passphrase', '', '--quick-gen-key', uid, 'ed25519', 'sign', 'never')
        self.fingerprint = next(
            line.split(':')[9]
            for line in self.gpg('--with-colons', '--list-keys').splitlines()
            if line.startswith('fpr:')
        )

    def gpg(self, *args: str) -> str:
        return subprocess.run(
            ['gpg', '--homedir', str(self.home), '--batch', '--pinentry-mode', 'loopback', *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def clearsign(self, source: Path, target: Path) -> None:
        self.gpg('--yes', '--output', str(target), '--clearsign', str(source))

    def detach_sign(self, source: Path, target: Path, armor: bool = False) -> None:
        self.gpg(
            '--yes',
            '--output',
            str(target),
            *(['--armor'] if armor else []),
            '--detach-sign',
            str(source),
        )

    def export(self, target: Path, *, armor: bool = False, secret: bool = False) -> None:
        self.gpg(
            '--yes',
            '--output',
            str(target),
            *(['--armor'] if armor else []),
            '--export-secret-keys' if secret else '--export',
        )

    def close(self) -> None:
        subprocess.run(['gpgconf', '--homedir', str(self.home), '--kill', 'all'], check=False)


@pytest.fixture(scope='module')
def signers(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Signer, Signer]]:
    base = tmp_path_factory.mktemp('gnupg')
    pair = (
        Signer(base / 'a', 'repository <a@example.invalid>'),
        Signer(base / 'b', 'someone else <b@example.invalid>'),
    )
    yield pair
    for signer in pair:
        signer.close()


def signed_deb_tree(
    root: Path,
    signer: Signer,
    *,
    keyring_from: Signer | None = None,
    stale_inrelease: bool = False,
    secret_keyring: bool = False,
) -> Path:
    """deb_tree() with its InRelease, Release.gpg and keyring made by real keys."""
    deb_tree(root, codenames=('trixie',))
    dist = root / 'dists' / 'trixie'
    signed = dist / 'Release'
    if stale_inrelease:
        signed = root / 'stale-Release'
        signed.write_text((dist / 'Release').read_text().replace('trixie', 'bookworm'))
    signer.clearsign(signed, dist / 'InRelease')
    if stale_inrelease:
        signed.unlink()
    signer.detach_sign(dist / 'Release', dist / 'Release.gpg')
    (keyring_from or signer).export(root / 'hello-archive-keyring.gpg', secret=secret_keyring)
    return root


def signed_rpm_tree(root: Path, signer: Signer) -> Path:
    """rpm_tree() with its repomd.xml.asc and key made by a real key."""
    rpm_tree(root)
    repodata = root / '44' / 'repodata'
    signer.detach_sign(repodata / 'repomd.xml', repodata / 'repomd.xml.asc', armor=True)
    signer.export(root / 'hello.key', armor=True)
    return root


def test_signatures_record_the_signing_key(tmp_path: Path, signers: tuple[Signer, Signer]) -> None:
    a, _ = signers
    result = normalize_repository(signed_deb_tree(tmp_path / 'deb', a))
    assert part(result, 'signatures') == {
        'dists/trixie/InRelease': f'VALIDSIG {a.fingerprint}, payload is Release',
        'dists/trixie/Release.gpg': f'VALIDSIG {a.fingerprint}',
    }
    rpm = normalize_repository(signed_rpm_tree(tmp_path / 'rpm', a))
    assert rpm['signatures'] == {'44/repodata/repomd.xml.asc': f'VALIDSIG {a.fingerprint}'}


def test_signatures_detect_a_stale_inrelease(
    tmp_path: Path, signers: tuple[Signer, Signer]
) -> None:
    a, _ = signers
    result = normalize_repository(signed_deb_tree(tmp_path / 'deb', a, stale_inrelease=True))
    assert part(result, 'signatures')['dists/trixie/InRelease'] == (
        f'VALIDSIG {a.fingerprint}, payload differs from Release'
    )


def test_signatures_detect_another_key(tmp_path: Path, signers: tuple[Signer, Signer]) -> None:
    a, b = signers
    result = normalize_repository(signed_deb_tree(tmp_path / 'deb', a, keyring_from=b))
    assert part(result, 'signatures')['dists/trixie/Release.gpg'] == 'not verified'


def test_keys_record_public_keys_only(tmp_path: Path, signers: tuple[Signer, Signer]) -> None:
    a, _ = signers
    public = normalize_repository(signed_deb_tree(tmp_path / 'pub', a))
    assert ['fpr', a.fingerprint] in part(public, 'keys')['hello-archive-keyring.gpg']
    assert not any(r[0] == 'sec' for r in part(public, 'keys')['hello-archive-keyring.gpg'])
    secret = normalize_repository(signed_deb_tree(tmp_path / 'sec', a, secret_keyring=True))
    assert any(r[0] == 'sec' for r in part(secret, 'keys')['hello-archive-keyring.gpg'])


def test_release_keeps_the_files_it_lists(tmp_path: Path) -> None:
    a = deb_tree(tmp_path / 'a', codenames=('trixie',))
    b = deb_tree(tmp_path / 'b', codenames=('trixie',))
    release = b / 'dists' / 'trixie' / 'Release'
    release.write_text(release.read_text() + f' {SHA_A} 45 main/source/Sources\n')
    assert normalize_repository(a) != normalize_repository(b)
