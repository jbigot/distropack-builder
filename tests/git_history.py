# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Upstream git repositories for the tests: commits, tags, signed commits."""

import os
import subprocess
from pathlib import Path

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


def signed_commit(repo: Path, signature: bytes) -> str:
    """A commit object of repo with signature in its gpgsig header."""
    raw = git(repo, 'cat-file', 'commit', 'HEAD').encode()
    head, _, body = raw.partition(b'\n\n')
    lines = signature.decode().rstrip('\n').split('\n')
    gpgsig = 'gpgsig ' + '\n '.join(lines)
    obj = head + b'\n' + gpgsig.encode() + b'\n\n' + body + b'\n'
    return git(repo, 'hash-object', '-t', 'commit', '-w', '--stdin', input=obj)
