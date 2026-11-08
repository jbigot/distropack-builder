# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Helpers for the tests of the repository as a whole: synthetic repositories, archives, wheels."""

import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from email.parser import HeaderParser
from pathlib import Path

from support import REPO_ROOT

# Neither the user's nor the system's git configuration (signing, hooks, default branch) applies.
GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
GIT_IDENTITY = ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid']


def git(repo: Path, *args: str) -> str:
    """Run git in repo with a neutral configuration; its standard output."""
    return subprocess.run(
        ['git', *GIT_IDENTITY, '-C', str(repo), *args],
        env=os.environ | GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def synthetic_repo(dest: Path) -> Path:
    """A one-commit, untagged repository holding the working tree of REPO_ROOT; dest.

    The copy covers the tracked and untracked files git does not ignore, uncommitted changes
    included, minus the tracked files deleted without `git rm`.
    """

    def names(*args: str) -> set[str]:
        return set(git(REPO_ROOT, 'ls-files', '-z', *args).split('\0')) - {''}

    for name in sorted(names('-co', '--exclude-standard') - names('-d')):
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / name, target, follow_symlinks=False)
    git(dest, 'init', '-q', '-b', 'main')
    git(dest, 'add', '-A')
    git(dest, 'commit', '-q', '-m', 'Synthetic copy of the working tree')
    return dest


def tag(repo: Path, name: str, rev: str = 'HEAD') -> None:
    """Put the lightweight tag name on rev."""
    git(repo, 'tag', name, rev)


def git_archive(repo: Path, dest: Path) -> Path:
    """Extract `git archive HEAD` of repo into dest, a tree without .git; dest.

    An enclosing work tree would give its own tags to the version (design §9), so dest must lie
    outside any.
    """
    existing = next(p for p in dest.parents if p.exists())  # git -C fails on a missing directory
    probe = subprocess.run(
        ['git', '-C', str(existing), 'rev-parse', '--show-toplevel'],
        env=os.environ | GIT_ENV,
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:
        raise RuntimeError(
            f'{dest} is inside the git work tree {probe.stdout.strip()}: an archive '
            'extracted there takes its version from that repository'
        )
    archive = subprocess.run(
        ['git', '-C', str(repo), 'archive', '--format=tar', 'HEAD'],
        env=os.environ | GIT_ENV,
        check=True,
        capture_output=True,
    ).stdout
    dest.mkdir(parents=True)
    with tempfile.TemporaryFile() as data:
        data.write(archive)
        data.seek(0)
        with tarfile.open(fileobj=data) as tar:
            tar.extractall(dest, filter='tar')
    return dest


def build_wheel(tree: Path, package: str, out: Path) -> Path:
    """Build the wheel of the workspace member package from tree into out; the wheel's path."""
    return build(tree, package, out, 'wheel')


def build(tree: Path, package: str | None, out: Path, kind: str) -> Path:
    """Build the `kind` distribution (sdist or wheel) of package from tree into out; its path.

    With package None, tree is the project to build (an unpacked sdist).
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(('SETUPTOOLS_SCM_', 'VCS_VERSIONING_'))
    }
    # A configuration field setuptools-scm deprecates fails the build, before a release drops it.
    deprecated = 'error:Configuration field'
    selection = [] if package is None else ['--package', package]
    subprocess.run(
        ['uv', 'build', '-q', f'--{kind}', *selection, '--out-dir', str(out)],
        cwd=tree,
        env=env | GIT_ENV | {'PYTHONWARNINGS': deprecated},
        check=True,
    )
    stem = '*' if package is None else package.replace('-', '_')
    (built,) = out.glob(f'{stem}-*.whl' if kind == 'wheel' else f'{stem}-*.tar.gz')
    return built


def wheel_version(wheel: Path) -> str:
    """The version in the wheel's METADATA, never the file name's, which may carry a local part."""
    with zipfile.ZipFile(wheel) as archive:
        (metadata,) = (n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))
        version = HeaderParser().parsestr(archive.read(metadata).decode())['Version']
    assert version is not None
    return version


def version_of(tree: Path, package: str) -> str:
    """The version the wheel of package gets when built from tree."""
    with tempfile.TemporaryDirectory() as out:
        return wheel_version(build_wheel(tree, package, Path(out)))
