# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Upstream snapshots from git: which version a commit is, its tarball, who signed it.

The host's git binary does the work, as dpkg-buildpackage does a build's: git is a tool, not a
library to replace. A clone is blobless and bare, and reused from run to run.
"""

import gzip
import re
import subprocess
import tarfile
from pathlib import Path

from debian.debian_support import Version

from distropack_builder import signing
from distropack_builder.versions import Snapshot

# A tag naming a version: 1.12.0, v1.12.0.
_VERSION_TAG = re.compile(r'^v?(?P<version>[0-9][0-9A-Za-z.+~]*)$')
_SSH = b'-----BEGIN SSH SIGNATURE-----'


class UnverifiedCommit(Exception):
    """A commit whose signature is missing, of another kind, or by none of the keys trusted."""

    def __init__(self, sha: str, reason: str) -> None:
        """The failure of commit sha, for reason."""
        super().__init__(f'commit {sha} is not verified: {reason}')
        self.sha = sha


def _git(directory: Path, *args: str) -> bytes:
    return subprocess.run(
        ['git', '-C', str(directory), *args], capture_output=True, check=True
    ).stdout


def _clone(git_url: str, clone_dir: Path) -> None:
    """A blobless bare clone of git_url at clone_dir, made or brought up to date."""
    if (clone_dir / 'HEAD').is_file():
        _git(
            clone_dir,
            'fetch',
            '--quiet',
            '--tags',
            '--force',
            'origin',
            '+refs/heads/*:refs/heads/*',
        )
        return
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ['git', 'clone', '--quiet', '--bare', '--filter=blob:none', git_url, str(clone_dir)],
        capture_output=True,
        check=True,
    )


def describe(git_url: str, ref: str, clone_dir: Path) -> Snapshot:
    """The snapshot ref of git_url is: its commit, the highest version tag below it, the distance.

    The highest, not the nearest: a later patch release of an older series, merged back, must not
    win over the newer series.

    Raises:
        ValueError: no version tag is reachable from ref.
    """
    _clone(git_url, clone_dir)
    commit = _git(clone_dir, 'rev-parse', f'{ref}^{{commit}}').decode().strip()
    tags = _git(clone_dir, 'tag', '--merged', commit).decode().split()
    versions = {
        tag: found['version'] for tag in tags if (found := _VERSION_TAG.match(tag)) is not None
    }
    if not versions:
        raise ValueError(f'no version tag is reachable from {ref} in {git_url}')
    tag = max(versions, key=lambda name: Version(versions[name]))
    distance = int(_git(clone_dir, 'rev-list', '--count', f'{tag}..{commit}').decode())
    return Snapshot(commit, versions[tag], distance)


def archive(clone_dir: Path, commit: str, prefix: str, dst: Path) -> None:
    """A tar archive at dst of commit's tree, its members under prefix/, as uscan's git mode makes.

    A blobless clone fetches the blobs it lacks from its origin.
    """
    _git(clone_dir, 'archive', '--format=tar', f'--prefix={prefix}/', '-o', str(dst), commit)


def repack(src: Path, dst: Path, top_dir: str) -> None:
    """A gzip tarball of src's members, its top directory renamed top_dir, byte-deterministic.

    The members keep their order and metadata; the gzip header holds no time nor file name, so
    that the same input gives the same bytes, whenever and wherever it is repacked.
    """
    with tarfile.open(src) as listing:
        tops = {member.name.split('/', 1)[0] for member in listing.getmembers()}
        loose = [m.name for m in listing.getmembers() if '/' not in m.name and not m.isdir()]
    if len(tops) != 1 or loose:
        raise ValueError(f'{src} has no single top-level directory to rename')
    with (
        tarfile.open(src) as source,
        dst.open('wb') as raw,
        gzip.GzipFile('', 'wb', fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as target,
    ):
        for member in source.getmembers():
            _, _, rest = member.name.partition('/')
            member.name = f'{top_dir}/{rest}' if rest else top_dir
            if member.islnk():  # a hard link names its target from the archive's root
                _, _, linked = member.linkname.partition('/')
                member.linkname = f'{top_dir}/{linked}'
            data = source.extractfile(member) if member.isfile() else None
            target.addfile(member, data)


def commit_signer(clone_dir: Path, sha: str, keyring: bytes) -> str:
    """The fingerprint of the key of keyring that signed commit sha of clone_dir's repository.

    The signature is the commit's gpgsig header, over the commit object without it, as git signs.

    Raises:
        UnverifiedCommit: the commit is not signed, signed with SSH, or by no key of keyring.
    """
    raw = _git(clone_dir, 'cat-file', 'commit', sha)
    header, separator, message = raw.partition(b'\n\n')
    kept: list[bytes] = []
    signature: list[bytes] = []
    lines = header.split(b'\n')
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(b'gpgsig '):
            signature.append(line.removeprefix(b'gpgsig '))
            index += 1
            while index < len(lines) and lines[index].startswith(b' '):
                signature.append(lines[index][1:])
                index += 1
            continue
        kept.append(line)
        index += 1
    if not signature:
        raise UnverifiedCommit(sha, 'it is not signed')
    armored = b'\n'.join(signature) + b'\n'
    if armored.startswith(_SSH):
        raise UnverifiedCommit(
            sha, 'it has an SSH signature, which only OpenPGP keys can check here'
        )
    payload = b'\n'.join(kept) + separator + message
    try:
        return signing.verify_detached(payload, armored, keyring)
    except ValueError as error:
        raise UnverifiedCommit(sha, f'no key of the keyring signed it ({error})') from error


def on_branch(clone_dir: Path, sha: str, ref: str) -> bool:
    """Whether commit sha is ref or one of its ancestors."""
    result = subprocess.run(
        ['git', '-C', str(clone_dir), 'merge-base', '--is-ancestor', sha, ref], capture_output=True
    )
    return result.returncode == 0
