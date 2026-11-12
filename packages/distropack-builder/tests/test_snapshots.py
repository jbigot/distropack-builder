# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Resolving a channel's snapshots at their trust level, recording them, their tarballs."""

import shutil
import tarfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from distropack_builder import snapshots
from distropack_builder.config import BuildConfig
from distropack_builder.signing import Signer
from distropack_builder.snapshots import Record, SnapshotFailed
from distropack_builder.versions import Snapshot
from git_history import commit, git, signed_commit


def make_config(root: Path, entries: Mapping[str, Mapping[str, Any]]) -> BuildConfig:
    """A configuration with channels s (release) and w (snapshot), each package's w entry given."""
    return BuildConfig.model_validate(
        {
            'channels': {'s': {'kind': 'release'}, 'w': {'kind': 'snapshot'}},
            'distribs': {},
            'packages': {name: {'channels': {'w': entry}} for name, entry in entries.items()},
            'root': root,
        }
    )


def url(repo: Path) -> str:
    return f'file://{repo}'


def test_ref_resolves_to_its_head(history: Path, tmp_path: Path) -> None:
    head = git(history, 'rev-parse', 'HEAD')
    conf = make_config(tmp_path, {'pdi': {'git': url(history), 'ref': 'main', 'verify': 'none'}})
    records = snapshots.resolve(conf, 'w', tmp_path / 'git', {})
    assert records == {'pdi': Record(head, '1.12.0', 4, 'main', 'not verified')}
    record = records['pdi']
    assert isinstance(record, Record)
    assert record.snapshot() == Snapshot(head, '1.12.0', 4)
    assert (tmp_path / 'git' / 'pdi.git' / 'HEAD').is_file()


def test_commit_entry_is_pinned(history: Path, tmp_path: Path) -> None:
    sha = git(history, 'rev-parse', '1.12.0')
    conf = make_config(tmp_path, {'pdi': {'git': url(history), 'commit': sha}})
    records = snapshots.resolve(conf, 'w', tmp_path / 'git', {})
    assert records == {'pdi': Record(sha, '1.12.0', 0, None, 'pinned by the packaging')}


def test_given_commit_wins_over_moved_ref(history: Path, tmp_path: Path) -> None:
    given = git(history, 'rev-parse', 'HEAD')
    commit(history, 'five')  # the branch moved after the dispatch
    conf = make_config(tmp_path, {'pdi': {'git': url(history), 'ref': 'main', 'verify': 'none'}})
    records = snapshots.resolve(conf, 'w', tmp_path / 'git', {'pdi': given})
    assert records == {'pdi': Record(given, '1.12.0', 4, 'main', 'not verified')}


def test_given_commit_off_branch_fails(history: Path, tmp_path: Path) -> None:
    off = git(history, 'rev-parse', 'stable')
    git(history, 'switch', '-q', 'stable')
    off = commit(history, 'only-on-stable')
    git(history, 'switch', '-q', 'main')
    conf = make_config(tmp_path, {'pdi': {'git': url(history), 'ref': 'main', 'verify': 'none'}})
    records = snapshots.resolve(conf, 'w', tmp_path / 'git', {'pdi': off})
    failed = records['pdi']
    assert isinstance(failed, SnapshotFailed)
    assert str(failed) == f'pdi: commit {off} is not on main'


def test_check_given_refuses(history: Path, tmp_path: Path) -> None:
    sha = git(history, 'rev-parse', 'HEAD')
    conf = make_config(
        tmp_path,
        {
            'pdi': {'git': url(history), 'ref': 'main', 'verify': 'none'},
            'pinned': {'git': url(history), 'commit': sha},
        },
    )
    snapshots.check_given(conf, 'w', {'pdi': sha})
    with pytest.raises(ValueError, match='^other: '):
        snapshots.check_given(conf, 'w', {'other': sha})
    with pytest.raises(ValueError, match='^pinned: '):
        snapshots.check_given(conf, 'w', {'pinned': sha})


def sign_head(repo: Path, signer: Signer) -> str:
    raw = git(repo, 'cat-file', 'commit', 'HEAD').encode() + b'\n'
    sha = signed_commit(repo, signer.detached(raw))
    git(repo, 'update-ref', 'refs/heads/main', sha)
    return sha


def test_keys_level_records_fingerprint(history: Path, tmp_path: Path, signer: Signer) -> None:
    sha = sign_head(history, signer)
    (tmp_path / 'keys.asc').write_bytes(signer.public_key(armored=True))
    conf = make_config(
        tmp_path, {'pdi': {'git': url(history), 'ref': 'main', 'verify': {'keys': 'keys.asc'}}}
    )
    record = snapshots.resolve(conf, 'w', tmp_path / 'git', {})['pdi']
    assert isinstance(record, Record)
    assert record.commit == sha
    assert record.level == f'signed by {signer.fingerprint}'


def test_unsigned_head_fails_alone(history: Path, tmp_path: Path, signer: Signer) -> None:
    (tmp_path / 'keys.asc').write_bytes(signer.public_key(armored=True))
    other = tmp_path / 'other'
    shutil.copytree(history, other)
    conf = make_config(
        tmp_path,
        {
            'pdi': {'git': url(history), 'ref': 'main', 'verify': {'keys': 'keys.asc'}},
            'plugin': {'git': url(other), 'ref': 'main', 'verify': 'none'},
        },
    )
    records = snapshots.resolve(conf, 'w', tmp_path / 'git', {})
    failed = records['pdi']
    assert isinstance(failed, SnapshotFailed)
    assert str(failed).startswith('pdi: commit ')
    assert 'not signed' in str(failed)
    assert isinstance(records['plugin'], Record)


def test_records_round_trip_per_channel(tmp_path: Path) -> None:
    path = tmp_path / 'snapshots.json'
    weekly: dict[str, Record | SnapshotFailed] = {
        'pdi': Record('a' * 40, '1.12.0', 4, 'main', 'not verified'),
        'plugin': SnapshotFailed('plugin', 'commit x is not on main'),
    }
    nightly: dict[str, Record | SnapshotFailed] = {
        'pdi': Record('b' * 40, '1.12.0', 5, None, 'pinned by the packaging')
    }
    snapshots.write(path, 'weekly', weekly)
    snapshots.write(path, 'nightly', nightly)
    read = snapshots.read(path, 'weekly')
    assert read is not None
    assert read['pdi'] == weekly['pdi']
    assert isinstance(read['plugin'], SnapshotFailed)
    assert str(read['plugin']) == 'plugin: commit x is not on main'
    assert snapshots.read(path, 'nightly') == nightly
    assert snapshots.read(path, 'other') is None
    assert snapshots.read(tmp_path / 'missing.json', 'weekly') is None


@pytest.mark.parametrize('text', ['', '{', '[]', '{"weekly": []}', '{"weekly": {"pdi": {}}}'])
def test_malformed_records_are_absent(tmp_path: Path, text: str) -> None:
    path = tmp_path / 'snapshots.json'
    path.write_text(text)
    assert snapshots.read(path, 'weekly') is None


def test_tarball_made_once(history: Path, tmp_path: Path) -> None:
    head = git(history, 'rev-parse', 'HEAD')
    download = tmp_path / 'download'
    conf = make_config(tmp_path, {'pdi': {'git': url(history), 'ref': 'main', 'verify': 'none'}})
    record = snapshots.resolve(conf, 'w', download / 'git', {})['pdi']
    assert isinstance(record, Record)
    made = snapshots.tarball(download, 'pdi', record)
    assert made == download / 'snapshots' / f'pdi-{head}.tar.gz'
    mtime = made.stat().st_mtime_ns
    # another channel resolving the same commit, with another label: the same archive
    other = Record(head, '1.12.0', 4, None, 'pinned by the packaging')
    assert snapshots.tarball(download, 'pdi', other) == made
    assert made.stat().st_mtime_ns == mtime
    with tarfile.open(made) as tar:
        names = [m.name for m in tar.getmembers()]
    assert names and all(n == f'pdi-{head}' or n.startswith(f'pdi-{head}/') for n in names)
    elsewhere = tmp_path / 'elsewhere'
    shutil.copytree(download / 'git', elsewhere / 'git')
    again = snapshots.tarball(elsewhere, 'pdi', record)
    assert again.read_bytes() == made.read_bytes()
