# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""A snapshot channel's commits: resolved, checked at their trust level, recorded, archived.

`fetch` resolves each package of a channel to a commit once for a whole run, checks it at the
level its entry asks, and records the result in the download directory's ``snapshots.json``, which
every build of the run reads: all of them build the commit `fetch` checked, even when the branch
moved meanwhile. The file maps each channel to its records, so that channels sharing a download
directory never overwrite each other's choice. What depends on a commit alone, its clone and its
tarball, is stored once and shared by every channel and distribution.
"""

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from distropack_builder import upstream
from distropack_builder.config import BuildConfig, PackageChannel, VerifyKeys
from distropack_builder.versions import Snapshot

PINNED = 'pinned by the packaging'
NOT_VERIFIED = 'not verified'


@dataclass(frozen=True)
class Record:
    """The commit a channel builds of a package, and how it was checked.

    Attributes:
        commit: the commit's full hash.
        base: the version of the highest version tag reachable from it.
        distance: how many commits it is after that tag.
        ref: the branch it was taken from, None for a commit pinned by the packaging.
        level: how it was checked, as the changelog entry says it: ``pinned by the packaging``,
            ``signed by <fingerprint>`` or ``not verified``.
    """

    commit: str
    base: str
    distance: int
    ref: str | None
    level: str

    def snapshot(self) -> Snapshot:
        """The snapshot the record names, for its versions."""
        return Snapshot(self.commit, self.base, self.distance)


class SnapshotFailed(Exception):
    """A package whose commit could not be resolved or did not pass its check."""

    def __init__(self, package: str, reason: str) -> None:
        """The failure of package, for reason."""
        super().__init__(f'{package}: {reason}')
        self.package = package
        self.reason = reason

    def __eq__(self, other: object) -> bool:
        """Failures are equal when they are of the same package for the same reason."""
        if not isinstance(other, SnapshotFailed):
            return NotImplemented
        return (self.package, self.reason) == (other.package, other.reason)

    def __hash__(self) -> int:
        """As the package and the reason."""
        return hash((self.package, self.reason))


Records = dict[str, Record | SnapshotFailed]


def check_given(config: BuildConfig, channel: str, given: Mapping[str, str]) -> None:
    """Refuse commits given for packages whose commit a channel does not choose from a branch.

    Raises:
        ValueError: a package of given does not build in channel, or is pinned by its entry.
    """
    for package in given:
        entry = config.packages.get(package)
        if entry is None or channel not in entry.channels:
            raise ValueError(f'{package}: it does not build in channel {channel}')
        if entry.channels[channel].commit is not None:
            raise ValueError(
                f'{package}: build.conf pins its commit in channel {channel}; no other can be given'
            )


def resolve(config: BuildConfig, channel: str, clones: Path, given: Mapping[str, str]) -> Records:
    """The record of every package of a snapshot channel, or why it has none.

    A package of given builds that commit, which must be on its entry's ref; any other the commit
    of its entry, or the head of its ref. Clones are kept at ``clones/<package>.git``.
    """
    records: Records = {}
    for package in config.channel_packages(channel):
        entry = config.packages[package].channels[channel]
        try:
            records[package] = _resolve(
                config.root, entry, clones / f'{package}.git', given.get(package)
            )
        except subprocess.CalledProcessError as error:
            stderr = error.stderr.decode(errors='replace').strip() if error.stderr else ''
            reason = stderr.splitlines()[-1] if stderr else str(error)
            records[package] = SnapshotFailed(package, reason)
        except (ValueError, OSError, upstream.UnverifiedCommit) as error:
            records[package] = SnapshotFailed(package, str(error))
    return records


def _resolve(root: Path, entry: PackageChannel, clone: Path, given: str | None) -> Record:
    if entry.commit is not None:
        snap = upstream.describe(entry.git, entry.commit, clone)
        return Record(snap.commit, snap.base, snap.distance, None, PINNED)
    assert entry.ref is not None  # the model asks for one of them
    snap = upstream.describe(entry.git, given or entry.ref, clone)
    if given is not None and not upstream.on_branch(clone, snap.commit, entry.ref):
        raise ValueError(f'commit {snap.commit} is not on {entry.ref}')
    if isinstance(entry.verify, VerifyKeys):
        keyring = (root / entry.verify.keys).read_bytes()
        level = f'signed by {upstream.commit_signer(clone, snap.commit, keyring)}'
    else:
        level = NOT_VERIFIED
    return Record(snap.commit, snap.base, snap.distance, entry.ref, level)


def write(path: Path, channel: str, records: Mapping[str, Record | SnapshotFailed]) -> None:
    """Record the snapshots of channel in path, keeping those of the other channels."""
    data: dict[str, object] = {}
    try:
        loaded = json.loads(path.read_text())
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        pass
    data[channel] = {
        name: {'error': record.reason} if isinstance(record, SnapshotFailed) else asdict(record)
        for name, record in records.items()
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f'.{path.name}.partial')
    partial.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    os.replace(partial, path)


def read(path: Path, channel: str) -> Records | None:
    """The snapshots of channel recorded in path; None when none are, or the file is unreadable."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get(channel), dict):
        return None
    records: Records = {}
    for name, value in data[channel].items():
        if not isinstance(value, dict):
            return None
        if set(value) == {'error'} and isinstance(value['error'], str):
            records[name] = SnapshotFailed(name, value['error'])
            continue
        try:
            record = Record(**value)
        except TypeError:
            return None
        if not (
            isinstance(record.commit, str)
            and isinstance(record.base, str)
            and isinstance(record.distance, int)
            and isinstance(record.ref, str | None)
            and isinstance(record.level, str)
        ):
            return None
        records[name] = record
    return records


def tarball(download_dir: Path, package: str, record: Record) -> Path:
    """The tarball of a package's commit, top directory ``<package>-<commit>``, made once.

    It depends on the commit alone, so every channel and distribution uses the same bytes: the
    gzip is deterministic. The clone is the one `resolve` made under ``download_dir/git``.
    """
    top = f'{package}-{record.commit}'
    made = download_dir / 'snapshots' / f'{top}.tar.gz'
    if made.is_file():
        return made
    made.parent.mkdir(parents=True, exist_ok=True)
    tar = made.with_name(f'.{top}.tar')
    partial = made.with_name(f'.{top}.tar.gz.partial')
    try:
        upstream.archive(download_dir / 'git' / f'{package}.git', record.commit, top, tar)
        upstream.repack(tar, partial, top)
        os.replace(partial, made)
    finally:
        tar.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)
    return made
