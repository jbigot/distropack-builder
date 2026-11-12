# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The builder images report their environment, and rebuild a stored source binary-only."""

import hashlib
import shutil
from datetime import date
from pathlib import Path

import pytest

from distro_releases import Release, distribution
from distropack_builder import builders
from distropack_builder.bake import REGISTRY
from distropack_builder.context import BuildContext
from distropack_builder.docker import Cancellation, Engine, Mount, run
from distropack_builder.rpmquery import SpecInfo
from distropack_builder.signing import Signer
from distropack_builder.versions import binnmu_entry
from support import FIXTURE_PASSPHRASE, FIXTURE_PROJECT

pytestmark = [pytest.mark.e2e, pytest.mark.enable_socket, pytest.mark.usefixtures('docker_engine')]

CONTROL = """Source: tiny
Section: misc
Priority: optional
Maintainer: test <test@example.invalid>
Build-Depends: debhelper-compat (= 13)
Standards-Version: 4.7.0
Rules-Requires-Root: no

Package: tiny
Architecture: any
Depends: ${misc:Depends}
Description: tiny test package
 Tiny.
"""
CHANGELOG = """tiny (1.0) unstable; urgency=medium

  * Initial release.

 -- test <test@example.invalid>  Thu, 05 Nov 2026 12:00:00 +0000
"""
SPEC = """Name: tiny
Version: 1.0
Release: 1%{?dist}
Summary: tiny test package
License: MIT
BuildArch: noarch
BuildRequires: make

%description
Tiny.

%install
mkdir -p %{buildroot}/usr/share/tiny

%files
/usr/share/tiny

%changelog
* Thu Nov 05 2026 test <test@example.invalid> - 1.0-1
- Initial release.
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project holding one native deb package and one spec, tiny."""
    debian = tmp_path / 'project' / 'tiny' / 'debian'
    (debian / 'source').mkdir(parents=True)
    (debian / 'source' / 'format').write_text('3.0 (native)\n')
    (debian / 'control').write_text(CONTROL)
    (debian / 'changelog').write_text(CHANGELOG)
    (debian / 'rules').write_text('#!/usr/bin/make -f\n%:\n\tdh $@\n')
    (debian / 'rules').chmod(0o755)
    (debian.parent / 'tiny.spec').write_text(SPEC)
    return tmp_path / 'project'


def release(dist: str, name: str) -> Release:
    (found,) = (
        r for r in distribution(dist).releases(date.today()) if name in (r.codename, r.version)
    )
    return found


def context(tmp_path: Path) -> BuildContext:
    work = tmp_path / 'work'
    work.mkdir(parents=True, exist_ok=True)
    return BuildContext(Engine.connect(), run, Cancellation(), work, 2)


def signer() -> Signer:
    return Signer.load(FIXTURE_PROJECT / 'key.gpg', FIXTURE_PASSPHRASE)


def deb_builder(ctx: BuildContext, project: Path) -> builders.DebPackageBuilder:
    return builders.DebPackageBuilder(ctx, release('debian', 'trixie'), project, signer(), 'tiny')


def test_deb_environment_reported_and_known(project: Path, tmp_path: Path) -> None:
    first = deb_builder(context(tmp_path), project)
    first.build(frozenset({first}), {})
    installed = (
        first._context.workdir / 'tiny.debian-13-trixie.buildenv' / 'installed'
    ).read_text()
    lines = installed.splitlines()
    assert lines == sorted(lines) and any(line.startswith('debhelper ') for line in lines)
    assert first.env_key == hashlib.sha256(installed.encode()).hexdigest()
    assert first.outdir is not None and list(first.outdir.glob('tiny_*_amd64.deb'))
    # the same environment again: nothing is built
    second = deb_builder(context(tmp_path / 'again'), project)
    second.known_env = first.env_key
    second.build(frozenset({second}), {})
    assert second.unchanged
    assert second.outdir is not None and list(second.outdir.iterdir()) == []


def test_rpm_environment_reported(project: Path, tmp_path: Path) -> None:
    ctx = context(tmp_path)
    spec = SpecInfo(frozenset({'make'}), frozenset({'tiny'}), (), '1.0', '1.fc44')
    rpm = builders.RpmPackageBuilder(
        ctx, release('fedora', '44'), project, signer(), 'tiny', spec=spec
    )
    rpm.build(frozenset({rpm}), {})
    installed = (ctx.workdir / 'tiny.fedora-44.buildenv' / 'installed').read_text().splitlines()
    assert installed == sorted(installed) and any(line.startswith('make-') for line in installed)


def test_binnmu_of_a_stored_source(project: Path, tmp_path: Path) -> None:
    """A stored source rebuilt -B: only the binary outputs, the binNMU entry on top."""
    first = deb_builder(context(tmp_path), project)
    first.build(frozenset({first}), {})
    assert first.outdir is not None
    src = tmp_path / 'binnmu'
    src.mkdir()
    for name in ('*.dsc', '*.tar.*'):
        for path in first.outdir.glob(name):
            shutil.copy2(path, src / path.name)
    stored = sorted(p.name for p in src.iterdir())
    (src / 'binnmu.changelog').write_text(
        binnmu_entry(
            'tiny',
            '1.0~bpo13+b1',
            'trixie',
            'test <test@example.invalid>',
            'Thu, 05 Nov 2026 12:00:00 +0000',
            builders.ARCHITECTURE,
        )
        + '\n'
    )
    engine = Engine.connect()
    image = REGISTRY + builders.builder_image(release('debian', 'trixie'))
    temporary = 'distropack-test/binnmu:deps'
    run(engine, image, ['deps'], mounts=[Mount('/src', src)], as_root=True, commit_to=temporary)
    run(
        engine,
        temporary,
        ['build', '-B'],
        mounts=[Mount('/src', src)],
        as_root=True,
        network=False,
        remove_image=True,
    )
    new = sorted(
        p.name for p in src.iterdir() if p.name not in stored and p.name != 'binnmu.changelog'
    )
    assert new and all(n.endswith(('.deb', '.buildinfo', '.changes')) for n in new)
    changes = next(src.glob('*.changes')).read_text()
    assert 'Version: 1.0~bpo13+b1' in changes
