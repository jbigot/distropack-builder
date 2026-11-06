"""Fixtures of the tests of the repository as a whole."""

from pathlib import Path

import pytest
from workspace_helpers import git, synthetic_repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A synthetic repository of the working tree, with two commits so that HEAD~1 exists."""
    path = synthetic_repo(tmp_path / 'repo')
    git(path, 'commit', '-q', '--allow-empty', '-m', 'Second commit')
    return path
