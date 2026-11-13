# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""tests/fixture_project.py, which writes the Action workflow's fixture project."""

import json
import subprocess
import sys
from pathlib import Path

import yaml

from support import REPO_ROOT, SNAP


def write(dest: Path, *options: str) -> None:
    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / 'tests' / 'fixture_project.py'),
            str(dest),
            'http://127.0.0.1:8765',
            '--releases',
            'debian:trixie',
            '--packages',
            'hello-lib',
            *options,
        ],
        check=True,
        capture_output=True,
    )


def test_channels_and_commits(tmp_path: Path) -> None:
    write(tmp_path, '--channels', 'stable-1-1', 'weekly-1-1', '--snapshot-commits', '2')
    conf = yaml.safe_load((tmp_path / 'project' / 'build.conf').read_text())
    assert conf['channels'] == {
        'stable-1-1': {'kind': 'release'},
        'weekly-1-1': {'kind': 'snapshot', 'epoch_bump': 1},
    }
    assert list(conf['packages'][SNAP]['channels']) == ['weekly-1-1']
    commits = json.loads((tmp_path / 'commits.json').read_text())
    bare = tmp_path / 'project' / 'upstream' / 'hello-lib.git'
    listed = subprocess.run(
        ['git', '-C', str(bare), 'rev-list', 'main'], check=True, capture_output=True, text=True
    ).stdout.split()
    assert len(commits) == 2 and listed[:2] == commits[::-1]  # oldest first
    served = tmp_path / 'served'
    assert (served / f'{SNAP}-1.0.tar.gz').read_bytes() == (
        served / 'hello-lib-1.0.tar.gz'
    ).read_bytes()


def test_no_channels(tmp_path: Path) -> None:
    write(tmp_path)
    conf = yaml.safe_load((tmp_path / 'project' / 'build.conf').read_text())
    assert 'channels' not in conf
    assert not (tmp_path / 'commits.json').exists()
