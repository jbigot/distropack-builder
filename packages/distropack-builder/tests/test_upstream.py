# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Upstream snapshots from git: describe, repack, commit signatures, branches."""

import io
import os
import subprocess
import tarfile
import time
from pathlib import Path

import pysequoia
import pytest

from distropack_builder import upstream
from distropack_builder.signing import Signer
from distropack_builder.versions import Snapshot
from support import FIXTURE_PASSPHRASE, FIXTURE_PROJECT

DATA = Path(__file__).parent / 'data'
ENV = {
    'GIT_AUTHOR_NAME': 't',
    'GIT_AUTHOR_EMAIL': 't@example.invalid',
    'GIT_COMMITTER_NAME': 't',
    'GIT_COMMITTER_EMAIL': 't@example.invalid',
    'GIT_CONFIG_GLOBAL': '/dev/null',
    'GIT_CONFIG_NOSYSTEM': '1',
}


def git(repo: Path, *args: str, input: bytes | None = None) -> str:
    result = subprocess.run(
        ['git', '-C', str(repo), *args],
        env={**os.environ, **ENV},
        input=input,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


def commit(repo: Path, message: str) -> str:
    (repo / message).write_text(message)  # a file per commit: the branches merge cleanly
    git(repo, 'add', message)
    git(repo, 'commit', '-q', '-m', message)
    return git(repo, 'rev-parse', 'HEAD')


@pytest.fixture
def history(tmp_path: Path) -> Path:
    """main: 1.11.0, 1.12.0, two commits; a branch from 1.11.0 tagged 1.11.1, merged back."""
    repo = tmp_path / 'upstream'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    commit(repo, 'one')
    git(repo, 'tag', '1.11.0')
    git(repo, 'switch', '-q', '-c', 'stable')
    commit(repo, 'fix')
    git(repo, 'tag', '1.11.1')
    git(repo, 'switch', '-q', 'main')
    commit(repo, 'two')
    git(repo, 'tag', '1.12.0')
    commit(repo, 'three')
    git(repo, 'merge', '-q', '--no-edit', 'stable')
    commit(repo, 'four')
    git(repo, 'tag', 'not-a-version')
    return repo


def test_describe(history: Path, tmp_path: Path) -> None:
    """The highest version tag reachable, not the nearest one: 1.11.1 is nearer but lower."""
    head = git(history, 'rev-parse', 'HEAD')
    snap = upstream.describe(f'file://{history}', 'main', tmp_path / 'clone')
    assert snap == Snapshot(head, '1.12.0', 4)
    # the clone is reused
    again = upstream.describe(f'file://{history}', 'main', tmp_path / 'clone')
    assert again == snap


def test_describe_without_a_version_tag(tmp_path: Path) -> None:
    repo = tmp_path / 'bare'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    commit(repo, 'one')
    with pytest.raises(ValueError, match='main'):
        upstream.describe(f'file://{repo}', 'main', tmp_path / 'clone')


def tarball(path: Path, top: str, members: dict[str, bytes]) -> None:
    with tarfile.open(path, 'w:xz') as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(f'{top}/{name}')
            info.size, info.mtime, info.mode = len(data), 1700000000, 0o644
            tar.addfile(info, io.BytesIO(data))


def test_repack(tmp_path: Path) -> None:
    src = tmp_path / 'src.tar.xz'
    tarball(src, 'pdi-abcdef0', {'a': b'A', 'b/c': b'C'})
    first, second = tmp_path / 'one.tar.gz', tmp_path / 'two.tar.gz'
    upstream.repack(src, first, 'pdi-1.12.0+main.4.gabcdef0')
    time.sleep(1.1)
    upstream.repack(src, second, 'pdi-1.12.0+main.4.gabcdef0')
    assert first.read_bytes() == second.read_bytes()  # deterministic, whatever the time
    header = first.read_bytes()[:10]
    assert header[3] & 0x08 == 0  # no file name
    assert header[4:8] == b'\0\0\0\0'  # no time
    with tarfile.open(first) as tar:
        assert [m.name for m in tar.getmembers()] == [
            'pdi-1.12.0+main.4.gabcdef0/a',
            'pdi-1.12.0+main.4.gabcdef0/b/c',
        ]
        assert [m.mtime for m in tar.getmembers()] == [1700000000, 1700000000]


def signed_commit(repo: Path, signature: bytes) -> str:
    """A commit object of repo with signature in its gpgsig header."""
    raw = git(repo, 'cat-file', 'commit', 'HEAD').encode()
    head, _, body = raw.partition(b'\n\n')
    lines = signature.decode().rstrip('\n').split('\n')
    gpgsig = 'gpgsig ' + '\n '.join(lines)
    obj = head + b'\n' + gpgsig.encode() + b'\n\n' + body + b'\n'
    return git(repo, 'hash-object', '-t', 'commit', '-w', '--stdin', input=obj)


def payload(repo: Path) -> bytes:
    raw = git(repo, 'cat-file', 'commit', 'HEAD').encode()
    return raw + b'\n'


@pytest.fixture
def signer() -> Signer:
    return Signer.load(FIXTURE_PROJECT / 'key.gpg', FIXTURE_PASSPHRASE)


def test_commit_signer(history: Path, signer: Signer) -> None:
    sha = signed_commit(history, signer.detached(payload(history)))
    assert upstream.commit_signer(history, sha, signer.public_key(armored=True)) == (
        signer.fingerprint
    )


def test_commit_signed_by_another_key(history: Path, signer: Signer) -> None:
    sha = signed_commit(history, signer.detached(payload(history)))
    other = pysequoia.Tsk.generate('Other <other@example.invalid>').extract_certificate()
    with pytest.raises(upstream.UnverifiedCommit, match=sha):
        upstream.commit_signer(history, sha, str(other).encode())


def test_unsigned_commit(history: Path, signer: Signer) -> None:
    sha = git(history, 'rev-parse', 'HEAD')
    with pytest.raises(upstream.UnverifiedCommit, match='not signed'):
        upstream.commit_signer(history, sha, signer.public_key(armored=True))


def test_ssh_signed_commit(history: Path, signer: Signer) -> None:
    ssh = b'-----BEGIN SSH SIGNATURE-----\nU1NIU0lH\n-----END SSH SIGNATURE-----\n'
    sha = signed_commit(history, ssh)
    with pytest.raises(upstream.UnverifiedCommit, match='SSH'):
        upstream.commit_signer(history, sha, signer.public_key(armored=True))


def test_web_flow_merge(tmp_path: Path) -> None:
    """A merge GitHub signed on pdi's main (6466cd1c), against GitHub's web-flow keyring."""
    repo = tmp_path / 'pdi'
    repo.mkdir()
    git(repo, 'init', '-q', '--bare')
    raw = (DATA / 'web-flow-commit.txt').read_bytes()
    sha = git(repo, 'hash-object', '-t', 'commit', '-w', '--literally', '--stdin', input=raw)
    assert sha == '6466cd1c37f885a8b3868a2f1c5a9a3a931aab0a'
    keyring = (DATA / 'web-flow.gpg').read_bytes()
    # GitHub's current web-flow key, the second of its keyring
    assert upstream.commit_signer(repo, sha, keyring) == '968479A1AFF927E37D1A566BB5690EEEBB952194'


def test_on_branch(history: Path) -> None:
    fix = git(history, 'rev-parse', '1.11.1')
    two = git(history, 'rev-parse', '1.12.0')
    assert upstream.on_branch(history, fix, 'main')
    assert not upstream.on_branch(history, two, 'stable')
