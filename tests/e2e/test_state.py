# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""A release keeps its last complete build: reused, rebuilt binary-only, refused, published.

hello-lib stands for a library and hello-app for what depends on it; hello-app's deb is
Architecture: any, with an Architecture: all hello-app-doc, so that a binNMU rebuilds the first
and carries the second over.
"""

import hashlib
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

RELEASES = {'debian': ['trixie'], 'fedora': ['44']}
PACKAGES = ['hello-lib', 'hello-app']
Project = Callable[[dict[str, list[str]], list[str]], Path]
RunPkgbuild = Callable[..., subprocess.CompletedProcess[str]]
DEBIAN = 'Debian GNU/Linux 13 (Trixie)'


class Runs:
    """Runs of the builder on one project, with one state and one download directory."""

    def __init__(self, project: Path, run: RunPkgbuild, env: dict[str, str]) -> None:
        self.project = project
        self._run = run
        self._state = project.parent / 'state'
        self._downloads = project.parent / 'downloads'
        # the deb tarball is uscan's (git archive, xz), which the fixture's sources cannot list
        result = subprocess.run(
            [sys.executable, '-m', 'distropack_builder', 'sources', 'build.conf', 'hello-lib'],
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert result.returncode == 0, result.stdout

    def __call__(self, *args: str, status: int = 0) -> str:
        """Run with args; its output, once its exit status is checked."""
        result = self._run(
            self.project,
            '--state-dir',
            str(self._state),
            '--download-dir',
            str(self._downloads),
            *args,
        )
        assert result.returncode == status, result.stdout
        return result.stdout

    def published(self) -> dict[str, bytes]:
        """Every package file published, by path in the repositories: what users download."""
        root = self.project / 'repositories'
        return {
            str(p.relative_to(root)): p.read_bytes()
            for p in root.rglob('*')
            if p.is_file() and p.name.endswith(('.deb', '.dsc', '.tar.xz', '.tar.gz', '.rpm'))
        }

    def names(self) -> list[str]:
        """The names of the package files published."""
        return sorted(Path(path).name for path in self.published())


@pytest.fixture
def runs(project: Project, run_pkgbuild: RunPkgbuild, pkgbuild_env: dict[str, str]) -> Runs:
    return Runs(project(RELEASES, PACKAGES), run_pkgbuild, pkgbuild_env)


def new_entry(project: Path, package: str, version: str, day: int) -> None:
    """A new top entry of package's debian/changelog, at version, dated November day, 2026."""
    changelog = project / package / 'debian' / 'changelog'
    entry = (
        f'{package} ({version}) unstable; urgency=medium\n\n  * New version.\n\n'
        f' -- distropack test <test@example.invalid>  Fri, {day:02} Nov 2026 12:00:00 +0000\n\n'
    )
    changelog.write_text(entry + changelog.read_text())


def bump_release(project: Path, package: str, release: int) -> None:
    """Set the Release of package's spec, %{?dist} kept."""
    spec = project / package / f'{package}.spec'
    spec.write_text(re.sub(r'(?m)^(Release:\s*)\d+', rf'\g<1>{release}', spec.read_text()))


def deb_field(deb: Path, field: str) -> str:
    """A field of a binary package's control file."""
    return subprocess.run(
        ['dpkg-deb', '-f', str(deb), field], check=True, capture_output=True, text=True
    ).stdout.strip()


def tree_hash(directory: Path) -> str:
    """The sha256 of every file under directory, names and bytes."""
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob('*') if p.is_file()):
        digest.update(str(path.relative_to(directory)).encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()


@pytest.mark.e2e
def test_second_run_reuses_everything(runs: Runs) -> None:
    runs()
    before = runs.published()
    out = runs()
    assert len(re.findall(r'^reused ', out, re.M)) == 4, out
    assert not re.findall(r'^built ', out, re.M), out
    assert runs.published() == before


@pytest.mark.e2e
def test_dependency_bump_binnmus_its_dependent(runs: Runs) -> None:
    runs()
    source = {
        name: data
        for name, data in runs.published().items()
        if Path(name).name in ('hello-app_1.0~bpo13.dsc', 'hello-app_1.0~bpo13.tar.xz')
    }
    assert len(source) == 2, runs.names()
    new_entry(runs.project, 'hello-lib', '1.0-2', 6)
    bump_release(runs.project, 'hello-lib', 2)
    runs()
    names = runs.names()
    assert 'hello-lib_1.0-2~bpo13_all.deb' in names, names
    assert 'hello-app_1.0~bpo13+b1_amd64.deb' in names, names
    assert 'hello-app-doc_1.0~bpo13_all.deb' in names, names  # carried over
    assert 'hello-app-1.0-1.fc44.1.noarch.rpm' in names, names
    published = runs.published()
    assert {name: published[name] for name in source} == source  # the source is never altered
    (deb,) = (runs.project / 'repositories' / 'debian').rglob('hello-app_1.0~bpo13+b1_amd64.deb')
    assert deb_field(deb, 'Source') == 'hello-app (1.0~bpo13)'
    new_entry(runs.project, 'hello-lib', '1.0-3', 7)
    runs()
    assert 'hello-app_1.0~bpo13+b2_amd64.deb' in runs.names()
    new_entry(runs.project, 'hello-app', '1.1', 8)
    runs()
    names = runs.names()
    assert 'hello-app_1.1~bpo13_amd64.deb' in names, names
    assert not [n for n in names if n.startswith('hello-app_1.1~bpo13+b')], names


@pytest.mark.e2e
def test_unversioned_change_refused(runs: Runs) -> None:
    runs()
    before = runs.published()
    rules = runs.project / 'hello-lib' / 'debian' / 'rules'
    rules.write_text(rules.read_text() + '# changed, its version not\n')
    out = runs(status=1)
    assert 'hello-lib: sources or packaging changed without a new version' in out, out
    assert runs.published() == before


@pytest.mark.e2e
def test_deb_failure_keeps_the_debian_release(runs: Runs) -> None:
    runs()
    debian_before = {k: v for k, v in runs.published().items() if k.startswith('debian/')}
    new_entry(runs.project, 'hello-app', '1.1', 6)
    bump_release(runs.project, 'hello-app', 2)
    rules = runs.project / 'hello-app' / 'debian' / 'rules'
    rules.write_text(rules.read_text().replace('hello > greeting', 'hello > greeting; exit 1'))
    out = runs(status=1)
    assert f'failed hello-app on {DEBIAN}' in out, out
    published = runs.published()
    assert {k: v for k, v in published.items() if k.startswith('debian/')} == debian_before
    assert 'hello-app-1.0-2.fc44.noarch.rpm' in runs.names()


@pytest.mark.e2e
def test_no_publish_then_publish_only(runs: Runs) -> None:
    runs('-D', 'fedora', '--no-publish')
    assert not (runs.project / 'repositories').exists()
    out = runs('--publish-only')
    assert f'no state for {DEBIAN}' in out, out
    assert 'hello-app-1.0-1.fc44.noarch.rpm' in runs.names()
    assert not (runs.project / 'repositories' / 'debian' / 'dists').exists()


@pytest.mark.e2e
def test_packaging_untouched(runs: Runs) -> None:
    before = {name: tree_hash(runs.project / name) for name in PACKAGES}
    runs()
    new_entry(runs.project, 'hello-lib', '1.0-2', 6)
    after_entry = {name: tree_hash(runs.project / name) for name in PACKAGES}
    runs()  # hello-lib built from source, hello-app rebuilt binary-only
    assert {name: tree_hash(runs.project / name) for name in PACKAGES} == after_entry
    assert after_entry['hello-app'] == before['hello-app']
