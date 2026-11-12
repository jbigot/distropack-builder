# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The publication guards end to end: the fixture's site served as the published one.

A site built from the same versions with other bytes is refused; a new version assembles into a
size-limited site that installs as an upgrade of the published one; a snapshot channel installs
beside the release channel.
"""

import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from support import FIXTURE_PASSPHRASE, add_snapshot_package, add_upstream_commit, use_channels

DEBIAN = {'debian': ['trixie']}
Project = Callable[[dict[str, list[str]], list[str]], Path]


@dataclass(frozen=True)
class Published:
    """The fixture's built site, served as the published one: its URL from here, and from a
    container."""

    url: str
    from_containers: str


@pytest.fixture
def published(fixture_build: Path) -> Iterator[Published]:
    """The fixture's site, served by a busybox httpd container, its port published on every
    interface of the host.

    The host reaches it on 127.0.0.1; a container, on any network of the engine, through the
    host's own address, as it reaches any published site (with a rootless engine, the host's
    loopback is not the containers').
    """
    name = f'distropack-test-published-{time.monotonic_ns()}'
    subprocess.run(
        [
            'docker',
            'run',
            '-d',
            '--name',
            name,
            '-p',
            '0.0.0.0::8080',
            '--mount',
            f'type=bind,source={fixture_build},target=/site,readonly',
            'docker.io/library/busybox:stable',
            'httpd',
            '-f',
            '-p',
            '8080',
            '-h',
            '/site',
        ],
        check=True,
        capture_output=True,
    )
    try:
        port = (
            subprocess.run(
                ['docker', 'port', name, '8080'], check=True, capture_output=True, text=True
            )
            .stdout.splitlines()[0]
            .rsplit(':', 1)[-1]
            .strip()
        )
        address = subprocess.run(
            ['hostname', '-I'], check=True, capture_output=True, text=True
        ).stdout.split()[0]
        yield Published(f'http://127.0.0.1:{port}', f'http://{address}:{port}')
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True)


def cli(
    env: dict[str, str], project: Path, *args: str, conf_last: bool = True
) -> subprocess.CompletedProcess[str]:
    """The builder's command args in project, build.conf after them (or already among them)."""
    return subprocess.run(
        [sys.executable, '-m', 'distropack_builder', *args, *(['build.conf'] if conf_last else [])],
        cwd=project,
        env={**env, 'DISTROPACK_GPG_PASSPHRASE': FIXTURE_PASSPHRASE},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def ok(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, result.stdout
    return result.stdout


def published_at(project: Path, url: str) -> None:
    """The project's Debian repository is the one url publishes."""
    conf_path = project / 'build.conf'
    conf = yaml.safe_load(conf_path.read_text())
    conf['distribs']['debian']['repository']['url'] = f'{url}/repositories/debian'
    conf_path.write_text(yaml.safe_dump(conf))


def new_entry(project: Path, package: str, version: str) -> None:
    changelog = project / package / 'debian' / 'changelog'
    entry = (
        f'{package} ({version}) unstable; urgency=medium\n\n  * New version.\n\n'
        ' -- distropack test <test@example.invalid>  Fri, 06 Nov 2026 12:00:00 +0000\n\n'
    )
    changelog.write_text(entry + changelog.read_text())


def sources(env: dict[str, str], project: Path) -> None:
    ok(cli(env, project, 'sources', 'build.conf', 'hello-lib', conf_last=False))


@pytest.mark.e2e
def test_same_versions_other_bytes_refused(
    project: Project, published: Published, pkgbuild_env: dict[str, str], tmp_path: Path
) -> None:
    """A lost state: the same versions built again, their sources signed anew."""
    directory = project(DEBIAN, ['hello-lib', 'hello-app'])
    published_at(directory, published.url)
    sources(pkgbuild_env, directory)
    states = str(tmp_path / 'states')
    ok(cli(pkgbuild_env, directory, 'run', '--state-dir', states, '--no-publish'))
    site = tmp_path / 'site'
    result = cli(pkgbuild_env, directory, 'site', '--output', str(site), '--state-dir', states)
    assert result.returncode == 1, result.stdout
    assert 'is already published' in result.stdout
    assert not (site / 'repositories' / 'debian').exists()


@pytest.mark.e2e
def test_new_entry_upgrades_cleanly(
    project: Project, published: Published, pkgbuild_env: dict[str, str], tmp_path: Path
) -> None:
    directory = project(DEBIAN, ['hello-lib', 'hello-app'])
    published_at(directory, published.url)
    new_entry(directory, 'hello-lib', '1.0-2')
    new_entry(directory, 'hello-app', '1.1')
    sources(pkgbuild_env, directory)
    states = str(tmp_path / 'states')
    ok(cli(pkgbuild_env, directory, 'run', '--state-dir', states, '--no-publish'))
    site = tmp_path / 'site'
    ok(
        cli(
            pkgbuild_env,
            directory,
            'site',
            '--output',
            str(site),
            '--state-dir',
            states,
            '--max-size',
            '50M',
        )
    )
    out = ok(
        cli(
            pkgbuild_env,
            directory,
            'check-install',
            '--site',
            str(site),
            '--previous',
            f'debian={published.from_containers}/repositories/debian',
        )
    )
    assert 'install check passed: Debian GNU/Linux 13 (Trixie), channel stable' in out


@pytest.mark.e2e
def test_stable_and_weekly_install(
    project: Project, pkgbuild_env: dict[str, str], tmp_path: Path
) -> None:
    """apt and dnf install the stable channel alone, and with weekly enabled."""
    releases = {'debian': ['trixie'], 'fedora': ['44']}
    directory = project(releases, ['hello-lib'])
    sources(pkgbuild_env, directory)
    states = str(tmp_path / 'states')
    ok(cli(pkgbuild_env, directory, 'run', '--state-dir', states))
    use_channels(directory)
    add_snapshot_package(directory, orig_base='http://unused.invalid', autosetup=True)
    add_upstream_commit(directory / 'upstream' / 'hello-lib.git', 'one')
    ok(cli(pkgbuild_env, directory, 'run', '--state-dir', states, '--channel', 'weekly'))
    site = tmp_path / 'site'
    ok(cli(pkgbuild_env, directory, 'site', '--output', str(site), '--state-dir', states))
    out = ok(cli(pkgbuild_env, directory, 'check-install', '--site', str(site)))
    for release in ('Debian GNU/Linux 13 (Trixie)', 'Fedora 44'):
        assert f'install check passed: {release}, channel stable' in out, out
        assert f'install check passed: {release}, channel weekly' in out, out
