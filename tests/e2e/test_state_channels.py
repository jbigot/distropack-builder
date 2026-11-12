# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""A snapshot channel, end to end: built from upstream commits on top of the release channel.

hello-snap is hello-app as a 3.0 (quilt) source, built in channel weekly from hello-lib's
upstream git repository, its spec unpacking the snapshot's `<name>-<commit>` directory; its
build dependency hello-lib comes from the stable channel's state.
"""

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from support import (
    FIXTURE_PASSPHRASE,
    SNAP,
    add_snapshot_package,
    add_upstream_commit,
    use_channels,
)

RELEASES = {'debian': ['trixie'], 'fedora': ['44']}
Project = Callable[[dict[str, list[str]], list[str]], Path]


class Channels:
    """Runs of the builder on one project with channels stable and weekly, one state directory."""

    def __init__(self, project: Path, env: dict[str, str]) -> None:
        self.project = project
        self.env = env
        self.state = project.parent / 'state'
        self.downloads = project.parent / 'downloads'
        result = self._cli('sources', 'build.conf', 'hello-lib')
        assert result.returncode == 0, result.stdout

    def _cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, '-m', 'distropack_builder', *args],
            cwd=self.project,
            env={**self.env, 'DISTROPACK_GPG_PASSPHRASE': FIXTURE_PASSPHRASE},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def run(self, *args: str, status: int = 0) -> str:
        result = self._cli(
            'run',
            '-j',
            '3',
            '--state-dir',
            str(self.state),
            '--download-dir',
            str(self.downloads),
            *args,
            'build.conf',
        )
        assert result.returncode == status, result.stdout
        return result.stdout

    def fetch(self, *args: str) -> str:
        result = self._cli(
            'fetch',
            '--channel',
            'weekly',
            '--state-dir',
            str(self.state),
            '--download-dir',
            str(self.downloads),
            *args,
            'build.conf',
        )
        assert result.returncode == 0, result.stdout
        return result.stdout

    def weekly(self) -> None:
        """Declare the channels, and hello-snap in weekly."""
        use_channels(self.project)
        add_snapshot_package(self.project, orig_base='http://unused.invalid', autosetup=True)

    def commit(self, name: str) -> str:
        return add_upstream_commit(self.project / 'upstream' / 'hello-lib.git', name)

    def packages(self, component: str) -> str:
        path = self.project / 'repositories/debian/dists/trixie' / component / 'binary-amd64'
        return (path / 'Packages').read_text()

    def manifest(self, channel: str, uid: str) -> bytes:
        return (self.state / channel / uid / 'manifest.json').read_bytes()


@pytest.fixture
def channels(project: Project, pkgbuild_env: dict[str, str], docker_engine: None) -> Channels:
    return Channels(project(RELEASES, ['hello-lib']), pkgbuild_env)


def tree_hash(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob('*') if p.is_file()):
        digest.update(str(path.relative_to(directory)).encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()


def stanza(packages: str, name: str) -> str:
    (found,) = (s for s in packages.split('\n\n') if s.startswith(f'Package: {name}\n'))
    return found


@pytest.mark.e2e
def test_weekly_after_stable(channels: Channels) -> None:
    channels.run()
    stable = {uid: channels.manifest('stable', uid) for uid in ('debian-13-trixie', 'fedora-44')}
    channels.weekly()
    channels.commit('one')
    head = channels.commit('two')
    channels.run('--channel', 'weekly')
    release = (channels.project / 'repositories/debian/dists/trixie/Release').read_text()
    assert 'Components: main weekly' in release
    snap = stanza(channels.packages('weekly'), SNAP)
    assert f'Version: 1:1.0+main.2.g{head[:7]}-1~bpo13' in snap
    assert f'Package: {SNAP}' not in channels.packages('main')
    rpms = sorted(
        p.name for p in (channels.project / 'repositories/weekly.fedora/44').rglob('*.rpm')
    )
    assert any(name.startswith('hello-lib-1.0-1') for name in rpms), rpms
    assert any(name.startswith(f'{SNAP}-1.0+main.2.g{head[:7]}-1') for name in rpms), rpms
    stable_rpms = (channels.project / 'repositories/fedora/44').rglob('*.rpm')
    assert not [p for p in stable_rpms if p.name.startswith(SNAP)]
    assert {uid: channels.manifest('stable', uid) for uid in stable} == stable


@pytest.mark.e2e
def test_pending_directory_in_weekly_only(channels: Channels) -> None:
    channels.run()
    channels.weekly()
    channels.commit('one')
    pending = channels.project / f'{SNAP}-weekly'
    subprocess.run(['cp', '-a', str(channels.project / SNAP), str(pending)], check=True)
    control = pending / 'debian' / 'control'
    marker = 'base-files'  # a dependency only the pending packaging declares
    control.write_text(
        control.read_text().replace(
            'Depends: ${misc:Depends}', f'Depends: ${{misc:Depends}}, {marker}', 1
        )
    )
    channels.run('--channel', 'weekly')
    depends = stanza(channels.packages('weekly'), SNAP).split('\nDepends:', 1)[1].split('\n')[0]
    assert marker in depends
    assert marker not in (channels.project / SNAP / 'debian' / 'control').read_text()


@pytest.mark.e2e
def test_weekly_without_stable_state(channels: Channels) -> None:
    channels.weekly()
    channels.commit('one')
    out = channels.run('--channel', 'weekly', status=1)
    assert 'build the stable channel first' in out


@pytest.mark.e2e
def test_dispatched_older_commit(channels: Channels) -> None:
    channels.run()
    channels.weekly()
    before = {name: tree_hash(channels.project / name) for name in ('hello-lib', SNAP)}
    older = channels.commit('one')
    channels.commit('two')  # the branch moved after the push that names older
    out = channels.fetch('--snapshot', f'{SNAP}={older}')
    assert 'changed=true' in out
    recorded = json.loads((channels.downloads / 'snapshots.json').read_text())
    assert recorded['weekly'][SNAP]['commit'] == older
    channels.run('--channel', 'weekly')
    assert f'+main.1.g{older[:7]}-1~bpo13' in stanza(channels.packages('weekly'), SNAP)
    assert 'changed=false' in channels.fetch('--snapshot', f'{SNAP}={older}')
    assert {name: tree_hash(channels.project / name) for name in before} == before
