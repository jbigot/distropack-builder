# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Reduce a published repository to what must stay identical from one build to the next.

Two builds of the same packages differ in what is volatile by nature: the `.pdidev.<seconds>`
suffix of deb versions, dates, checksums, sizes, signature bytes, and the hash prefixes of the rpm
metadata files. normalize_repository() keeps everything else: the files published, the package
stanzas, the releases and the files they list, the dependencies, the file lists, the generated
README and .repo files, which key made each repository signature and whether it verifies against
the key the repository publishes, and the key packets of that published key.

The golden master that compared the fixture project's repositories with master's is retired (Phase
7 of the modernization). This module stays, with data/rocky-master.json, master's Rocky Linux 10
repository so reduced, for Phase 10 to compare its el repository with.
"""

import bz2
import gzip
import lzma
import re
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import IO, cast

import zstandard
from debian.deb822 import Deb822

PDIDEV_VERSION = re.compile(r'\.pdidev\.\d+')
REPODATA_HASH = re.compile(r'(^|/)[0-9a-f]{64}-')
DEB_INDEXES = ('Packages', 'Sources')
DEB_VOLATILE = {
    'md5sum',
    'sha1',
    'sha256',
    'sha512',
    'size',
    'files',
    'checksums-sha1',
    'checksums-sha256',
    'checksums-sha512',
    'date',
    'valid-until',
}
# Fields of a Release file listing the indexes it covers, one "<hash> <size> <path>" per line.
RELEASE_FILE_LISTS = ('md5sum', 'sha1', 'sha256', 'sha512')
KEY_FILES = ('*-archive-keyring.gpg', '*.key')
TEXT_FILES = ('index.html', 'README.html')
NS = {
    'repo': 'http://linux.duke.edu/metadata/repo',
    'common': 'http://linux.duke.edu/metadata/common',
    'rpm': 'http://linux.duke.edu/metadata/rpm',
    'filelists': 'http://linux.duke.edu/metadata/filelists',
}
# Openers, not open files: the callers open them in a with statement.
OPENERS: dict[str, Callable[[Path], IO[bytes]]] = {
    '.gz': lambda p: cast(IO[bytes], gzip.open(p)),  # noqa: SIM115
    '.bz2': lambda p: bz2.open(p),  # noqa: SIM115
    '.xz': lambda p: lzma.open(p),  # noqa: SIM115
    '.zst': lambda p: zstandard.open(p, 'rb'),
}


def normalize_text(text: str) -> str:
    """Replace the volatile parts of deb versions."""
    return PDIDEV_VERSION.sub('.pdidev.N', text)


def normalize_path(path: str) -> str:
    """A relative path with its volatile parts replaced."""
    return normalize_text(REPODATA_HASH.sub(r'\1', path))


def read_bytes(path: Path) -> bytes:
    """The content of a file, decompressed according to its extension."""
    opener = OPENERS.get(path.suffix)
    if opener is None:
        return path.read_bytes()
    with opener(path) as stream:
        return stream.read()


def deb822_stanzas(path: Path) -> list[dict[str, str]]:
    """The stanzas of a deb822 index, without their volatile fields, sorted.

    The file lists of a Release file are kept as the sorted paths they list, without hash nor size.
    """
    stanzas = []
    for stanza in Deb822.iter_paragraphs(read_bytes(path).decode().splitlines()):
        kept = {
            key: normalize_text(value)
            for key, value in stanza.items()
            if key.lower() not in DEB_VOLATILE
        }
        if path.name == 'Release':
            kept |= {
                f'{key} paths': '\n'.join(
                    sorted(line.split()[-1] for line in value.splitlines() if line.strip())
                )
                for key, value in stanza.items()
                if key.lower() in RELEASE_FILE_LISTS
            }
        stanzas.append(kept)
    return sorted(stanzas, key=lambda s: sorted(s.items()))


def deb_indexes(root: Path) -> dict[str, object]:
    """Release files, package and source indexes, and Contents files of an apt repository.

    An index is read from its uncompressed form when aptly wrote one, else from a compressed one.
    """
    result: dict[str, object] = {}
    for path in sorted(root.rglob('*')):
        if not path.is_file():
            continue
        rel = normalize_path(str(path.relative_to(root)))
        stem = path.name.removesuffix(path.suffix) if path.suffix in OPENERS else path.name
        if path.name == 'Release':
            result[rel] = deb822_stanzas(path)
        elif stem in DEB_INDEXES:
            uncompressed = path.with_name(stem)
            if path == uncompressed or not uncompressed.exists():
                result[normalize_path(str(uncompressed.relative_to(root)))] = deb822_stanzas(path)
        elif stem.startswith('Contents-'):
            result[rel] = sorted(read_bytes(path).decode().splitlines())
    return result


def rpm_entries(element: ET.Element | None) -> list[dict[str, str]]:
    """The rpm:entry children of a provides/requires element, sorted."""
    if element is None:
        return []
    return sorted(
        (dict(e.attrib) for e in element.findall('rpm:entry', NS)), key=lambda e: sorted(e.items())
    )


def rpm_metadata(repodata: Path) -> dict[str, object]:
    """The packages a repomd.xml describes: versions, provides, requires and files."""
    repomd = ET.parse(repodata / 'repomd.xml').getroot()
    locations = {
        data.get('type'): data.find('repo:location', NS) for data in repomd.findall('repo:data', NS)
    }

    def load(kind: str) -> ET.Element:
        location = locations[kind]
        assert location is not None, f'{repodata}: no location for {kind}'
        return ET.fromstring(read_bytes(repodata.parent / location.attrib['href']))

    packages: dict[str, dict[str, object]] = {}
    for pkg in load('primary').findall('common:package', NS):
        version = pkg.find('common:version', NS)
        assert version is not None
        fmt = pkg.find('common:format', NS)
        name = pkg.findtext('common:name', namespaces=NS)
        key = f'{name}.{pkg.findtext("common:arch", namespaces=NS)}'
        packages[key] = {
            'version': dict(version.attrib),
            'provides': rpm_entries(None if fmt is None else fmt.find('rpm:provides', NS)),
            'requires': rpm_entries(None if fmt is None else fmt.find('rpm:requires', NS)),
            'files': [],
        }
    for pkg in load('filelists').findall('filelists:package', NS):
        key = f'{pkg.get("name")}.{pkg.get("arch")}'
        packages[key]['files'] = sorted(f.text or '' for f in pkg.findall('filelists:file', NS))
    return dict(sorted(packages.items()))


def published_key(root: Path) -> Path | None:
    """The public key a repository publishes for its users, if any."""
    return next((p for pattern in KEY_FILES for p in sorted(root.glob(pattern))), None)


def key_packets(key: Path) -> list[list[str]]:
    """The key, subkey, fingerprint and user id records of a key file, as gpg lists them.

    A file gpg cannot read gives [['unreadable']]: a corrupt published key is a difference too.
    """
    with TemporaryDirectory() as home:
        listing = subprocess.run(
            ['gpg', '--homedir', home, '--batch', '--with-colons', '--show-keys', str(key)],
            capture_output=True,
            text=True,
        )
    if listing.returncode != 0:
        return [['unreadable']]
    records = []
    for line in listing.stdout.splitlines():
        fields = line.split(':')
        if fields[0] in ('pub', 'sec', 'sub', 'ssb'):
            records.append([fields[0], fields[3], fields[2]])  # algorithm, length
        elif fields[0] in ('fpr', 'uid'):
            records.append([fields[0], fields[9]])
    return records


def verify(signature: Path, key: Path | None, signed: Path | None = None) -> str:
    """'VALIDSIG <fingerprint>' when signature verifies against key, else 'not verified'.

    A clear-signed file (signed is None) also says whether its payload is the file next to it
    named Release, as an InRelease must be.
    """
    if key is None:
        return 'not verified'
    with TemporaryDirectory() as tmp:
        keyring = Path(tmp) / 'keyring.gpg'
        subprocess.run(
            [
                'gpg',
                '--homedir',
                tmp,
                '--batch',
                '--yes',
                '--output',
                str(keyring),
                '--dearmor',
                str(key),
            ]
            if key.read_bytes().startswith(b'-----BEGIN')
            else ['cp', str(key), str(keyring)],
            check=True,
            capture_output=True,
        )
        payload = Path(tmp) / 'payload'
        status = subprocess.run(
            [
                'gpgv',
                '--homedir',
                tmp,
                '--status-fd',
                '1',
                '--keyring',
                str(keyring),
                *(['--output', str(payload)] if signed is None else []),
                str(signature),
                *([str(signed)] if signed is not None else []),
            ],
            capture_output=True,
        ).stdout.decode(errors='replace')  # a notation value, sequoia's salt, may be binary
        fingerprints = [
            line.split()[2] for line in status.splitlines() if line.startswith('[GNUPG:] VALIDSIG ')
        ]
        if not fingerprints:
            return 'not verified'
        verdict = 'VALIDSIG ' + ' '.join(fingerprints)
        release = signature.with_name('Release')
        if signed is None and signature.name == 'InRelease':
            same = release.exists() and payload.read_bytes() == release.read_bytes()
            verdict += ', payload is Release' if same else ', payload differs from Release'
        return verdict


def signatures(root: Path, files: list[Path]) -> dict[str, str]:
    """Each repository signature, checked against the key the repository publishes."""
    key = published_key(root)
    result = {}
    for path in files:
        rel = normalize_path(str(path.relative_to(root)))
        if path.name == 'InRelease' or path.suffix == '.dsc':
            result[rel] = verify(path, key)
        elif path.name == 'Release.gpg':
            result[rel] = verify(path, key, path.with_name('Release'))
        elif path.name == 'repomd.xml.asc':
            result[rel] = verify(path, key, path.with_name('repomd.xml'))
    return result


def normalize_repository(root: Path) -> dict[str, object]:
    """What two builds of the same packages into root must have in common."""
    files = sorted(p for p in root.rglob('*') if p.is_file())
    return {
        'tree': sorted(normalize_path(str(p.relative_to(root))) for p in files),
        'deb': deb_indexes(root),
        'rpm': {
            normalize_path(str(p.parent.relative_to(root))): rpm_metadata(p.parent)
            for p in files
            if p.name == 'repomd.xml'
        },
        'text': {
            normalize_path(str(p.relative_to(root))): normalize_text(p.read_text())
            for p in files
            if p.suffix == '.repo' or p.name in TEXT_FILES
        },
        'signatures': signatures(root, files),
        'keys': {
            str(p.relative_to(root)): key_packets(p)
            for pattern in KEY_FILES
            for p in sorted(root.glob(pattern))
        },
    }
