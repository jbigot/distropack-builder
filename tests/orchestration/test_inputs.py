# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The download phase of a channel: snapshots, origs, specs and downloads, before any build."""

import re
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
import respx
import yaml

from distro_releases import Release, distribution
from distropack_builder import config, inputs, snapshots
from distropack_builder.context import BuildContext
from distropack_builder.docker import Cancellation
from distropack_builder.downloads import DownloadFailed
from distropack_builder.rpmquery import SpecFailed, SpecInfo
from distropack_builder.snapshots import Record, SnapshotFailed
from distropack_builder.uscan import OrigFiles, snapshot_orig
from fake_runner import FakeRunner, host_path, tools_respond
from support import add_upstream_commit, make_project

ORIG_BASE = 'https://orig.test'
SHA = 'abcdef0123456789abcdef0123456789abcdef01'
RECORD = Record(SHA, '1.12.0', 4, 'main', 'not verified')


def test_snapshot_spec() -> None:
    spec = (
        'Name: pdi\nVersion: 1.11.1\nRelease: 1%{?dist}\n\n%prep\n%autosetup\n\n'
        '%changelog\n* Thu Nov 05 2026 Packager <p@example.invalid> - 1.11.1-1\n- Initial.\n'
    )
    rewritten = inputs.snapshot_spec(spec, '1.12.0+main.4.gabcdef0', RECORD)
    assert rewritten == (
        f'%global commit {SHA}\n'
        'Name: pdi\nVersion: 1.12.0+main.4.gabcdef0\nRelease: 1%{?dist}\n\n%prep\n%autosetup\n\n'
        '%changelog\n'
        '* Thu Nov 05 2026 Packager <p@example.invalid> - 1.12.0+main.4.gabcdef0-1%{?dist}\n'
        f'- Snapshot of main at {SHA}, not verified\n\n'
        '* Thu Nov 05 2026 Packager <p@example.invalid> - 1.11.1-1\n- Initial.\n'
    )
    plain = 'Name: pdi\nVersion: 1.11.1\nRelease: 1\n'
    assert inputs.snapshot_spec(plain, '1.12.0', RECORD) == (
        f'%global commit {SHA}\nName: pdi\nVersion: 1.12.0\nRelease: 1\n'
    )


def test_snapshot_entry_of_a_pinned_commit() -> None:
    pinned = Record(SHA, '1.12.0', 4, None, 'pinned by the packaging')
    assert inputs.snapshot_entry(pinned) == f'Snapshot at {SHA}, pinned by the packaging'


@pytest.fixture
def context(tmp_path: Path) -> BuildContext:
    workdir = tmp_path / 'work'
    workdir.mkdir()
    return BuildContext(cast(Any, None), FakeRunner(tools_respond), Cancellation(), workdir, 1)


def runner_of(context: BuildContext) -> FakeRunner:
    return cast(FakeRunner, context.run)


def test_snapshot_orig(context: BuildContext, tmp_path: Path) -> None:
    project = make_project(tmp_path / 'project', ORIG_BASE, {'debian': ['trixie']}, ['hello-lib'])
    tarball = tmp_path / 'snapshots' / f'hello-lib-{SHA}.tar.gz'
    tarball.parent.mkdir()
    tarball.write_bytes(b'tarball')
    directory = tmp_path / 'origs'
    found = snapshot_orig(
        context, project, 'hello-lib', tarball, 'hello-lib', '1.0+main.1.gabcdef0', directory
    )
    (call,) = runner_of(context).calls
    assert call.args[0] == 'mk-origtargz'
    assert call.args[call.args.index('--package') + 1] == 'hello-lib'
    assert call.args[call.args.index('--version') + 1] == '1.0+main.1.gabcdef0'
    assert '--copy' in call.args
    assert host_path(call, call.args[-1]) == tarball
    assert call.workdir is not None
    assert host_path(call, call.workdir) == project / 'hello-lib'
    assert found == OrigFiles(
        tarball, directory / 'hello-lib_1.0+main.1.gabcdef0.orig.tar.gz', True
    )
    again = snapshot_orig(
        context, project, 'hello-lib', tarball, 'hello-lib', '1.0+main.1.gabcdef0', directory
    )
    assert again == found
    assert len(runner_of(context).calls) == 1


def release(dist: str, name: str, on: date) -> Release:
    (found,) = (r for r in distribution(dist).releases(on) if name in (r.codename, r.version))
    return found


def channel_project(tmp_path: Path, dists: dict[str, list[str]]) -> Path:
    """The fixture project with hello-lib, in a channel w that takes it from its upstream main."""
    project = make_project(tmp_path / 'project', ORIG_BASE, dists, ['hello-lib'])
    conf_path = project / 'build.conf'
    conf = yaml.safe_load(conf_path.read_text())
    conf['channels'] = {'stable': {'kind': 'release'}, 'w': {'kind': 'snapshot'}}
    conf['packages']['hello-lib'] = {
        'channels': {
            'w': {
                'git': f'file://{project}/upstream/hello-lib.git',
                'ref': 'main',
                'verify': 'none',
            }
        }
    }
    for dist in dists:
        if dist == 'fedora':
            conf['distribs'][dist]['repository']['channels'] = {
                'w': {'path': 'repositories/w.fedora', 'url': 'http://repo.example.invalid/w'}
            }
    conf_path.write_text(yaml.safe_dump(conf))
    return project


def selection(
    conf: config.BuildConfig, releases: list[Release]
) -> tuple[dict[str, set[Release]], dict[str, set[Release]]]:
    selected: dict[str, set[Release]] = {dist: set() for dist in conf.distribs}
    for found in releases:
        selected[found.distribution.id].add(found)
    return selected, {name: set() for name in conf.packages}


def test_fetch_records_and_reuses_snapshots(
    context: BuildContext, tmp_path: Path, recorded_http: respx.MockRouter
) -> None:
    project = channel_project(tmp_path, {'debian': ['trixie']})
    first = add_upstream_commit(project / 'upstream' / 'hello-lib.git', 'one')
    conf = config.load(project / 'build.conf')
    selected, disabled = selection(conf, [])
    download = tmp_path / 'download'
    fetched = inputs.fetch(context, conf, 'w', selected, disabled, download, {})
    assert fetched.snapshots == {'hello-lib': Record(first, '1.0', 1, 'main', 'not verified')}
    assert snapshots.read(download / 'snapshots.json', 'w') == fetched.snapshots
    add_upstream_commit(project / 'upstream' / 'hello-lib.git', 'two')  # the branch moved
    again = inputs.fetch(context, conf, 'w', selected, disabled, download, {})
    assert again.snapshots == fetched.snapshots


def test_given_snapshot_overrides_the_file(
    context: BuildContext, tmp_path: Path, recorded_http: respx.MockRouter
) -> None:
    project = channel_project(tmp_path, {'debian': ['trixie']})
    first = add_upstream_commit(project / 'upstream' / 'hello-lib.git', 'one')
    conf = config.load(project / 'build.conf')
    selected, disabled = selection(conf, [])
    download = tmp_path / 'download'
    inputs.fetch(context, conf, 'w', selected, disabled, download, {})
    second = add_upstream_commit(project / 'upstream' / 'hello-lib.git', 'two')
    given = inputs.fetch(context, conf, 'w', selected, disabled, download, {'hello-lib': first})
    record = given.snapshots['hello-lib']
    assert isinstance(record, Record) and record.commit == first
    later = inputs.fetch(context, conf, 'w', selected, disabled, download, {'hello-lib': second})
    record = later.snapshots['hello-lib']
    assert isinstance(record, Record) and record.commit == second
    assert snapshots.read(download / 'snapshots.json', 'w') == later.snapshots


def test_failed_snapshot_fails_its_package_only(
    context: BuildContext, tmp_path: Path, recorded_http: respx.MockRouter, frozen: date
) -> None:
    project = channel_project(tmp_path, {'debian': ['trixie'], 'fedora': ['44']})
    conf_path = project / 'build.conf'
    conf = yaml.safe_load(conf_path.read_text())
    conf['packages']['hello-lib']['channels']['w']['git'] = f'file://{project}/upstream/none.git'
    conf_path.write_text(yaml.safe_dump(conf))
    loaded = config.load(conf_path)
    trixie, f44 = release('debian', 'trixie', frozen), release('fedora', '44', frozen)
    selected, disabled = selection(loaded, [trixie, f44])
    fetched = inputs.fetch(context, loaded, 'w', selected, disabled, tmp_path / 'download', {})
    failure = fetched.snapshots['hello-lib']
    assert isinstance(failure, SnapshotFailed)
    orig = fetched.origs['hello-lib']
    assert isinstance(orig, DownloadFailed)
    assert failure.reason in str(orig)
    spec = fetched.specs[('hello-lib', f44.uid)]
    assert isinstance(spec, SpecFailed)
    assert failure.reason in spec.message
    assert not runner_of(context).calls  # nothing to query, nothing to name


def test_rpm_snapshot_source_not_downloaded(
    context: BuildContext, tmp_path: Path, recorded_http: respx.MockRouter, frozen: date
) -> None:
    project = channel_project(tmp_path, {'fedora': ['44']})
    spec = project / 'hello-lib' / 'hello-lib.spec'
    spec.write_text(
        re.sub(
            r'^Source0:.*$',
            'Source0: https://orig.test/hello-lib-%{version}.tar.gz\n'
            'Source1: https://orig.test/extra.txt',
            spec.read_text(),
            flags=re.M,
        )
    )
    head = add_upstream_commit(project / 'upstream' / 'hello-lib.git', 'one')
    extra = recorded_http.get('https://orig.test/extra.txt').respond(200, content=b'extra')
    loaded = config.load(project / 'build.conf')
    f44 = release('fedora', '44', frozen)
    selected, disabled = selection(loaded, [f44])
    fetched = inputs.fetch(context, loaded, 'w', selected, disabled, tmp_path / 'download', {})
    info = fetched.specs[('hello-lib', f44.uid)]
    assert isinstance(info, SpecInfo)
    assert info.version == f'1.0+main.1.g{head[:7]}'
    assert extra.call_count == 1
    assert list(fetched.files) == ['https://orig.test/extra.txt']
    built = fetched.built_specs['hello-lib']
    assert built.read_text().startswith(f'%global commit {head}\n')


def test_release_channel_inputs_unchanged(
    context: BuildContext, tmp_path: Path, recorded_http: respx.MockRouter, frozen: date
) -> None:
    project = make_project(
        tmp_path / 'project', ORIG_BASE, {'debian': ['trixie'], 'fedora': ['44']}, ['hello-lib']
    )
    orig = recorded_http.get(f'{ORIG_BASE}/hello-lib-1.0.tar.gz').respond(200, content=b'x')
    loaded = config.load(project / 'build.conf')
    trixie, f44 = release('debian', 'trixie', frozen), release('fedora', '44', frozen)
    selected, disabled = selection(loaded, [trixie, f44])
    download = tmp_path / 'download'
    fetched = inputs.fetch(context, loaded, 'stable', selected, disabled, download, {})
    assert fetched.snapshots == {}
    assert not (download / 'snapshots.json').exists()
    assert isinstance(fetched.origs['hello-lib'], OrigFiles)
    assert [c.args[0] for c in runner_of(context).calls] == ['uscan', 'query']
    assert fetched.built_specs == {'hello-lib': project / 'hello-lib' / 'hello-lib.spec'}
    assert list(fetched.files) == [f'{ORIG_BASE}/hello-lib-1.0.tar.gz']
    assert orig.call_count == 1
