"""Reduce a published repository to what must stay identical from one build to the next.

Two builds of the same packages differ in what is volatile by nature: the `.pdidev.<seconds>`
suffix of deb versions, dates, checksums, sizes, signatures, and the hash prefixes of the rpm
metadata files. normalize_repository() keeps everything else: the files published, the package
stanzas, the releases, the dependencies, the file lists, the generated README and .repo files.
"""

import bz2
import gzip
import lzma
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import IO

import zstandard
from debian.deb822 import Deb822

PDIDEV_VERSION = re.compile(r'\.pdidev\.\d+')
REPODATA_HASH = re.compile(r'(^|/)[0-9a-f]{64}-')
DEB_INDEXES = ('Packages', 'Sources')
DEB_VOLATILE = {'md5sum', 'sha1', 'sha256', 'sha512', 'size', 'files', 'checksums-sha1',
                'checksums-sha256', 'checksums-sha512', 'date', 'valid-until'}
TEXT_FILES = ('index.html', 'README.html')
NS = {
    'repo': 'http://linux.duke.edu/metadata/repo',
    'common': 'http://linux.duke.edu/metadata/common',
    'rpm': 'http://linux.duke.edu/metadata/rpm',
    'filelists': 'http://linux.duke.edu/metadata/filelists',
}
OPENERS: dict[str, Callable[[Path], IO[bytes]]] = {
    '.gz': lambda p: gzip.open(p),
    '.bz2': lambda p: bz2.open(p),
    '.xz': lambda p: lzma.open(p),
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
    """The stanzas of a deb822 index, without their volatile fields, sorted."""
    stanzas = [
        {key: normalize_text(value) for key, value in stanza.items()
         if key.lower() not in DEB_VOLATILE}
        for stanza in Deb822.iter_paragraphs(read_bytes(path).decode().splitlines())
    ]
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
    return sorted((dict(e.attrib) for e in element.findall('rpm:entry', NS)),
                  key=lambda e: sorted(e.items()))


def rpm_metadata(repodata: Path) -> dict[str, object]:
    """The packages a repomd.xml describes: versions, provides, requires and files."""
    repomd = ET.parse(repodata / 'repomd.xml').getroot()
    locations = {data.get('type'): data.find('repo:location', NS)
                 for data in repomd.findall('repo:data', NS)}

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
        key = f"{name}.{pkg.findtext('common:arch', namespaces=NS)}"
        packages[key] = {
            'version': dict(version.attrib),
            'provides': rpm_entries(None if fmt is None else fmt.find('rpm:provides', NS)),
            'requires': rpm_entries(None if fmt is None else fmt.find('rpm:requires', NS)),
            'files': [],
        }
    for pkg in load('filelists').findall('filelists:package', NS):
        key = f"{pkg.get('name')}.{pkg.get('arch')}"
        packages[key]['files'] = sorted(f.text or '' for f in pkg.findall('filelists:file', NS))
    return dict(sorted(packages.items()))


def normalize_repository(root: Path) -> dict[str, object]:
    """What two builds of the same packages into root must have in common."""
    files = sorted(p for p in root.rglob('*') if p.is_file())
    return {
        'tree': sorted(normalize_path(str(p.relative_to(root))) for p in files),
        'deb': deb_indexes(root),
        'rpm': {normalize_path(str(p.parent.relative_to(root))): rpm_metadata(p.parent)
                for p in files if p.name == 'repomd.xml'},
        'text': {normalize_path(str(p.relative_to(root))): normalize_text(p.read_text())
                 for p in files if p.suffix == '.repo' or p.name in TEXT_FILES},
    }
