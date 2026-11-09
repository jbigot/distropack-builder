# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The built repositories install in clean containers, with their signatures checked.

This checks that apt and dnf accept the repositories as their users configure them, signatures
checked, and refuse them once tampered with.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from support import FIXTURE_RELEASES

# The systems each repository is installed on: Enterprise Linux's, built on Rocky Linux, on Rocky
# Linux, AlmaLinux and Red Hat's freely redistributable UBI, which shows it installs on RHEL.
HOSTS = [
    ('debian', 'docker.io/library/debian:trixie'),
    ('ubuntu', 'docker.io/library/ubuntu:noble'),
    ('fedora', 'registry.fedoraproject.org/fedora:44'),
    ('el', 'docker.io/rockylinux/rockylinux:10'),
    ('el', 'docker.io/library/almalinux:10'),
    ('el', 'registry.access.redhat.com/ubi10/ubi'),
]
IMAGES = dict(HOSTS)  # the first host of each distribution
DEB_SCRIPT = """set -e
printf '%s\\n' 'Types: deb' 'URIs: file:///repo' 'Suites: {release}' 'Components: main' \\
    'Signed-By: /repo/hello-archive-keyring.gpg' > /etc/apt/sources.list.d/hello.sources
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y hello-app
cat /usr/share/hello-app/greeting
"""
RPM_SCRIPT = """set -e
sed -e 's|^baseurl=.*|baseurl=file:///repo/{release}|' \\
    -e 's|^gpgkey=.*|gpgkey=file:///repo/hello.key|' \\
    /repo/{release}/hello.repo > /etc/yum.repos.d/hello.repo
grep -qx gpgcheck=1 /etc/yum.repos.d/hello.repo
grep -qx repo_gpgcheck=1 /etc/yum.repos.d/hello.repo
dnf install -y hello-app
cat /usr/share/hello-app/greeting
"""


def install(
    dist: str, repository: Path, image: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Install hello-app from repository in a clean container of image, dist's first host."""
    release = FIXTURE_RELEASES[dist][0]
    script = (DEB_SCRIPT if dist in ('debian', 'ubuntu') else RPM_SCRIPT).format(release=release)
    return subprocess.run(
        [
            'docker',
            'run',
            '--rm',
            '--mount',
            f'type=bind,source={repository},target=/repo,readonly',
            image or IMAGES[dist],
            'sh',
            '-c',
            script,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


@pytest.mark.e2e
@pytest.mark.parametrize(('dist', 'image'), HOSTS, ids=[image for _, image in HOSTS])
def test_install_from_repository(fixture_build: Path, dist: str, image: str) -> None:
    result = install(dist, fixture_build / 'repositories' / dist, image)
    assert result.returncode == 0, result.stdout
    assert result.stdout.rstrip().endswith('Hello from hello-lib'), result.stdout


@pytest.mark.e2e
@pytest.mark.parametrize('dist', sorted(FIXTURE_RELEASES))
def test_tampered_repository_is_refused(fixture_build: Path, tmp_path: Path, dist: str) -> None:
    repository = Path(shutil.copytree(fixture_build / 'repositories' / dist, tmp_path / dist))
    release = FIXTURE_RELEASES[dist][0]
    if dist in ('debian', 'ubuntu'):
        signed = repository / 'dists' / release / 'InRelease'
        old, new = 'Label: Hello test', 'Label: Hello tesT'
    else:
        signed = repository / release / 'repodata' / 'repomd.xml'
        old, new = '</repomd>', ' </repomd>'
    assert old in signed.read_text()
    signed.write_text(signed.read_text().replace(old, new))
    result = install(dist, repository)
    assert result.returncode != 0, result.stdout
