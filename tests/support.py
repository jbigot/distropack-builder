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
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import yaml

from distro_releases import Release, distribution
from distropack_builder import sources

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / 'tests' / 'characterization' / 'data'
# The recorded release data, shared with distro-releases' own tests.
RELEASE_DATA = REPO_ROOT / 'packages' / 'distro-releases' / 'tests' / 'data'
# The modules load_script reloads, so that each test starts from fresh module state.
MODULES = (
    'distropack_builder.targets',
    'distropack_builder.bake',
    'distropack_builder.inputs',
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
def fixture_releases(environ: Mapping[str, str]) -> dict[str, list[str]]:
    """The releases the end-to-end tests build the fixture for, by distribution.

    One per family, openSUSE's two aside; with DISTROPACK_NIGHTLY=1 (the nightly run), a second
    one each, so that one packaging is built for two releases of every family. The first is the
    install tests'.
    """
    releases = {
        'debian': ['trixie'],
        'ubuntu': ['noble'],
        'fedora': ['44'],
        'el': ['10'],
        'opensuse': ['16.0', 'tumbleweed'],
    }
    if environ.get('DISTROPACK_NIGHTLY') == '1':
        # bookworm, in Debian's LTS, keeps the extended support's images exercised
        second = {'debian': 'bookworm', 'ubuntu': 'jammy', 'fedora': '43', 'el': '9'}
        for dist, release in second.items():
            releases[dist].append(release)
    return releases


FIXTURE_RELEASES = fixture_releases(os.environ)


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
    # no file name in the gzip header: the same bytes whatever the target is called
    with target.open('wb') as file, gzip.GzipFile('', 'wb', fileobj=file, mtime=0) as gz:
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


# The snapshot package of the tests of snapshot channels.
SNAP = 'hello-snap'


def use_channels(project: Path, release_channel: str = 'stable', snapshot: str = 'weekly') -> None:
    """Declare the channels: release_channel, and the snapshot channel, epoch_bump 1."""
    conf_path = project / 'build.conf'
    conf = yaml.safe_load(conf_path.read_text())
    conf['channels'] = {
        release_channel: {'kind': 'release'},
        snapshot: {'kind': 'snapshot', 'epoch_bump': 1},
    }
    if 'fedora' in conf['distribs']:
        conf['distribs']['fedora']['repository']['channels'] = {
            snapshot: {
                'path': f'repositories/{snapshot}.fedora',
                'url': f'http://repo.example.invalid/{snapshot}.fedora',
            }
        }
    conf_path.write_text(yaml.safe_dump(conf))


def add_snapshot_package(
    project: Path,
    snapshot: str = 'weekly',
    orig_base: str = 'https://orig.test',
    *,
    autosetup: bool = False,
) -> None:
    """hello-snap: hello-app as a quilt source, built in snapshot from hello-lib's upstream main.

    Its rpm spec unpacks the snapshot's <name>-<commit> directory, as a snapshot spec does.
    """
    target = project / SNAP
    shutil.copytree(FIXTURE_PROJECT / 'hello-app', target)
    for path in target.rglob('*'):
        if path.is_file():
            path.write_text(path.read_text().replace('hello-app', SNAP))
    (target / 'hello-app.spec').rename(target / f'{SNAP}.spec')
    changelog = target / 'debian' / 'changelog'
    changelog.write_text(changelog.read_text().replace(f'{SNAP} (1.0)', f'{SNAP} (1.0-1)', 1))
    (target / 'debian' / 'source' / 'format').write_text('3.0 (quilt)\n')
    (target / 'debian' / 'watch').write_text((project / 'hello-lib/debian/watch').read_text())
    # its release-channel builds download hello-lib's tarball under its own name
    lib_line = (project / 'hello-lib' / 'sources').read_text()
    (target / 'sources').write_text(lib_line.replace('hello-lib-1.0', f'{SNAP}-1.0'))
    spec = target / f'{SNAP}.spec'
    spec.write_text(
        spec.read_text().replace(
            '%description', f'Source0: {orig_base}/{SNAP}-%{{version}}.tar.gz\n\n%description', 1
        )
    )
    if autosetup:  # unpack the snapshot's tarball, as a spec building snapshots does
        spec.write_text(
            spec.read_text().replace(
                '%build',
                f'%prep\n%autosetup -n {SNAP}-%{{?commit}}%{{!?commit:%{{version}}}}\n\n%build',
                1,
            )
        )
    conf_path = project / 'build.conf'
    conf = yaml.safe_load(conf_path.read_text())
    conf['packages'][SNAP] = {
        'channels': {
            snapshot: {
                'git': f'file://{project}/upstream/hello-lib.git',
                'ref': 'main',
                'verify': 'none',
            }
        }
    }
    conf_path.write_text(yaml.safe_dump(conf))


def add_upstream_commit(bare: Path, name: str) -> str:
    """Commit a file name to the main branch of the bare repository bare; the new commit."""
    env = {
        'GIT_AUTHOR_NAME': 'upstream',
        'GIT_AUTHOR_EMAIL': 'upstream@example.invalid',
        'GIT_COMMITTER_NAME': 'upstream',
        'GIT_COMMITTER_EMAIL': 'upstream@example.invalid',
        'GIT_CONFIG_GLOBAL': '/dev/null',
        'GIT_CONFIG_NOSYSTEM': '1',
        'PATH': os.environ['PATH'],
    }
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch) / 'work'

        def git(*args: str, cwd: Path = work) -> str:
            return subprocess.run(
                ['git', *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
            ).stdout.strip()

        git('clone', '-q', str(bare), str(work), cwd=Path(scratch))
        (work / name).write_text(name)
        git('add', name)
        git('commit', '-q', '-m', name)
        git('push', '-q', 'origin', 'main')
        return git('rev-parse', 'HEAD')


def make_project(
    target: Path, orig_base: str, versions: dict[str, list[str]], packages: list[str]
) -> Path:
    """Copy the fixture project to target, limited to some releases and packages.

    Every *.in file of the copy is rendered with @ORIG_BASE@ set to orig_base. Each upstream
    tree <name>-<version> becomes the bare git repository upstream/<name>.git, tagged v<version>,
    which the deb packages' watch files read, and the sources line of <name>-<version>.tar.gz, the
    tarball the tests serve (write_tarball), is written to <name>/sources.
    """
    project_dir = Path(
        shutil.copytree(FIXTURE_PROJECT, target, ignore=shutil.ignore_patterns('upstream'))
    )
    for upstream in (FIXTURE_PROJECT / 'upstream').iterdir():
        name, _, version = upstream.name.rpartition('-')
        write_git_upstream(upstream, project_dir / 'upstream' / f'{name}.git', f'v{version}')
        # the line of the tarball the tests serve, which the fake uscan writes too
        with tempfile.TemporaryDirectory() as scratch:
            tarball = Path(scratch) / f'{upstream.name}.tar.gz'
            write_tarball(upstream, tarball)
            (project_dir / name / 'sources').write_text(sources.line(tarball.name, tarball) + '\n')
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
