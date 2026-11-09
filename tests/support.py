# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Constants shared by the test files and the fixtures of conftest.py."""

import gzip
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from datetime import date
from pathlib import Path

import yaml

from distro_releases import Release, distribution

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / 'tests' / 'characterization' / 'data'
# The recorded release data, shared with distro-releases' own tests.
RELEASE_DATA = REPO_ROOT / 'packages' / 'distro-releases' / 'tests' / 'data'
# The modules load_script reloads, so that each test starts from fresh module state.
MODULES = (
    'distropack_builder.targets',
    'distropack_builder.bake',
    'distropack_builder.builders',
    'distropack_builder.cli',
)
# The names the tests pass to load_script, from before the move, and the modules they load.
SCRIPT_MODULES = {
    'build': 'distropack_builder.bake',
    'pkgbuild': 'distropack_builder.cli',
}
FROZEN_DATE = date(2026, 11, 5)
FIXTURE_PROJECT = REPO_ROOT / 'tests' / 'e2e' / 'project'
FIXTURE_PASSPHRASE = 'test-passphrase'
# The releases the shared fixture build covers: one per family, and Rocky Linux for Phase 10's el.
FIXTURE_RELEASES = {'debian': ['trixie'], 'ubuntu': ['noble'], 'fedora': ['44'], 'rocky': ['10']}


def made_release(dist: str, version: str | None, codename: str | None, order: int) -> Release:
    """A release of dist with only its identity filled in, for tests needing no release data."""
    return Release(
        distribution(dist),
        version,
        codename,
        version or str(codename),
        order,
        None,
        None,
        None,
        None,
        None,
    )


TRIXIE = made_release('debian', '13', 'trixie', 13)
FORKY = made_release('debian', '14', 'forky', 14)
SID = made_release('debian', None, 'sid', 1000)


def write_tarball(source: Path, target: Path) -> None:
    """A reproducible .tar.gz of source, whose single top-level directory is source's name."""

    def reset(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, '', ''
        return info

    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w') as tar:
        for path in [source, *sorted(source.rglob('*'))]:
            tar.add(
                path, arcname=str(path.relative_to(source.parent)), recursive=False, filter=reset
            )
    with gzip.GzipFile(target, 'wb', mtime=0) as gz:
        gz.write(data.getvalue())


def write_git_upstream(source: Path, target: Path, tag: str) -> None:
    """A bare git repository at target, holding source's files in one commit, tagged tag.

    The commit is reproducible: fixed author, committer and dates.
    """
    when = '2026-01-01T00:00:00+00:00'
    env = {
        'GIT_AUTHOR_NAME': 'upstream',
        'GIT_AUTHOR_EMAIL': 'upstream@example.invalid',
        'GIT_AUTHOR_DATE': when,
        'GIT_COMMITTER_NAME': 'upstream',
        'GIT_COMMITTER_EMAIL': 'upstream@example.invalid',
        'GIT_COMMITTER_DATE': when,
        'GIT_CONFIG_GLOBAL': '/dev/null',
        'GIT_CONFIG_NOSYSTEM': '1',
        'PATH': os.environ['PATH'],
    }
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch) / 'work'
        shutil.copytree(source, work)

        def git(*args: str, cwd: Path = work) -> None:
            subprocess.run(['git', *args], cwd=cwd, env=env, check=True, capture_output=True)

        git('init', '-q', '-b', 'main')
        git('add', '-A')
        git('commit', '-q', '-m', 'upstream')
        git('tag', tag)
        git('clone', '-q', '--bare', str(work), str(target), cwd=Path(scratch))


def make_project(
    target: Path, orig_base: str, versions: dict[str, list[str]], packages: list[str]
) -> Path:
    """Copy the fixture project to target, limited to some releases and packages.

    Every *.in file of the copy is rendered with @ORIG_BASE@ set to orig_base. Each upstream
    tree <name>-<version> becomes the bare git repository upstream/<name>.git, tagged v<version>,
    which the deb packages' watch files read.
    """
    project_dir = Path(
        shutil.copytree(FIXTURE_PROJECT, target, ignore=shutil.ignore_patterns('upstream'))
    )
    for upstream in (FIXTURE_PROJECT / 'upstream').iterdir():
        name, _, version = upstream.name.rpartition('-')
        write_git_upstream(upstream, project_dir / 'upstream' / f'{name}.git', f'v{version}')
    for template in project_dir.rglob('*.in'):
        template.with_suffix('').write_text(template.read_text().replace('@ORIG_BASE@', orig_base))
        template.unlink()
    conf_path = project_dir / 'build.conf'
    conf = yaml.safe_load(conf_path.read_text())
    conf['distribs'] = {
        dist: conf['distribs'][dist] | {'versions': rels} for dist, rels in versions.items()
    }
    conf['packages'] = {name: conf['packages'][name] for name in packages}
    conf_path.write_text(yaml.safe_dump(conf))
    return project_dir


def source0(spec: Path) -> str:
    """The URL of the spec's Source0, its %{version} expanded."""
    text = spec.read_text()
    version: str = re.findall(r'^Version:\s*(\S+)', text, re.M)[0]
    url: str = re.findall(r'^Source0:\s*(\S+)', text, re.M)[0]
    return url.replace('%{version}', version)
