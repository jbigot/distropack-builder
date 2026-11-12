# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The state of a release: the packages of its last builds, and what they were built from.

`<root>/<channel>/<release uid>/` holds `manifest.json` and one directory per package with the
files of its last build. A release's state is replaced whole, through `<dir>.new`, so that a run
interrupted half-way leaves the previous one: the old state is set aside as `<dir>.old` while the
new one takes its place, and read from there if the run stopped in between. A manifest that
cannot be read, of another schema, or naming a file missing, is no state for what it covers:
never a crash, never a reuse.
"""

import hashlib
import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from os import link
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

# Bumped when what static_key hashes changes, so that every stored key stops matching.
KEY_VERSION = 1
SCHEMA = 1
# The channel of the releases until there are others (Phase 13): the state's first path component.
RELEASE_CHANNEL = 'stable'
MANIFEST = 'manifest.json'


def static_key(packaging: Path, sources: Mapping[str, Path], channel: str) -> str:
    """The key of what a build is made from, but its environment: packaging, sources, channel.

    A sha256 over KEY_VERSION, the channel, every file under packaging (its relative path and
    bytes, sorted by path) and the sha256 of each source file with its name; not the times.
    A packaging that is a file (an rpm spec) counts as the one file named after itself.
    """
    digest = hashlib.sha256(f'distropack-state {KEY_VERSION}\0{channel}\0'.encode())
    if packaging.is_file():
        files, top = [packaging], packaging.parent
    else:
        files, top = sorted(p for p in packaging.rglob('*') if p.is_file()), packaging
    for path in files:
        relative = path.relative_to(top).as_posix().encode()
        content = path.read_bytes()
        digest.update(b'file\0' + relative + b'\0' + str(len(content)).encode() + b'\0' + content)
    for name in sorted(sources):
        source = hashlib.sha256(sources[name].read_bytes()).hexdigest()
        digest.update(b'source\0' + name.encode() + b'\0' + source.encode() + b'\0')
    return digest.hexdigest()


def packaging_key(packaging: Path) -> str:
    """The key of packaging alone, a directory or a spec file: no sources, no channel."""
    return static_key(packaging, {}, '')


class _Entry(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    static_key: str
    env_key: str
    base_version: str
    rebuild: int
    version: str
    snapshot_commit: str | None
    packaging_hash: str
    files: list[str]
    source_files: list[str]
    arch_all_files: list[str]


class _Manifest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    schema_: int
    packages: dict[str, _Entry]

    @classmethod
    def read(cls, path: Path) -> '_Manifest | None':
        """The manifest at path, or None for anything else than a manifest of SCHEMA."""
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get('schema') != SCHEMA:
            return None
        try:
            return cls.model_validate({'schema_': data['schema'], 'packages': data.get('packages')})
        except ValidationError:
            return None

    def dump(self) -> str:
        """The manifest as JSON."""
        packages = {name: entry.model_dump() for name, entry in self.packages.items()}
        return json.dumps({'schema': self.schema_, 'packages': packages}, indent=2, sort_keys=True)


@dataclass(frozen=True)
class StoredPackage:
    """A package of a release's state: its last build's files and what it was built from.

    Attributes:
        name: the package, as build.conf names it.
        static_key: the static key it was built from.
        env_key: the environment key of its build.
        base_version: its base version, without the rebuild number.
        rebuild: its rebuild number.
        version: its full version.
        snapshot_commit: the commit of a snapshot channel's build, None on the release channel.
        packaging_hash: the sha256 of its packaging directory alone.
        directory: where its files are.
        source_files: its source package: the .dsc and the files it lists, or the .src.rpm.
        arch_all_files: its Architecture: all debs, which a binNMU does not rebuild.
    """

    name: str
    static_key: str
    env_key: str
    base_version: str
    rebuild: int
    version: str
    snapshot_commit: str | None
    packaging_hash: str
    directory: Path
    source_files: tuple[str, ...]
    arch_all_files: tuple[str, ...]


class ReleaseState:
    """The state of one release of one channel, read."""

    def __init__(self, root: Path, channel: str, release_uid: str) -> None:
        """The state of release_uid on channel, under root."""
        self._directory = root / channel / release_uid
        if not self._directory.exists():
            # a commit interrupted between its two renames: the previous state is set aside
            self._directory = self._directory.with_name(release_uid + '.old')
        self._manifest = _Manifest.read(self._directory / MANIFEST)

    def exists(self) -> bool:
        """Whether the release has a state that can be read."""
        return self._manifest is not None

    def package(self, name: str) -> StoredPackage | None:
        """The stored package name, None when absent or when a file of it is missing."""
        if self._manifest is None or name not in self._manifest.packages:
            return None
        entry = self._manifest.packages[name]
        directory = self._directory / name
        if not all((directory / file).is_file() for file in entry.files):
            return None
        return StoredPackage(
            name,
            entry.static_key,
            entry.env_key,
            entry.base_version,
            entry.rebuild,
            entry.version,
            entry.snapshot_commit,
            entry.packaging_hash,
            directory,
            tuple(entry.source_files),
            tuple(entry.arch_all_files),
        )

    def packages(self) -> dict[str, StoredPackage]:
        """Every package stored whole, by name."""
        if self._manifest is None:
            return {}
        found = {name: self.package(name) for name in self._manifest.packages}
        return {name: package for name, package in found.items() if package is not None}


class ReleaseStateWriter:
    """A new state of one release, written aside, then committed whole or aborted."""

    def __init__(self, root: Path, channel: str, release_uid: str) -> None:
        """A writer of release_uid's state on channel, under root, starting empty."""
        self._directory = root / channel / release_uid
        self._new = self._directory.with_name(self._directory.name + '.new')
        shutil.rmtree(self._new, ignore_errors=True)
        self._new.mkdir(parents=True)
        self._entries: dict[str, _Entry] = {}

    def add(
        self,
        name: str,
        *,
        static_key: str,
        env_key: str,
        base_version: str,
        rebuild: int,
        version: str,
        snapshot_commit: str | None,
        packaging_hash: str,
        outdir: Path,
        source_files: Sequence[str],
        arch_all_files: Sequence[str],
    ) -> None:
        """Store the build of name: every file of outdir, hard-linked where possible."""
        directory = self._new / name
        directory.mkdir()
        files = []
        for path in sorted(p for p in outdir.iterdir() if p.is_file()):
            try:
                link(path, directory / path.name)
            except OSError:
                shutil.copy2(path, directory / path.name)
            files.append(path.name)
        self._entries[name] = _Entry(
            static_key=static_key,
            env_key=env_key,
            base_version=base_version,
            rebuild=rebuild,
            version=version,
            snapshot_commit=snapshot_commit,
            packaging_hash=packaging_hash,
            files=files,
            source_files=list(source_files),
            arch_all_files=list(arch_all_files),
        )

    def commit(self) -> None:
        """Write the manifest, then make the new state the release's, replacing the old one."""
        manifest = _Manifest(schema_=SCHEMA, packages=self._entries)
        (self._new / MANIFEST).write_text(manifest.dump() + '\n')
        old = self._directory.with_name(self._directory.name + '.old')
        shutil.rmtree(old, ignore_errors=True)
        if self._directory.exists():
            self._directory.rename(old)
        self._new.rename(self._directory)
        shutil.rmtree(old, ignore_errors=True)

    def abort(self) -> None:
        """Drop the new state: the release keeps its previous one."""
        shutil.rmtree(self._new, ignore_errors=True)
