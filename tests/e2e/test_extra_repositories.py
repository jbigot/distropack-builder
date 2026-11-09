# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""A project whose package depends on another project's, from that project's repository."""

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from distropack_builder.signing import Signer
from support import FIXTURE_PASSPHRASE

Project = Callable[[dict[str, list[str]], list[str]], Path]
RunPkgbuild = Callable[[Path], subprocess.CompletedProcess[str]]

INSTALL = {
    'debian': (
        'docker.io/library/debian:trixie',
        """set -e
for repo in lib ext; do
  printf '%s\\n' 'Types: deb' "URIs: file:///repo-$repo" 'Suites: trixie' 'Components: main' \\
      "Signed-By: /repo-$repo/hello-archive-keyring.gpg" > /etc/apt/sources.list.d/$repo.sources
done
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y hello-ext
cat /usr/share/hello-ext/greeting
""",
    ),
    'fedora': (
        'registry.fedoraproject.org/fedora:44',
        """set -e
for repo in lib ext; do
  sed -e "s|^\\[hello\\]|[$repo]|" -e "s|^baseurl=.*|baseurl=file:///repo-$repo/44|" \\
      -e "s|^gpgkey=.*|gpgkey=file:///repo-$repo/hello.key|" /repo-$repo/44/hello.repo \\
      > /etc/yum.repos.d/$repo.repo
done
dnf install -y hello-ext
cat /usr/share/hello-ext/greeting
""",
    ),
}


@pytest.mark.e2e
@pytest.mark.parametrize(('dist', 'release'), [('debian', 'trixie'), ('fedora', '44')])
def test_package_from_another_project(
    fixture_build: Path,
    project: Project,
    run_pkgbuild: RunPkgbuild,
    dist: str,
    release: str,
) -> None:
    """hello-ext build-depends on hello-lib, which only the fixture project's repository has."""
    other = project({dist: [release]}, ['hello-ext'])
    signer = Signer.load(other / 'key.gpg', FIXTURE_PASSPHRASE)
    (other / 'lib.asc').write_bytes(signer.public_key(armored=True))
    published = fixture_build / 'repositories' / dist
    url = f'file://{published}/' if dist == 'debian' else f'file://{published}/{{version}}'
    conf = yaml.safe_load((other / 'build.conf').read_text())
    conf['distribs'][dist]['extra_repositories'] = [{'url': url, 'key': 'lib.asc', 'name': 'lib'}]
    (other / 'build.conf').write_text(yaml.safe_dump(conf))
    result = run_pkgbuild(other)
    assert result.returncode == 0, result.stdout
    image, script = INSTALL[dist]
    installed = subprocess.run(
        [
            'docker',
            'run',
            '--rm',
            '--mount',
            f'type=bind,source={published},target=/repo-lib,readonly',
            '--mount',
            f'type=bind,source={other / "repositories" / dist},target=/repo-ext,readonly',
            image,
            'sh',
            '-c',
            script,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert installed.returncode == 0, installed.stdout
    assert installed.stdout.rstrip().endswith('Hello from hello-lib'), installed.stdout
