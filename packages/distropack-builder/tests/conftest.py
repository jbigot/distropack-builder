# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Fixtures of the unit tests of distropack-builder."""

from pathlib import Path

import pytest

from distropack_builder.signing import Signer
from git_history import commit, git
from support import FIXTURE_PASSPHRASE, FIXTURE_PROJECT


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


@pytest.fixture
def signer() -> Signer:
    return Signer.load(FIXTURE_PROJECT / 'key.gpg', FIXTURE_PASSPHRASE)
