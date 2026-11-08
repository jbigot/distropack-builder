# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The helpers the packaging tests rely on refuse what would make them pass vacuously."""

from pathlib import Path

import pytest

from workspace_helpers import git_archive

pytestmark = pytest.mark.packaging


@pytest.mark.parametrize('dest', ['tree', 'missing/tree'])
def test_archive_refuses_a_work_tree(repo: Path, dest: str) -> None:
    with pytest.raises(RuntimeError, match='inside the git work tree'):
        git_archive(repo, repo / dest)
    assert not (repo / dest).exists()
