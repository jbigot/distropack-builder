# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""What a repository publishes, read from its indexes, and the versions a publication may add.

The same reader serves both sides of the guard: the repository at its URL, and the one just
written in a directory. A package about to be published must sort strictly above the version
published, or be that version with the same bytes (a package reused as it was): otherwise users
who have it would never get the new bytes, or would be offered a lower version.
"""

import functools
import gzip
import hashlib
import lzma
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from debian.deb822 import Packages, Sources
from debian.debian_support import Version as DebVersion

from distro_releases import Release
from distropack_builder import repodata
from distropack_builder.versions import rpm_evr_compare

_REPO = '{http://linux.duke.edu/metadata/repo}'


class PublishedError(Exception):
    """A repository's indexes could not be read, other than for not existing yet."""


@dataclass(frozen=True)
class Version:
    """A package as an index publishes it: its version, and its file's sha256."""

    version: str
    sha256: str


@dataclass(frozen=True)
class Offence:
    """A package whose version is not above the one published, or is with other bytes."""

    name: str
    arch: str
    version: str
    published: str

    def __str__(self) -> str:
        """`<package> <version>: <published version> is already published`."""
        return f'{self.name} {self.version}: {self.published} is already published'


Versions = dict[tuple[str, str], tuple[Version, ...]]


def _read(source: str | Path, relative: str, client: httpx.Client) -> bytes | None:
    """The file at relative under source, a URL or a directory; None when it does not exist."""
    if isinstance(source, Path):
        path = source / relative
        return path.read_bytes() if path.is_file() else None
    url = f'{source.rstrip("/")}/{relative}'
    try:
        response = client.get(url)
    except httpx.HTTPError as error:
        raise PublishedError(f'{url}: {error}') from error
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise PublishedError(f'{url}: {response.status_code} {response.reason_phrase}')
    return response.content


def _index(source: str | Path, stem: str, client: httpx.Client) -> str | None:
    """A deb index, the first of its .xz, .gz and plain forms that exists, decompressed."""
    for suffix in ('.xz', '.gz', ''):
        data = _read(source, stem + suffix, client)
        if data is None:
            continue
        if suffix == '.xz':
            data = lzma.decompress(data)
        elif suffix == '.gz':
            data = gzip.decompress(data)
        return data.decode()
    return None


def versions(
    source: str | Path,
    family: Literal['deb', 'rpm'],
    release: Release,
    component: str,
    client: httpx.Client,
) -> Versions:
    """The packages source publishes for release (and component, for deb), by (name, arch).

    A deb source package is keyed `(name, 'source')`, its sha256 its .dsc's. A repository or a
    release that does not exist yet publishes nothing.

    Raises:
        PublishedError: an index could not be fetched (but for a 404) or read.
    """
    try:
        listed = _versions(source, family, release, component, client)
    except PublishedError:
        raise
    except Exception as error:  # noqa: BLE001 -- a malformed index, however it breaks the parser
        raise PublishedError(f'{source}: an index of {release} cannot be read: {error}') from error
    found: dict[tuple[str, str], list[Version]] = {}
    for key, version in listed:
        found.setdefault(key, []).append(version)
    return {key: tuple(versions) for key, versions in found.items()}


def _versions(
    source: str | Path,
    family: Literal['deb', 'rpm'],
    release: Release,
    component: str,
    client: httpx.Client,
) -> list[tuple[tuple[str, str], Version]]:
    """The packages source publishes, a (key, version) pair each, in the order of its indexes."""
    found: list[tuple[tuple[str, str], Version]] = []
    if family == 'deb':
        base = f'dists/{release.codename}/{component}'
        packages = _index(source, f'{base}/binary-amd64/Packages', client)
        for paragraph in Packages.iter_paragraphs(packages or '', use_apt_pkg=False):
            key = (paragraph['Package'], paragraph['Architecture'])
            DebVersion(paragraph['Version'])  # raises on what is no version
            found.append((key, Version(paragraph['Version'], paragraph['SHA256'])))
        sources = _index(source, f'{base}/source/Sources', client)
        for stanza in Sources.iter_paragraphs(sources or '', use_apt_pkg=False):
            dsc = [f for f in stanza.get('Checksums-Sha256', []) if f['name'].endswith('.dsc')]
            if dsc:
                DebVersion(stanza['Version'])
                found.append(
                    ((stanza['Package'], 'source'), Version(stanza['Version'], dsc[0]['sha256']))
                )
        return found
    directory = f'{release.version}/repodata'
    repomd = _read(source, f'{directory}/repomd.xml', client)
    if repomd is None:
        return found
    try:
        root = ET.fromstring(repomd)
    except ET.ParseError as error:
        raise PublishedError(f'{source}/{directory}/repomd.xml: {error}') from error
    entry = root.find(f"{_REPO}data[@type='primary']")
    location = None if entry is None else entry.find(f'{_REPO}location')
    checksum = None if entry is None else entry.find(f'{_REPO}checksum')
    if entry is None or location is None:
        raise PublishedError(f'{source}/{directory}/repomd.xml lists no primary metadata')
    href = location.get('href', '')
    data = _read(source, f'{release.version}/{href}', client)
    if data is None:
        raise PublishedError(f'{source}/{release.version}/{href}: missing')
    if checksum is not None and checksum.text:
        digest = hashlib.new(checksum.get('type', 'sha256'), data).hexdigest()
        if digest != checksum.text.strip():
            raise PublishedError(f'{source}/{release.version}/{href}: not what repomd.xml lists')
    with tempfile.TemporaryDirectory(prefix='distropack-primary.') as scratch:
        primary = Path(scratch) / Path(href).name
        primary.write_bytes(data)
        for name, arch, evr, sha256 in repodata.read_primary(primary):
            found.append(((name, arch), Version(evr, sha256)))
    return found


def compare(family: str, a: str, b: str) -> int:
    """-1, 0 or 1 as version a sorts before, as or after b, in family's order."""
    if family == 'rpm':
        return rpm_evr_compare(a, b)
    first, second = DebVersion(a), DebVersion(b)
    return (first > second) - (first < second)


def offences(
    new: Mapping[tuple[str, str], Sequence[Version]],
    old: Mapping[tuple[str, str], Sequence[Version]],
    family: str,
) -> list[Offence]:
    """The packages of new below the highest version old publishes, or equal to one but its bytes.

    A package old does not publish passes; one old publishes and new no longer has is no offence.
    """
    found = []
    for key, candidates in sorted(new.items()):
        published = old.get(key, ())
        if not published:
            continue
        highest = functools.reduce(
            lambda a, b: a if compare(family, a.version, b.version) >= 0 else b, published
        )
        for candidate in candidates:
            if compare(family, candidate.version, highest.version) < 0:
                found.append(Offence(key[0], key[1], candidate.version, highest.version))
                continue
            same = [p for p in published if compare(family, candidate.version, p.version) == 0]
            if any(p.sha256 != candidate.sha256 for p in same):
                found.append(Offence(key[0], key[1], candidate.version, same[0].version))
    return found
