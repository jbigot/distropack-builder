# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The install check's scripts: as the page says, signatures checked, upgrades never removing."""

from pathlib import Path

from distro_releases import Release, distribution
from distropack_builder import install_check
from distropack_builder.install_check import Target

TRIXIE = Release(
    distribution('debian'), '13', 'trixie', '13 (Trixie)', 13, 'stable', None, None, None, None
)
F44 = Release(distribution('fedora'), '44', None, '44', 44, None, None, None, None, None)
SITE = 'http://site:8080/repositories/debian'


def deb(components: tuple[str, ...] = ('main',), previous: str | None = None) -> str:
    target = Target('apt', 'hello', SITE, TRIXIE, components, previous)
    return install_check.script(target, ['hello-app'])


def test_deb_script_keeps_signatures() -> None:
    script = deb()
    assert 'trusted=yes' not in script and 'allow-insecure' not in script.lower()
    assert 'Signed-By: /usr/share/keyrings/hello-archive-keyring.gpg' in script
    assert f'URIs: {SITE}' in script and 'Suites: trixie' in script
    assert 'apt-get install -y hello-app' in script


def test_deb_snapshot_component_has_its_sources_file() -> None:
    script = deb(('main', 'weekly'))
    assert '/etc/apt/sources.list.d/hello-weekly.sources' in script
    assert 'Components: weekly' in script


def test_upgrade_refuses_removals() -> None:
    script = deb(previous='https://repo.example.org/debian')
    assert script.index('URIs: https://repo.example.org/debian') < script.index(f'URIs: {SITE}')
    assert 'apt-get -y --no-remove dist-upgrade' in script
    assert 'apt list --upgradable' in script  # nothing held back once upgraded


def test_rpm_script_checks_signatures() -> None:
    target = Target('dnf', 'hello', 'http://site:8080/repositories/fedora', F44, ('main',), None)
    script = install_check.script(target, ['hello-app', 'hello-lib'])
    assert 'gpgcheck=1' in script and 'repo_gpgcheck=1' in script
    assert 'baseurl=http://site:8080/repositories/fedora/44' in script
    assert 'gpgkey=http://site:8080/repositories/fedora/hello.key' in script
    assert 'dnf install -y hello-app hello-lib' in script
    assert '--nogpgcheck' not in script


def test_rpm_upgrade_never_erases() -> None:
    target = Target(
        'dnf', 'hello', 'http://site:8080/f', F44, ('main',), 'https://repo.example.org/fedora'
    )
    script = install_check.script(target, ['hello-app'])
    assert 'baseurl=https://repo.example.org/fedora/44' in script
    assert 'dnf upgrade -y --refresh' in script and '--allowerasing' not in script
    assert 'dnf check-update' in script


def test_zypper_script() -> None:
    target = Target('zypper', 'hello', 'http://site:8080/o', F44, ('main',), None)
    script = install_check.script(target, ['hello-app'])
    assert 'zypper --non-interactive addrepo' in script
    assert 'gpgcheck=1' in script and 'repo_gpgcheck=1' in script
    assert 'zypper --non-interactive install hello-app' in script


def test_default_packages_are_the_repository_s(tmp_path: Path) -> None:
    dist = tmp_path / 'dists' / 'trixie' / 'weekly' / 'binary-amd64'
    dist.mkdir(parents=True)
    (dist / 'Packages').write_text(
        'Package: hello-app\nArchitecture: amd64\nVersion: 1\nSHA256: a\n\n'
        'Package: hello-app-doc\nArchitecture: all\nVersion: 1\nSHA256: b\n'
    )
    assert install_check.binary_packages(tmp_path, 'deb', TRIXIE, 'weekly') == [
        'hello-app',
        'hello-app-doc',
    ]
