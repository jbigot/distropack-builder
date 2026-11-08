# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""sphinx-polyversion's configuration: the documentation of main and of every builder release.

    uv run --group docs sphinx-polyversion docs/poly.py <output directory>

Each version is built from its own checkout, in a virtual environment synced from that version's
uv.lock, so that its API reference shows its own docstrings. The output holds one directory per
version (`main`, and `X.Y.Z` for the tag `distropack-builder-vX.Y.Z`) and an `index.html`
opening the highest release, or `main` before the first one.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from packaging.version import Version
from sphinx_polyversion.api import apply_overrides
from sphinx_polyversion.driver import DefaultDriver
from sphinx_polyversion.git import Git, GitRef, GitRefType
from sphinx_polyversion.pyvenv import VirtualPythonEnvironment
from sphinx_polyversion.sphinx import SphinxBuilder

BRANCH = 'main'
TAG_PREFIX = 'distropack-builder-v'

# What the command line can override (sphinx-polyversion's `out`, `--local`, `--sequential`).
OUTPUT_DIR = 'site'
MOCK = False
SEQUENTIAL = False
apply_overrides(globals())

ROOT = Path(__file__).resolve().parent.parent


def _has_local_branch() -> bool:
    command = ['git', 'rev-parse', '--verify', '--quiet', f'refs/heads/{BRANCH}']
    return subprocess.run(command, cwd=ROOT, stdout=subprocess.DEVNULL).returncode == 0


class Versions(Git):
    """The release tags of the builder, and main: the local branch, else origin's.

    sphinx-polyversion's own filter compares every ref's remote with the one it is given, so
    that reading origin's branches would drop the tags, which have no remote.
    """

    def __init__(self) -> None:
        """Select the tags of distropack-builder's family and the branch main."""
        super().__init__(BRANCH, rf'{TAG_PREFIX}\d+\.\d+\.\d+')
        self._branch_remote = None if _has_local_branch() else 'origin'

    async def predicate(self, root: Path, ref: GitRef) -> bool:
        """Whether ref is a version of the site."""
        if ref.type_ == GitRefType.TAG:
            return bool(self.tag_regex.fullmatch(ref.name))
        return ref.name == BRANCH and ref.remote == self._branch_remote


def version_name(ref: GitRef) -> str:
    """The name of ref's directory: X.Y.Z for a release tag, the branch's name otherwise."""
    return ref.name.removeprefix(TAG_PREFIX)


def ordered(refs: Any) -> list[str]:
    """The names of refs, main first, then the releases from the highest down."""
    names = [version_name(ref) for ref in refs]
    branch = [name for name in names if name == BRANCH]
    releases = sorted((name for name in names if name != BRANCH), key=Version, reverse=True)
    return branch + releases


class UvEnvironment(VirtualPythonEnvironment):
    """A version's virtual environment, synced from that version's uv.lock with the docs group."""

    def __init__(self, path: Path, name: str) -> None:
        """The environment of the version name, checked out in path, in path/.venv."""
        super().__init__(path, name, venv=path / '.venv')
        self.version = name

    async def create_venv(self) -> None:
        """Sync the version's workspace into its own virtual environment.

        The checkout has no .git for hatch-vcs to read the packages' version from: it is the
        release's, or 0+main.
        """
        version = '0+main' if self.version == BRANCH else self.version
        env = os.environ | {
            'UV_PROJECT_ENVIRONMENT': str(self.venv),
            'SETUPTOOLS_SCM_PRETEND_VERSION': version,
        }
        process = await asyncio.create_subprocess_exec(
            'uv', 'sync', '-q', '--locked', '--group', 'docs', cwd=self.path, env=env
        )
        if await process.wait():
            raise RuntimeError(f'uv sync failed for {self.version}')


def data(
    driver: DefaultDriver[GitRef, UvEnvironment, Any], ref: GitRef, env: Any
) -> dict[str, Any]:
    """What a version's pages know: every version, theirs, and the commit they are built from."""
    return {
        'versions': ordered(driver.targets),
        'current': version_name(ref),
        'commit': ref.obj,
    }


def root_data(driver: DefaultDriver[GitRef, UvEnvironment, Any]) -> dict[str, Any]:
    """What the root page knows: the version it opens."""
    names = ordered(driver.targets)
    releases = [name for name in names if name != BRANCH]
    return {'latest': releases[0] if releases else BRANCH}


class Driver(DefaultDriver[GitRef, UvEnvironment, Any]):
    """sphinx-polyversion's driver, keeping the versions that fail instead of only logging them."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """sphinx-polyversion's DefaultDriver, with no version failed yet."""
        super().__init__(*args, **kwargs)
        self.failed: list[str] = []

    def build_failed(self, rev: GitRef, exc_info: Any) -> None:
        """Log the failure, as sphinx-polyversion does, and remember the version."""
        super().build_failed(rev, exc_info)
        self.failed.append(version_name(rev))


driver = Driver(
    ROOT,
    OUTPUT_DIR,
    vcs=Versions(),
    builder=SphinxBuilder(Path('docs'), args=['-W', '--keep-going', '-n']),
    env=UvEnvironment,
    namer=version_name,
    data_factory=data,
    root_data_factory=root_data,
    template_dir=ROOT / 'docs' / '_root',
    mock=MOCK,
)
driver.run(MOCK, SEQUENTIAL)
if driver.failed:
    sys.exit(f'the documentation of {", ".join(sorted(driver.failed))} failed to build')
