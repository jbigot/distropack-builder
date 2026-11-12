# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""What the builders ask of the engine: images, mounts, users; no container runs."""

import dataclasses
import hashlib
import importlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import httpx
import pysequoia
import pytest
import respx
import time_machine
import yaml
from debian.changelog import Changelog
from typer.testing import CliRunner

from distro_releases import Release, distribution
from distropack_builder import builders as builders_module
from distropack_builder import state
from distropack_builder.bake import PROTOCOL
from distropack_builder.context import BuildContext
from distropack_builder.docker import Cancellation, Cancelled, ContainerFailed, RunResult
from distropack_builder.rpmquery import SpecInfo
from distropack_builder.scheduler import Interrupted, Reused, schedule
from distropack_builder.signing import Signer
from distropack_builder.uscan import OrigFiles
from fake_runner import INSTALLED, Call, FakeRunner, StopBuild, tools_respond
from support import (
    FIXTURE_PASSPHRASE,
    FIXTURE_PROJECT,
    SNAP,
    add_snapshot_package,
    add_upstream_commit,
    make_project,
    use_channels,
    write_tarball,
)

REGISTRY = 'ghcr.io/jbigot/distropack-builder/'
ORIG_BASE = 'https://orig.test'
HELLO_LIB_ORIG = f'{ORIG_BASE}/hello-lib-1.0.tar.gz'

# The release channel of build.conf without channels:.
STABLE = 'stable'


def release(dist: str, name: str, on: date) -> Release:
    (found,) = (r for r in distribution(dist).releases(on) if name in (r.codename, r.version))
    return found


def respond(call: Call) -> RunResult | None:
    """Stop at the builder's container; other tools succeed silently, deps reports INSTALLED."""
    if call.is_deps:
        (call.targets['/buildenv'].source / 'installed').write_text(INSTALLED)
    if call.is_build:
        raise StopBuild()
    return None


@pytest.fixture
def cli(load_script: Callable[[str], ModuleType]) -> ModuleType:
    return load_script('pkgbuild')


@pytest.fixture
def builders(cli: ModuleType) -> ModuleType:
    """The builders, loaded with cli."""
    return importlib.import_module('distropack_builder.builders')


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner(respond)


@pytest.fixture
def context(runner: FakeRunner, tmp_path: Path) -> BuildContext:
    workdir = tmp_path / 'work'
    workdir.mkdir()
    return BuildContext(cast(Any, None), runner, Cancellation(), workdir, 1)


def builder(builders: ModuleType, context: BuildContext, rel: Release) -> Any:
    """The fixture project's builder of hello-app for rel."""
    signer = Signer.load(FIXTURE_PROJECT / 'key.gpg', FIXTURE_PASSPHRASE)
    if builders.targets.is_deb(rel.distribution):
        return builders.DebPackageBuilder(
            context, rel, FIXTURE_PROJECT / 'hello-app', signer, 'hello-app', channel=STABLE
        )
    spec = SpecInfo(frozenset({'hello-lib'}), frozenset({'hello-app'}), (), '1.0', '1.fc44')
    return builders.RpmPackageBuilder(
        context, rel, FIXTURE_PROJECT / 'hello-app', signer, 'hello-app', channel=STABLE, spec=spec
    )


def build(builders: ModuleType, context: BuildContext, rel: Release) -> None:
    with pytest.raises(StopBuild):
        builder(builders, context, rel).build(frozenset(), {})


def builder_call(runner: FakeRunner) -> Call:
    """The container that installs the build's dependencies, from the release's builder image."""
    (call,) = (c for c in runner.calls if c.is_deps)
    return call


def build_call(runner: FakeRunner) -> Call:
    """The container that builds, from the commit of the dependency container."""
    (call,) = (c for c in runner.calls if c.is_build)
    return call


def test_deb_builder_image_and_mounts(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date
) -> None:
    build(builders, context, release('debian', 'trixie', frozen))
    call = builder_call(runner)
    assert call.image == REGISTRY + f'debian_builder:trixie-p{PROTOCOL}'
    assert call.as_root
    assert sorted(call.targets) == ['/buildenv', '/localrepo', '/src']
    assert call.env['DISTROPACK_BUILD_PROFILES'] == 'debian,trixie'
    assert sorted(build_call(runner).targets) == ['/src']


def test_no_mk_build_deps(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date
) -> None:
    """The builder installs the build dependencies itself, in the target release."""
    build(builders, context, release('debian', 'trixie', frozen))
    assert not [c for c in runner.calls if 'mk-build-deps' in c.args]


def test_rpm_builder_mounts_read_only(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date
) -> None:
    build(builders, context, release('fedora', '44', frozen))
    call = builder_call(runner)
    assert call.image == REGISTRY + f'fedora_builder:44-p{PROTOCOL}'
    assert call.as_root
    mounts = call.targets
    assert (mounts['/src'].read_only, mounts['/localrepo'].read_only) == (True, True)
    built = build_call(runner).targets
    assert sorted(built) == ['/output', '/src']
    assert (built['/src'].read_only, built['/output'].read_only) == (True, False)


@pytest.mark.parametrize(('dist', 'name'), [('debian', 'trixie'), ('fedora', '44')])
def test_no_secret_key_in_a_builder(
    builders: ModuleType,
    context: BuildContext,
    runner: FakeRunner,
    frozen: date,
    dist: str,
    name: str,
) -> None:
    build(builders, context, release(dist, name, frozen))
    for call in (builder_call(runner), build_call(runner)):
        assert '/secret' not in call.targets
        assert not [m for m in call.mounts if m.source.name.startswith('distropack-sign.')]


def test_tools_run_as_the_runner_chooses_in_a_run(run_main: Callable[..., int]) -> None:
    """aptly and rpm_sign run as the runner chooses; only builder images run as root."""
    runner = FakeRunner(rpm_builds())
    assert run_main(runner, {'fedora': ['44'], 'debian': ['trixie']}, ['hello-undeclared']) == 0
    tools = [c for c in runner.calls if c.is_tool]
    assert {c.image.rsplit('/', 1)[1].split(':')[0] for c in tools} == {'deb_tools', 'rpm_sign'}
    assert not [c for c in tools if c.as_root]


@pytest.mark.parametrize(('dist', 'name'), [('debian', 'trixie')])
def test_tools_run_as_the_runner_chooses(
    builders: ModuleType,
    context: BuildContext,
    runner: FakeRunner,
    frozen: date,
    dist: str,
    name: str,
) -> None:
    build(builders, context, release(dist, name, frozen))
    tools = [c for c in runner.calls if c.is_tool]
    assert tools and not [c for c in tools if c.as_root]


@pytest.mark.parametrize(('dist', 'name'), [('debian', 'trixie'), ('fedora', '44')])
def test_cancelled_builder_is_interrupted(
    builders: ModuleType,
    context: BuildContext,
    runner: FakeRunner,
    frozen: date,
    dist: str,
    name: str,
) -> None:
    """A builder whose container is cancelled is interrupted, not failed."""

    def cancelled(call: Call) -> RunResult | None:
        if call.is_build:
            raise Cancelled()
        return tools_respond(call)

    runner.respond = cancelled
    found = builder(builders, context, release(dist, name, frozen))
    outcomes = schedule(
        {found: frozenset()},
        lambda b: b.build(frozenset(), {}),
        jobs=1,
        cancellation=context.cancellation,
    )
    assert outcomes == {found: Interrupted()}


def tarball() -> bytes:
    """hello-lib's upstream tarball, as the fixture's server serves it."""
    with tempfile.TemporaryDirectory() as scratch:
        write_tarball(FIXTURE_PROJECT / 'upstream' / 'hello-lib-1.0', Path(scratch) / 't.tar.gz')
        return (Path(scratch) / 't.tar.gz').read_bytes()


@pytest.fixture
def run_main(
    cli: ModuleType,
    recorded_http: respx.MockRouter,
    frozen: date,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[..., int]]:
    """Run main on a copy of the fixture project, with a fake runner; its exit status.

    hello-lib's upstream tarball is served at HELLO_LIB_ORIG, unless the test routes it otherwise.
    """
    recorded_http.get(HELLO_LIB_ORIG, name='orig').respond(200, content=tarball())

    def call(
        runner: FakeRunner,
        versions: dict[str, list[str]],
        packages: list[str],
        *args: str,
        prepare: Callable[[Path], None] = lambda project: None,
        passphrase: str = FIXTURE_PASSPHRASE,
    ) -> int:
        project = make_project(tmp_path / 'project', ORIG_BASE, versions, packages)
        prepare(project)
        monkeypatch.chdir(project)
        app = cli.make_app(runner=runner, connect=lambda host: cast(Any, None))
        result = CliRunner().invoke(
            app,
            ['run', '-j', '2', *args, 'build.conf'],
            env={'DISTROPACK_GPG_PASSPHRASE': passphrase},
            catch_exceptions=False,
        )
        # what the run printed, for the tests that read it with capsys
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.exit_code

    yield call


def test_local_repository_is_the_closure(run_main: Callable[..., int]) -> None:
    runner = FakeRunner(tools_respond)
    assert run_main(runner, {'debian': ['trixie']}, ['hello-lib', 'hello-app']) == 0
    included = [
        c
        for c in runner.calls
        if c.args[2:4] == ('repo', 'add')
        and c.targets['/aptlyout'].source.parent.name.startswith('hello-app.')
    ]
    # hello-app's own output directory, empty yet, is included as well, as master did
    assert sorted(c.targets['/data'].source.parent.name.split('.')[0] for c in included) == [
        'hello-app',
        'hello-lib',
    ]


def test_failed_download_fails_only_dependents(
    run_main: Callable[..., int],
    recorded_http: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An rpm Source: that cannot be downloaded (the deb sources are uscan's)."""
    recorded_http.routes['orig'].return_value = httpx.Response(404)
    runner = FakeRunner(rpm_builds())
    status = run_main(runner, {'fedora': ['44']}, ['hello-lib', 'hello-app', 'hello-undeclared'])
    assert status == 1
    built = {
        p
        for p in ('hello-lib', 'hello-app', 'hello-undeclared')
        for c in runner.calls
        if c.build_of(p)
    }
    assert built == {'hello-undeclared'}
    out = capsys.readouterr().out
    assert f'failed hello-lib on Fedora 44: while downloading {HELLO_LIB_ORIG}' in out
    assert 'skipped hello-app on Fedora 44: hello-lib failed' in out


@pytest.mark.parametrize('outcome', ['success', 'failure'])
def test_work_directory_removed(
    run_main: Callable[..., int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    temp = tmp_path / 'temp'
    temp.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(temp))

    def fail(call: Call) -> RunResult | None:
        if call.is_build:
            raise ContainerFailed(2, [call.image])
        return tools_respond(call)

    runner = FakeRunner(tools_respond if outcome == 'success' else fail)
    status = run_main(runner, {'debian': ['trixie']}, ['hello-lib', 'hello-app'])
    assert status == (0 if outcome == 'success' else 1)
    assert list(temp.iterdir()) == []


# A real rpm, which the fake builders copy under the name of what they build: the repository
# metadata is read from it.
RPM = (
    Path(__file__).parents[2]
    / 'packages/distropack-builder/tests/data/rpms'
    / 'hello-lib-1.0-1.fc44.noarch.rpm'
).read_bytes()


def rpm_builds(
    fail_on: tuple[str, str] | None = None, junk_on: str | None = None
) -> Callable[[Call], RunResult | None]:
    """Builders that write an rpm, but for fail_on, a (package, image tag) pair.

    On the release junk_on, they write a file that is not an rpm, for its publication to fail.
    """

    def respond(call: Call) -> RunResult | None:
        if call.is_build and '/output' in call.targets:  # an rpm builder
            tag = call.image.rpartition(':')[2].removesuffix(f'-p{PROTOCOL}')
            output = call.targets['/output'].source
            package = output.parent.name.split('.')[0]
            if (package, tag) == fail_on:
                raise ContainerFailed(1, [call.image], output='the build broke')
            rpm = output / f'{package}-1.0-1.fc{tag}.noarch.rpm'
            rpm.write_bytes(b'not an rpm' if tag == junk_on else RPM)
            return None
        return tools_respond(call)

    return respond


def indexed(project: Path) -> list[str]:
    """The Fedora releases whose repository metadata the run wrote."""
    fedora = project / 'repositories' / 'fedora'
    return sorted(d.name for d in fedora.iterdir() if (d / 'repodata' / 'repomd.xml').is_file())


def test_failing_release_publishes_nothing(run_main: Callable[..., int], tmp_path: Path) -> None:
    """hello-undeclared builds on 44, but hello-needs-root does not: 44 is not published."""
    runner = FakeRunner(rpm_builds(fail_on=('hello-needs-root', '44')))
    packages = ['hello-undeclared', 'hello-needs-root']
    assert run_main(runner, {'fedora': ['43', '44']}, packages) == 1
    assert indexed(tmp_path / 'project') == ['43']


@pytest.mark.parametrize('exists', [True, False])
def test_readme_keeps_failed_release_with_repository(
    run_main: Callable[..., int], tmp_path: Path, exists: bool
) -> None:
    def respond(call: Call) -> RunResult | None:
        if call.image.endswith(f'debian_builder:forky-p{PROTOCOL}'):
            raise ContainerFailed(1, [call.image])
        return tools_respond(call)

    def previous_run(project: Path) -> None:
        if exists:
            forky = project / 'repositories' / 'debian' / 'dists' / 'forky'
            forky.mkdir(parents=True)
            (forky / 'Release').write_text('previous run\n')

    runner = FakeRunner(respond)
    packages = ['hello-undeclared']
    status = run_main(runner, {'debian': ['trixie', 'forky']}, packages, prepare=previous_run)
    assert status == 1
    readme = (tmp_path / 'project' / 'repositories' / 'debian' / 'index.html').read_text()
    assert 'codename trixie' in readme
    assert ('codename forky' in readme) == exists
    published = [
        c.args[-2]
        for c in runner.calls
        if 'publish' in c.args and c.targets['/aptlyout'].source.name == 'debian'
    ]
    assert published and 'debian-14-forky' not in published


def test_failed_publication_spares_others(
    run_main: Callable[..., int], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The metadata of 43 cannot be written (its package is no rpm): 44 is still published."""
    runner = FakeRunner(rpm_builds(junk_on='43'))
    assert run_main(runner, {'fedora': ['43', '44']}, ['hello-undeclared']) == 1
    assert indexed(tmp_path / 'project') == ['44']
    out = capsys.readouterr().out
    assert 'publication failed for Fedora 43:' in out
    assert 'rpmReadPackageFile' in out


def test_sigterm_cancels(
    run_main: Callable[..., int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp = tmp_path / 'temp'
    temp.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(temp))
    previous = signal.getsignal(signal.SIGTERM)
    cancellations: list[Cancellation] = []

    def respond(call: Call) -> RunResult | None:
        if call.is_build:
            signal.raise_signal(signal.SIGTERM)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                time.sleep(0.01)
                if cancellations and cancellations[0].cancelled:
                    break
            raise Cancelled()
        return tools_respond(call)

    class Spy(FakeRunner):
        def __call__(self, *args: Any, **kwargs: Any) -> RunResult:
            if kwargs.get('cancellation') is not None and not cancellations:
                cancellations.append(kwargs['cancellation'])
            return super().__call__(*args, **kwargs)

    runner = Spy(respond)
    assert run_main(runner, {'debian': ['trixie']}, ['hello-undeclared']) == 130
    assert cancellations[0].cancelled
    assert not [
        c
        for c in runner.calls
        if 'publish' in c.args and c.targets['/aptlyout'].source.name == 'debian'
    ]
    assert list(temp.iterdir()) == []
    assert signal.getsignal(signal.SIGTERM) == previous


def test_orig_named_after_upstream_version(run_main: Callable[..., int]) -> None:
    """The upstream tarball is named from the changelog, not from the rebuild's version."""
    sources: list[list[str]] = []

    def respond(call: Call) -> RunResult | None:
        if call.build_of('hello-lib'):
            sources.append(sorted(p.name for p in call.targets['/src'].source.iterdir()))
        return tools_respond(call)

    runner = FakeRunner(respond)
    assert run_main(runner, {'debian': ['trixie', 'forky']}, ['hello-lib']) == 0
    assert sources == [['hello-lib-1.0', 'hello-lib_1.0.orig.tar.gz']] * 2


def test_aptly_adds_without_changes(run_main: Callable[..., int]) -> None:
    runner = FakeRunner(tools_respond)
    assert run_main(runner, {'debian': ['trixie']}, ['hello-undeclared']) == 0
    aptly = [c for c in runner.calls if 'aptly' in c.args]
    assert aptly and not [c for c in aptly if 'include' in c.args]
    assert [c for c in aptly if c.args[2:4] == ('repo', 'add')]
    assert not [c for c in runner.calls if any(a.endswith('.changes') for a in c.args)]
    assert not [c for c in runner.calls if 'debsign' in c.args or '/gnupghome' in c.targets]


def test_publish_skips_signing(run_main: Callable[..., int]) -> None:
    runner = FakeRunner(tools_respond)
    assert run_main(runner, {'debian': ['trixie']}, ['hello-undeclared']) == 0
    publish = [c for c in runner.calls if 'publish' in c.args]
    assert publish and all('-skip-signing' in c.args for c in publish)
    assert not [c for c in publish if any(a.startswith('-gpg-key') for a in c.args)]


def test_release_signed(run_main: Callable[..., int], tmp_path: Path) -> None:
    """Each published Release is signed both ways, trailing spaces dropped from all three."""
    runner = FakeRunner(tools_respond)
    assert run_main(runner, {'debian': ['trixie']}, ['hello-undeclared']) == 0
    dists = tmp_path / 'project' / 'repositories' / 'debian' / 'dists'
    names = sorted(d.name for d in dists.iterdir())
    assert names == ['stable', 'trixie']
    cert = pysequoia.Cert.from_file(str(FIXTURE_PROJECT / 'key.gpg'))
    for name in names:
        release = (dists / name / 'Release').read_bytes()
        assert release == f'Origin: hello\nCodename: {name}\n'.encode()
        detached = pysequoia.Sig.from_bytes((dists / name / 'Release.gpg').read_bytes())
        assert pysequoia.verify(bytes=release, store=lambda ids: [cert], signature=detached)
        inline = pysequoia.verify(
            bytes=(dists / name / 'InRelease').read_bytes(), store=lambda ids: [cert]
        )
        # the final newline, part of the signature's framing, is not in what sequoia reads back
        assert inline.valid_sigs and inline.bytes == release.removesuffix(b'\n')


def test_local_repository_unsigned(run_main: Callable[..., int]) -> None:
    """hello-app's builder reads a local repository holding hello-lib, with no signature."""
    seen: list[list[str]] = []

    def respond(call: Call) -> RunResult | None:
        if call.deps_of('hello-app'):
            local = call.targets['/localrepo'].source
            seen.append(sorted(p.name for p in local.rglob('*') if p.is_file()))
        return tools_respond(call)

    runner = FakeRunner(respond)
    assert run_main(runner, {'debian': ['trixie']}, ['hello-lib', 'hello-app']) == 0
    (files,) = seen
    assert 'Release' in files
    assert 'Release.gpg' not in files and 'InRelease' not in files


def test_one_query_per_rpm_release(run_main: Callable[..., int]) -> None:
    """Every spec of a release is queried in one container of its builder image; none for deb."""
    runner = FakeRunner(tools_respond)
    packages = ['hello-undeclared', 'hello-needs-root']
    assert run_main(runner, {'fedora': ['43', '44'], 'debian': ['trixie']}, packages) == 0
    queries = [c for c in runner.calls if c.is_query]
    assert sorted(c.image.rsplit('/', 1)[1] for c in queries) == [
        f'fedora_builder:43-p{PROTOCOL}',
        f'fedora_builder:44-p{PROTOCOL}',
    ]
    for call in queries:
        assert call.as_root
        specs = {t: m for t, m in call.targets.items() if t.startswith('/specs/')}
        assert sorted(specs) == ['/specs/hello-needs-root.spec', '/specs/hello-undeclared.spec']
        assert all(m.read_only for m in specs.values())
    assert not [c for c in runner.calls if 'rpmspec' in c.args]


def test_failed_query_fails_its_package_only(
    run_main: Callable[..., int], capsys: pytest.CaptureFixture[str]
) -> None:
    def respond(call: Call) -> RunResult | None:
        result = tools_respond(call)
        if call.is_query:
            broken = call.targets['/out'].source / 'hello-needs-root'
            (broken / 'status').write_text('1\n')
            (broken / 'errors').write_text('error: line 3: Unknown tag: Bogus\n')
        return result

    runner = FakeRunner(respond)
    packages = ['hello-undeclared', 'hello-needs-root']
    assert run_main(runner, {'fedora': ['44']}, packages) == 1
    assert [c for c in runner.calls if c.build_of('hello-undeclared')]
    assert not [c for c in runner.calls if c.build_of('hello-needs-root')]
    assert 'Unknown tag: Bogus' in capsys.readouterr().out


@pytest.mark.parametrize('outcome', ['success', 'failure'])
def test_rpm_sign_secret_removed(
    run_main: Callable[..., int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """rpm_sign gets the locked key and its passphrase read-only, for its call alone."""
    secrets: list[Path] = []
    builds = rpm_builds()

    def respond(call: Call) -> RunResult | None:
        if 'rpm_sign:' in call.image:
            secret = call.targets['/secret']
            assert secret.read_only
            assert sorted(p.name for p in secret.source.iterdir()) == ['key', 'passphrase']
            secrets.append(secret.source)
            if outcome == 'failure':
                raise ContainerFailed(1, [call.image])
        return builds(call)

    runner = FakeRunner(respond)
    status = run_main(runner, {'fedora': ['44']}, ['hello-undeclared'])
    assert status == (0 if outcome == 'success' else 1)
    assert secrets and not [s for s in secrets if s.exists()]
    for call in runner.calls:
        assert FIXTURE_PASSPHRASE not in ' '.join(call.args)
        assert FIXTURE_PASSPHRASE not in ' '.join(call.env.values())


def test_no_gpg_tool_images(run_main: Callable[..., int]) -> None:
    runner = FakeRunner(rpm_builds())
    assert run_main(runner, {'fedora': ['44'], 'debian': ['trixie']}, ['hello-undeclared']) == 0
    images = {c.image.rsplit('/', 1)[1].split(':')[0] for c in runner.calls}
    # master's gpg images; deb_tools is the Debian tools' image again (Phase 10a), without gpg
    assert not images & {'gpg2', 'rpm_tools'}
    assert {'rpm_sign', 'deb_tools'} <= images


def test_wrong_passphrase_is_a_message(
    run_main: Callable[..., int], runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    """A passphrase that does not unlock the key exits 1 with one line, before any container."""
    status = run_main(runner, {'debian': ['trixie']}, ['hello-undeclared'], passphrase='wrong')
    assert status == 1
    err = capsys.readouterr().err
    assert 'error: the passphrase does not unlock' in err and 'key.gpg' in err
    assert 'Traceback' not in err
    assert runner.calls == []


def test_failed_publication_keeps_previous_metadata(
    run_main: Callable[..., int], tmp_path: Path
) -> None:
    """A release whose new metadata cannot be written keeps the ones of its last publication."""

    def previous_run(project: Path) -> None:
        repodata = project / 'repositories' / 'fedora' / '43' / 'repodata'
        repodata.mkdir(parents=True)
        (repodata / 'repomd.xml').write_text('previous run\n')

    runner = FakeRunner(rpm_builds(junk_on='43'))
    status = run_main(runner, {'fedora': ['43']}, ['hello-undeclared'], prepare=previous_run)
    assert status == 1
    repodata = tmp_path / 'project' / 'repositories' / 'fedora' / '43' / 'repodata'
    assert sorted(p.name for p in repodata.iterdir()) == ['repomd.xml']
    assert (repodata / 'repomd.xml').read_text() == 'previous run\n'


def test_rpm_repository_page_is_index(run_main: Callable[..., int], tmp_path: Path) -> None:
    """The rpm repository's page is index.html, as the deb one's: no README.html any more."""
    runner = FakeRunner(rpm_builds())
    assert run_main(runner, {'fedora': ['44']}, ['hello-undeclared']) == 0
    fedora = tmp_path / 'project' / 'repositories' / 'fedora'
    assert 'codename None, at http://repo.example.invalid/fedora/44' in (
        (fedora / 'index.html').read_text()
    )
    assert not (fedora / 'README.html').exists()


def test_no_url_no_page(run_main: Callable[..., int], tmp_path: Path) -> None:
    def without_url(project: Path) -> None:
        conf = project / 'build.conf'
        conf.write_text(conf.read_text().replace('url: http://repo.example.invalid/debian', ''))

    runner = FakeRunner(tools_respond)
    assert run_main(runner, {'debian': ['trixie']}, ['hello-undeclared'], prepare=without_url) == 0
    debian = tmp_path / 'project' / 'repositories' / 'debian'
    assert (debian / 'dists' / 'trixie' / 'InRelease').is_file()
    assert not (debian / 'index.html').exists()


@pytest.mark.parametrize(
    ('text', 'expected'),
    [('{{ page.url }\n', 'line 1'), ('{{ page.nosuch }}\n', 'nosuch')],
    ids=['syntax', 'undefined'],
)
def test_broken_template_fails_before_builds(
    run_main: Callable[..., int],
    runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
    text: str,
    expected: str,
) -> None:
    def broken(project: Path) -> None:
        (project / 'README.tpl' / 'README.html.j2').write_text(text)

    assert run_main(runner, {'debian': ['trixie']}, ['hello-undeclared'], prepare=broken) == 2
    (line,) = capsys.readouterr().err.splitlines()
    assert line.startswith('distropack-build: error: ')
    assert 'README.tpl/README.html.j2' in line and expected in line
    assert runner.calls == []


def add_quilt_package(project: Path, name: str, version: str) -> None:
    """A copy of hello-lib named name, at upstream version, with the same debian/watch."""
    target = project / name
    shutil.copytree(project / 'hello-lib', target)
    for path in (target / 'debian').rglob('*'):
        if path.is_file():
            path.write_text(path.read_text().replace('hello-lib', name))
    changelog = target / 'debian' / 'changelog'
    changelog.write_text(changelog.read_text().replace('(1.0-1)', f'({version}-1)', 1))
    (target / 'debian' / 'watch').write_text((project / 'hello-lib/debian/watch').read_text())
    for spec in target.glob('*.spec'):
        spec.unlink()
    # hello-lib's line is copied; the fake uscan writes a placeholder for another version
    placeholder = hashlib.sha512(f'hello-lib {version}\n'.encode()).hexdigest()
    with (target / 'sources').open('a') as lines:
        lines.write(f'SHA512 ({name}-{version}.tar.gz) = {placeholder}\n')
    conf = yaml.safe_load((project / 'build.conf').read_text())
    conf['packages'][name] = {}
    (project / 'build.conf').write_text(yaml.safe_dump(conf))


def uscan_calls(runner: FakeRunner) -> list[Call]:
    return [c for c in runner.calls if c.args[:1] == ('uscan',)]


def test_shared_watch_downloads_once(run_main: Callable[..., int]) -> None:
    """Two packages with the same watch file and upstream version: one uscan, one renaming."""
    runner = FakeRunner(tools_respond)

    def prepare(project: Path) -> None:
        add_quilt_package(project, 'hello-lib2', '1.0')

    assert run_main(runner, {'debian': ['trixie']}, ['hello-lib'], prepare=prepare) == 0
    assert len(uscan_calls(runner)) == 1
    renamed = [c for c in runner.calls if c.args[:1] == ('mk-origtargz',)]
    assert len(renamed) == 1
    # uscan runs for one of them, mk-origtargz names the tarball for the other
    assert {uscan_calls(runner)[0].workdir, renamed[0].workdir} == {
        '/project/hello-lib',
        '/project/hello-lib2',
    }
    built = {c.targets['/src'].source.parent.name.split('.')[0] for c in runner.calls if c.is_build}
    assert built == {'hello-lib', 'hello-lib2'}


def test_different_versions_download_twice(run_main: Callable[..., int]) -> None:
    runner = FakeRunner(tools_respond)

    def prepare(project: Path) -> None:
        add_quilt_package(project, 'hello-lib2', '2.0')

    assert run_main(runner, {'debian': ['trixie']}, ['hello-lib'], prepare=prepare) == 0
    assert sorted(c.workdir or '' for c in uscan_calls(runner)) == [
        '/project/hello-lib',
        '/project/hello-lib2',
    ]
    assert not [c for c in runner.calls if c.args[:1] == ('mk-origtargz',)]


def test_uscan_failure_fails_the_package_and_descendants(
    run_main: Callable[..., int], capsys: pytest.CaptureFixture[str]
) -> None:
    def respond(call: Call) -> RunResult | None:
        if call.args[:1] == ('uscan',):
            raise ContainerFailed(
                1,
                [call.image, *call.args],
                output='uscan info: Scan watch files in .\nuscan error: no matching tag v1.0\n',
            )
        return tools_respond(call)

    runner = FakeRunner(respond)
    status = run_main(
        runner, {'debian': ['trixie']}, ['hello-lib', 'hello-app', 'hello-undeclared']
    )
    assert status == 1
    built = {
        p
        for p in ('hello-lib', 'hello-app', 'hello-undeclared')
        if any(c.build_of(p) for c in runner.calls)
    }
    assert built == {'hello-undeclared'}
    out = capsys.readouterr().out
    assert (
        'failed hello-lib on Debian GNU/Linux 13 (Trixie): while downloading with'
        ' hello-lib/debian/watch: uscan error: no matching tag v1.0' in out
    )
    assert 'skipped hello-app on Debian GNU/Linux 13 (Trixie): hello-lib failed' in out


def uscan_failing(output: str, times: int) -> Callable[[Call], RunResult | None]:
    """The tools answer, uscan failing times times with output first."""
    failures = [times]

    def respond(call: Call) -> RunResult | None:
        if call.args[:1] == ('uscan',) and failures[0]:
            failures[0] -= 1
            raise ContainerFailed(25, [call.image, *call.args], output=output)
        return tools_respond(call)

    return respond


GATEWAY_TIMEOUT = (
    'uscan warn: In watchfile debian/watch, reading webpage\n'
    '  https://github.com/pdidev/paraconf/tags failed: 504 Gateway Time-out\n'
)


def test_uscan_transient_failure_is_retried(run_main: Callable[..., int]) -> None:
    runner = FakeRunner(uscan_failing(GATEWAY_TIMEOUT, 2))
    assert run_main(runner, {'debian': ['trixie']}, ['hello-lib']) == 0
    assert len(uscan_calls(runner)) == 3


def test_uscan_failure_keeps_its_reason(
    run_main: Callable[..., int], capsys: pytest.CaptureFixture[str]
) -> None:
    """The reason is on the indented line after uscan's warning; retries end, and it shows."""
    runner = FakeRunner(uscan_failing(GATEWAY_TIMEOUT, 99))
    assert run_main(runner, {'debian': ['trixie']}, ['hello-lib']) == 1
    assert len(uscan_calls(runner)) == 5
    assert (
        'while downloading with hello-lib/debian/watch: uscan warn: In watchfile debian/watch,'
        ' reading webpage https://github.com/pdidev/paraconf/tags failed: 504 Gateway Time-out'
        in capsys.readouterr().out
    )


def test_unlisted_source_fails_the_package_only(
    run_main: Callable[..., int], capsys: pytest.CaptureFixture[str]
) -> None:
    """A tarball whose sources line differs fails its package and descendants, before building."""

    def prepare(project: Path) -> None:
        lines = (project / 'hello-lib' / 'sources').read_text()
        (project / 'hello-lib' / 'sources').write_text(lines.replace('= ', '= 0', 1)[:-2] + '\n')

    runner = FakeRunner(tools_respond)
    packages = ['hello-lib', 'hello-app', 'hello-undeclared']
    assert run_main(runner, {'debian': ['trixie']}, packages, prepare=prepare) == 1
    built = {p for p in packages if any(c.build_of(p) for c in runner.calls)}
    assert built == {'hello-undeclared'}
    out = capsys.readouterr().out
    assert (
        'failed hello-lib on Debian GNU/Linux 13 (Trixie): while downloading hello-lib-1.0.tar.gz:'
        ' hello-lib-1.0.tar.gz matches no line of the sources file' in out
    )


def test_missing_sources_file_fails_the_package(
    run_main: Callable[..., int], capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner(rpm_builds())

    def prepare(project: Path) -> None:
        (project / 'hello-lib' / 'sources').unlink()

    assert run_main(runner, {'fedora': ['44']}, ['hello-lib'], prepare=prepare) == 1
    assert 'no sources line for hello-lib-1.0.tar.gz' in capsys.readouterr().out


def test_verified_deb_source_needs_no_line(run_main: Callable[..., int]) -> None:
    """uscan checked the upstream signature: the tarball needs no sources line."""

    def prepare(project: Path) -> None:
        (project / 'hello-lib' / 'sources').unlink()
        debian = project / 'hello-lib' / 'debian'
        watch = debian / 'watch'
        watch.write_text(watch.read_text().replace('pgpmode=none', 'pgpmode=gittag'))
        (debian / 'upstream').mkdir()
        (debian / 'upstream' / 'signing-key.asc').write_text('the upstream key\n')

    runner = FakeRunner(tools_respond)
    assert run_main(runner, {'debian': ['trixie']}, ['hello-lib'], prepare=prepare) == 0


def test_extra_line_is_ignored(run_main: Callable[..., int]) -> None:
    def prepare(project: Path) -> None:
        with (project / 'hello-lib' / 'sources').open('a') as lines:
            lines.write(f'SHA512 (gone-0.1.tar.gz) = {"0" * 128}\n')

    runner = FakeRunner(tools_respond)
    assert run_main(runner, {'debian': ['trixie']}, ['hello-lib'], prepare=prepare) == 0


def test_sources_writes_lines_a_fresh_run_accepts(
    cli: ModuleType,
    recorded_http: respx.MockRouter,
    frozen: date,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sources writes, sorted, the line of each file the builds download; run then accepts them."""
    recorded_http.get(HELLO_LIB_ORIG, name='orig').respond(200, content=tarball())
    versions = {'debian': ['trixie'], 'fedora': ['44']}
    project = make_project(tmp_path / 'project', ORIG_BASE, versions, ['hello-lib', 'hello-app'])
    (project / 'hello-lib' / 'sources').unlink()
    monkeypatch.chdir(project)
    app = cli.make_app(runner=FakeRunner(tools_respond), connect=lambda host: cast(Any, None))
    env = {'DISTROPACK_GPG_PASSPHRASE': FIXTURE_PASSPHRASE}
    result = CliRunner().invoke(app, ['sources', 'build.conf', 'hello-lib', 'hello-app'], env=env)
    assert result.exit_code == 0, result.output
    digest = hashlib.sha512(tarball()).hexdigest()
    # the deb tarball (the fake uscan's) and the rpm one are the same file here
    assert (project / 'hello-lib' / 'sources').read_text() == (
        f'SHA512 (hello-lib-1.0.tar.gz) = {digest}\n'
    )
    assert not (project / 'hello-app' / 'sources').exists()  # downloads nothing
    runner = FakeRunner(rpm_builds())
    result = CliRunner().invoke(
        cli.make_app(runner=runner, connect=lambda host: cast(Any, None)),
        ['run', 'build.conf'],
        env=env,
    )
    assert result.exit_code == 0, result.output


def test_sources_unknown_package(
    cli: ModuleType, recorded_http: respx.MockRouter, frozen: date, tmp_path: Path
) -> None:
    project = make_project(tmp_path / 'project', ORIG_BASE, {'debian': ['trixie']}, ['hello-lib'])
    app = cli.make_app(runner=FakeRunner(tools_respond), connect=lambda host: cast(Any, None))
    result = CliRunner().invoke(app, ['sources', str(project / 'build.conf'), 'nosuch'])
    assert result.exit_code == 2
    assert 'nosuch is not a package of' in result.stderr


def test_sources_of_one_member_of_a_shared_download(
    cli: ModuleType,
    recorded_http: respx.MockRouter,
    frozen: date,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sources run for one package alone suits a run where another downloads their tarball.

    hello-lib2 shares hello-lib's watch file and version; its tarball is named after whichever
    package runs uscan, hello-lib in a full run, hello-lib2 when sources runs for it alone.
    """
    project = make_project(tmp_path / 'project', ORIG_BASE, {'debian': ['trixie']}, ['hello-lib'])
    add_quilt_package(project, 'hello-lib2', '1.0')
    (project / 'hello-lib2' / 'sources').unlink()
    monkeypatch.chdir(project)
    env = {'DISTROPACK_GPG_PASSPHRASE': FIXTURE_PASSPHRASE}
    app = cli.make_app(runner=FakeRunner(tools_respond), connect=lambda host: cast(Any, None))
    result = CliRunner().invoke(app, ['sources', 'build.conf', 'hello-lib2'], env=env)
    assert result.exit_code == 0, result.output
    assert 'hello-lib2-1.0.tar.gz' in (project / 'hello-lib2' / 'sources').read_text()
    runner = FakeRunner(tools_respond)
    app = cli.make_app(runner=runner, connect=lambda host: cast(Any, None))
    result = CliRunner().invoke(app, ['run', 'build.conf'], env=env)
    assert result.exit_code == 0, result.output
    assert uscan_calls(runner)[0].workdir == '/project/hello-lib'


def test_sources_says_when_a_package_downloads_nothing(
    cli: ModuleType,
    recorded_http: respx.MockRouter,
    frozen: date,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = {'debian': ['trixie'], 'fedora': ['44']}
    make_project(tmp_path / 'project', ORIG_BASE, versions, ['hello-lib', 'hello-app'])
    monkeypatch.chdir(tmp_path / 'project')
    app = cli.make_app(runner=FakeRunner(tools_respond), connect=lambda host: cast(Any, None))
    result = CliRunner().invoke(app, ['sources', '-D', 'fedora', 'build.conf', 'hello-app'])
    assert result.exit_code == 0, result.output
    assert 'hello-app downloads nothing for the releases selected' in result.stderr


def test_sources_query_failure_is_a_message(
    cli: ModuleType,
    recorded_http: respx.MockRouter,
    frozen: date,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_project(tmp_path / 'project', ORIG_BASE, {'fedora': ['44']}, ['hello-lib'])
    monkeypatch.chdir(tmp_path / 'project')

    def respond(call: Call) -> RunResult | None:
        if call.args[:1] == ('query',):
            raise ContainerFailed(125, [call.image], output='manifest unknown\n')
        return tools_respond(call)

    app = cli.make_app(runner=FakeRunner(respond), connect=lambda host: cast(Any, None))
    result = CliRunner().invoke(app, ['sources', 'build.conf', 'hello-lib'])
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert 'hello-lib:' in result.stderr


@pytest.mark.parametrize(('dist', 'release'), [('debian', 'trixie'), ('fedora', '44')])
def test_build_runs_without_network(run_main: Callable[..., int], dist: str, release: str) -> None:
    """Dependencies installed with network and committed; the build from that image, without."""
    runner = FakeRunner(rpm_builds() if dist == 'fedora' else tools_respond)
    assert run_main(runner, {dist: [release]}, ['hello-undeclared']) == 0
    (deps,) = [c for c in runner.calls if c.is_deps]
    (build,) = [c for c in runner.calls if c.is_build]
    assert deps.network and deps.commit_to is not None and not deps.remove_image
    assert build.image == deps.commit_to
    assert not build.network and build.remove_image and build.commit_to is None
    assert '/localrepo' in deps.targets and '/localrepo' not in build.targets


def test_deps_failure_runs_no_build(
    run_main: Callable[..., int], capsys: pytest.CaptureFixture[str]
) -> None:
    def respond(call: Call) -> RunResult | None:
        if call.is_deps:
            raise ContainerFailed(100, [call.image, *call.args], output='E: unmet dependencies\n')
        return tools_respond(call)

    runner = FakeRunner(respond)
    assert run_main(runner, {'debian': ['trixie']}, ['hello-undeclared']) == 1
    assert not [c for c in runner.calls if c.is_build]


def add_extra(project: Path, dist: str, url: str) -> None:
    """An extra repository for dist in build.conf, signed by the fixture's key."""
    signer = Signer.load(project / 'key.gpg', FIXTURE_PASSPHRASE)
    (project / 'extra.asc').write_bytes(signer.public_key(armored=True))
    conf = yaml.safe_load((project / 'build.conf').read_text())
    conf['distribs'][dist]['extra_repositories'] = [
        {'url': url, 'key': 'extra.asc', 'name': 'pdi', 'key_url': 'https://repo.pdi.dev/pdi.asc'}
    ]
    (project / 'build.conf').write_text(yaml.safe_dump(conf))


@pytest.mark.parametrize(
    ('dist', 'release', 'url', 'file', 'expected'),
    [
        (
            'debian',
            'trixie',
            'https://repo.pdi.dev/debian/',
            'pdi.sources',
            'Types: deb\nURIs: https://repo.pdi.dev/debian/\nSuites: trixie\nComponents: main\n'
            'Signed-By: /extrarepos/pdi.asc\n',
        ),
        (
            'fedora',
            '44',
            'https://repo.pdi.dev/fedora/{version}',
            'pdi.repo',
            '[pdi]\nname=pdi\nbaseurl=https://repo.pdi.dev/fedora/44\nenabled=1\ngpgcheck=1\n'
            'repo_gpgcheck=1\ngpgkey=file:///extrarepos/pdi.asc\n',
        ),
    ],
)
def test_extra_repositories_reach_the_dependency_container(
    run_main: Callable[..., int], dist: str, release: str, url: str, file: str, expected: str
) -> None:
    seen: dict[str, str] = {}

    def respond(call: Call) -> RunResult | None:
        if call.is_deps:
            extra = call.targets['/extrarepos']
            assert extra.read_only
            seen.update({p.name: p.read_text() for p in extra.source.iterdir()})
        if call.is_build:
            assert '/extrarepos' not in call.targets
        return (rpm_builds() if dist == 'fedora' else tools_respond)(call)

    runner = FakeRunner(respond)
    prepare = partial(add_extra, dist=dist, url=url)
    assert run_main(runner, {dist: [release]}, ['hello-undeclared'], prepare=prepare) == 0
    assert seen[file] == expected
    assert seen['pdi.asc'].startswith('-----BEGIN PGP PUBLIC KEY BLOCK-----')


def test_file_extra_repository_is_mounted(run_main: Callable[..., int], tmp_path: Path) -> None:
    mirror = tmp_path / 'mirror'
    mirror.mkdir()
    sources: list[str] = []

    def respond(call: Call) -> RunResult | None:
        if call.is_deps:
            sources.append((call.targets['/extrarepos'].source / 'pdi.sources').read_text())
        return tools_respond(call)

    runner = FakeRunner(respond)
    prepare = partial(add_extra, dist='debian', url=f'file://{mirror}/')
    assert run_main(runner, {'debian': ['trixie']}, ['hello-undeclared'], prepare=prepare) == 0
    (deps,) = [c for c in runner.calls if c.is_deps]
    assert deps.targets['/extramirrors/pdi'].source == mirror
    assert deps.targets['/extramirrors/pdi'].read_only
    assert 'URIs: file:///extramirrors/pdi/\n' in sources[0]


def test_binary_extra_key_is_written_armored(run_main: Callable[..., int]) -> None:
    """apt before 3.0 reads a Signed-By file by its extension: the .asc must be armored."""
    seen: dict[str, bytes] = {}

    def prepare(project: Path) -> None:
        add_extra(project, dist='debian', url='https://repo.pdi.dev/debian/')
        signer = Signer.load(project / 'key.gpg', FIXTURE_PASSPHRASE)
        (project / 'extra.asc').write_bytes(signer.public_key(armored=False))

    def respond(call: Call) -> RunResult | None:
        if call.is_deps:
            seen['key'] = (call.targets['/extrarepos'].source / 'pdi.asc').read_bytes()
        return tools_respond(call)

    assert (
        run_main(FakeRunner(respond), {'debian': ['trixie']}, ['hello-undeclared'], prepare=prepare)
        == 0
    )
    assert seen['key'].startswith(b'-----BEGIN PGP PUBLIC KEY BLOCK-----')


def test_zypper_deps_trusts_only_the_configured_keys() -> None:
    """--gpg-auto-import-keys would trust whatever key an extra repository presents."""
    entrypoint = (
        Path(builders_module.__file__).parent / 'images' / 'zypper_builder' / 'entrypoint'
    ).read_text()
    deps = entrypoint[entrypoint.index('deps)') : entrypoint.index('build)')]
    assert '--gpg-auto-import-keys' not in deps


@pytest.mark.parametrize(
    ('now', 'tz'),
    [
        (datetime(2026, 11, 9, 12, tzinfo=UTC), 'UTC'),
        (datetime(2031, 1, 1, tzinfo=UTC), 'Asia/Tokyo'),
    ],
)
def test_generated_entry_dated_from_the_packaging(
    run_main: Callable[..., int], monkeypatch: pytest.MonkeyPatch, now: datetime, tz: str
) -> None:
    """The rebuild's entry: the base version, dated as the packaging's top entry, any clock."""
    monkeypatch.setenv('TZ', tz)
    entries: list[tuple[str, str | None]] = []

    def respond(call: Call) -> RunResult | None:
        if call.is_deps:
            (source,) = [d for d in call.targets['/src'].source.iterdir() if d.is_dir()]
            (top, *_) = Changelog((source / 'debian' / 'changelog').read_text())
            entries.append((str(top.version), top.date))
        return tools_respond(call)

    packaged = Changelog(
        (FIXTURE_PROJECT / 'hello-undeclared' / 'debian' / 'changelog').read_text()
    )
    with time_machine.travel(now, tick=False):
        assert run_main(FakeRunner(respond), {'debian': ['trixie']}, ['hello-undeclared']) == 0
    assert entries == [(f'{packaged.version}~bpo13', packaged.date)]


def test_download_dir_kept_across_runs(run_main: Callable[..., int], tmp_path: Path) -> None:
    """--download-dir keeps the downloads, uscan's included, in the same place from run to run."""
    kept = tmp_path / 'kept'
    places: list[Path] = []

    def respond(call: Call) -> RunResult | None:
        if call.args[:1] == ('uscan',):
            places.append(call.targets['/downloads'].source)
        return tools_respond(call)

    for _ in range(2):
        shutil.rmtree(tmp_path / 'project', ignore_errors=True)
        runner = FakeRunner(respond)
        assert (
            run_main(runner, {'debian': ['trixie']}, ['hello-lib'], '--download-dir', str(kept))
            == 0
        )
    assert len(places) == 2 and places[0] == places[1]
    assert places[0].is_relative_to(kept)
    assert list((kept / 'origs').glob('*/hello-lib-1.0.tar.gz'))


@pytest.mark.parametrize(('dist', 'name'), [('debian', 'trixie'), ('fedora', '44')])
def test_deps_reports_its_environment(
    builders: ModuleType,
    context: BuildContext,
    runner: FakeRunner,
    frozen: date,
    dist: str,
    name: str,
) -> None:
    """The dependency container writes what it installed; its sha256 is the environment key."""

    def respond(call: Call) -> RunResult | None:
        if call.is_deps:
            (call.targets['/buildenv'].source / 'installed').write_text(INSTALLED)
        if call.is_build:
            raise StopBuild()
        return None

    runner.respond = respond
    under = builder(builders, context, release(dist, name, frozen))
    with pytest.raises(StopBuild):
        under.build(frozenset(), {})
    deps = builder_call(runner)
    assert not deps.targets['/buildenv'].read_only
    assert 'DISTROPACK_KNOWN_ENV' not in deps.env
    assert deps.commit_unless == deps.targets['/buildenv'].source / 'unchanged'
    assert under.env_key == hashlib.sha256(INSTALLED.encode()).hexdigest()
    assert not under.unchanged


def reproducibility(differ: set[str] | None = None) -> Callable[[Call], RunResult | None]:
    """Builders writing the same packages twice, but the files of differ in the second build."""
    builds: list[int] = [0]

    def respond(call: Call) -> RunResult | None:
        if call.is_build:
            builds[0] += 1
            out = call.targets.get('/output', call.targets['/src']).source
            for name in ('hello-undeclared_1.0~bpo13_all.deb', 'hello-undeclared_1.0~bpo13.dsc'):
                content = name.encode()
                if builds[0] == 2 and differ and name in differ:
                    content += b' differs'
                (out / name).write_bytes(content)
            # a .buildinfo records the build: it may differ, and is not compared
            (out / 'hello-undeclared_1.0~bpo13_amd64.buildinfo').write_text(f'build {builds[0]}\n')
            return None
        if call.args[:1] == ('diffoscope',):
            return RunResult(1, '--- a/x.deb\n+++ b/x.deb\n│ -x\n│ +x differs\n', '')
        return tools_respond(call)

    return respond


def test_check_reproducible_builds_twice_varied(run_main: Callable[..., int]) -> None:
    runner = FakeRunner(reproducibility())
    status = run_main(runner, {'debian': ['trixie']}, ['hello-undeclared'], '--check-reproducible')
    assert status == 0
    first, second = [c for c in runner.calls if c.is_build]
    assert first.image == second.image
    assert not first.remove_image and second.remove_image
    assert first.hostname == 'builder' and second.hostname != 'builder'
    varied = {k: v for k, v in second.env.items() if first.env.get(k) != v}
    assert set(varied) >= {'TZ', 'LC_ALL', 'DISTROPACK_UMASK', 'DISTROPACK_BUILD_PATH'}
    assert first.args != second.args  # -j


def test_unreproducible_package_fails(
    run_main: Callable[..., int], capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner(reproducibility({'hello-undeclared_1.0~bpo13_all.deb'}))
    status = run_main(runner, {'debian': ['trixie']}, ['hello-undeclared'], '--check-reproducible')
    assert status == 1
    out = capsys.readouterr().out
    assert (
        'unreproducible hello-undeclared on Debian GNU/Linux 13 (Trixie):'
        ' hello-undeclared_1.0~bpo13_all.deb' in out
    )
    assert '+x differs' in out  # diffoscope's report, in the failed build's output
    diffoscopes = [c for c in runner.calls if c.args[:1] == ('diffoscope',)]
    assert [c.args[-1] for c in diffoscopes] == ['/second/hello-undeclared_1.0~bpo13_all.deb']
    assert diffoscopes[0].image.endswith('/deb_tools:p' + str(PROTOCOL))


def test_differing_buildinfo_alone_passes(run_main: Callable[..., int]) -> None:
    runner = FakeRunner(reproducibility())
    status = run_main(runner, {'debian': ['trixie']}, ['hello-undeclared'], '--check-reproducible')
    assert status == 0
    assert not [c for c in runner.calls if c.args[:1] == ('diffoscope',)]


def test_build_has_a_fixed_host_name_and_no_host_variable(run_main: Callable[..., int]) -> None:
    runner = FakeRunner(tools_respond)
    assert run_main(runner, {'debian': ['trixie']}, ['hello-undeclared']) == 0
    (build,) = [c for c in runner.calls if c.is_build]
    assert build.hostname == 'builder'
    assert set(build.env) == {'DH_VERBOSE', 'DISTROPACK_BUILD_PROFILES'}


def test_rpm_check_keeps_the_host_name(run_main: Callable[..., int]) -> None:
    """rpm records the build host in every package: varying it would make none reproducible."""

    def respond(call: Call) -> RunResult | None:
        if call.is_build:
            out = call.targets['/output'].source
            (out / 'hello-undeclared-1.0-1.fc44.noarch.rpm').write_bytes(RPM)
            return None
        return tools_respond(call)

    runner = FakeRunner(respond)
    status = run_main(runner, {'fedora': ['44']}, ['hello-undeclared'], '--check-reproducible')
    assert status == 0
    first, second = [c for c in runner.calls if c.is_build]
    assert first.hostname == second.hostname == 'builder'
    assert 'DISTROPACK_BUILD_PATH' not in second.env  # the source rpm records the spec expanded


# The build modes (Phase 12): hello-app, native, an Architecture: any package and an all one.
APP_BASE = '1.0~bpo13'
APP_SOURCE = (f'hello-app_{APP_BASE}.dsc', f'hello-app_{APP_BASE}.tar.xz')
APP_ALL = f'hello-app-doc_{APP_BASE}_all.deb'
ENV_KEY = hashlib.sha256(INSTALLED.encode()).hexdigest()
UNVERSIONED = (
    'hello-app: sources or packaging changed without a new version '
    '(add a debian/changelog entry / bump Release in the spec)'
)


def fixture_signer() -> Signer:
    return Signer.load(FIXTURE_PROJECT / 'key.gpg', FIXTURE_PASSPHRASE)


def app_key() -> str:
    """The static key of the fixture's hello-app deb: its whole directory, native."""
    return state.static_key(FIXTURE_PROJECT / 'hello-app', {}, STABLE)


def write_app_sources(directory: Path, version: str) -> None:
    """A native source package of hello-app at version, its .dsc listing its tarball."""
    (directory / f'hello-app_{version}.tar.xz').write_bytes(b'source of ' + version.encode())
    (directory / f'hello-app_{version}.dsc').write_text(
        f'Format: 3.0 (native)\nSource: hello-app\nVersion: {version}\nFiles:\n'
        f' 0123456789abcdef0123456789abcdef 14 hello-app_{version}.tar.xz\n'
    )


def stored_app(
    tmp_path: Path,
    *,
    base: str = APP_BASE,
    rebuild: int = 0,
    key: str | None = None,
    env_key: str = 'stored-environment',
) -> state.StoredPackage:
    """hello-app as a committed state of trixie stores it, read back."""
    built = tmp_path / 'stored-build'
    built.mkdir()
    version = base if rebuild == 0 else f'{base}+b{rebuild}'
    write_app_sources(built, base)
    (built / APP_ALL).write_bytes(b'doc deb')
    (built / f'hello-app_{version}_amd64.deb').write_bytes(b'app deb')
    root = tmp_path / 'state'
    writer = state.ReleaseStateWriter(root, STABLE, 'debian-13-trixie')
    writer.add(
        'hello-app',
        static_key=app_key() if key is None else key,
        env_key=env_key,
        base_version=base,
        rebuild=rebuild,
        version=version,
        snapshot_commit=None,
        packaging_hash='packaging',
        outdir=built,
        source_files=list(APP_SOURCE),
        arch_all_files=[APP_ALL],
    )
    writer.commit()
    found = state.ReleaseState(root, STABLE, 'debian-13-trixie').package('hello-app')
    assert found is not None
    return found


def app_builder(
    builders: ModuleType, context: BuildContext, rel: Release, stored: Any = None
) -> Any:
    under = builders.DebPackageBuilder(
        context, rel, FIXTURE_PROJECT / 'hello-app', fixture_signer(), 'hello-app', channel=STABLE
    )
    under.stored = stored
    return under


def deb_build(
    version: str = APP_BASE, *, sourceful: bool = True, unchanged: bool = False
) -> Callable[[Call], RunResult | None]:
    """deps reports INSTALLED (unchanged if asked); build writes what dpkg-buildpackage does."""

    def respond(call: Call) -> RunResult | None:
        if call.is_deps:
            buildenv = call.targets['/buildenv'].source
            (buildenv / 'installed').write_text(INSTALLED)
            if unchanged:
                (buildenv / 'unchanged').touch()
        if call.is_build:
            src = call.targets['/src'].source
            if sourceful:
                write_app_sources(src, version)
                (src / f'hello-app-doc_{version}_all.deb').write_bytes(b'new doc deb')
            (src / f'hello-app_{version}_amd64.deb').write_bytes(b'new app deb')
            (src / f'hello-app_{version}_amd64.buildinfo').write_text('buildinfo')
            (src / f'hello-app_{version}_amd64.changes').write_text('changes')
        return None

    return respond


def test_no_state_builds_sourceful(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date
) -> None:
    runner.respond = deb_build()
    under = app_builder(builders, context, release('debian', 'trixie', frozen))
    assert under.build(frozenset(), {}) is None
    assert 'DISTROPACK_KNOWN_ENV' not in builder_call(runner).env
    assert '-sa' in build_call(runner).args and '-B' not in build_call(runner).args
    assert (under.base_version, under.rebuild, under.version) == (APP_BASE, 0, APP_BASE)
    assert under.static_key == app_key()
    assert under.packaging_hash == state.packaging_key(FIXTURE_PROJECT / 'hello-app')
    assert under.env_key == ENV_KEY
    assert sorted(under.source_files) == sorted(APP_SOURCE)
    assert under.arch_all_files == [APP_ALL]


def test_other_base_builds_sourceful(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date, tmp_path: Path
) -> None:
    runner.respond = deb_build()
    stored = stored_app(tmp_path, base='0.9~bpo13', rebuild=3, key='other')
    under = app_builder(builders, context, release('debian', 'trixie', frozen), stored)
    assert under.build(frozenset(), {}) is None
    assert 'DISTROPACK_KNOWN_ENV' not in builder_call(runner).env
    assert '-sa' in build_call(runner).args
    assert (under.rebuild, under.version) == (0, APP_BASE)


def test_sid_changelog_used_unchanged(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date
) -> None:
    """On a release without a version, the base is the changelog's: no generated entry."""
    tops: list[list[str]] = []

    def respond(call: Call) -> RunResult | None:
        if call.is_deps:
            (source,) = [d for d in call.targets['/src'].source.iterdir() if d.is_dir()]
            tops.append(
                [str(b.version) for b in Changelog((source / 'debian/changelog').read_text())]
            )
        return deb_build('1.0')(call)

    runner.respond = respond
    under = app_builder(builders, context, release('debian', 'sid', frozen))
    under.build(frozenset(), {})
    assert tops == [['1.0']]
    assert under.version == '1.0'


def test_changed_packaging_same_version_refused(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date, tmp_path: Path
) -> None:
    stored = stored_app(tmp_path, key='the key of other packaging')
    under = app_builder(builders, context, release('debian', 'trixie', frozen), stored)
    with pytest.raises(builders.UnversionedChange) as raised:
        under.build(frozenset(), {})
    assert str(raised.value) == UNVERSIONED
    assert runner.calls == []


def test_same_key_passes_the_known_environment(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date, tmp_path: Path
) -> None:
    runner.respond = deb_build(f'{APP_BASE}+b1', sourceful=False)
    stored = stored_app(tmp_path, env_key='abc')
    under = app_builder(builders, context, release('debian', 'trixie', frozen), stored)
    under.build(frozenset(), {})
    assert builder_call(runner).env['DISTROPACK_KNOWN_ENV'] == 'abc'


def test_binnmu_layout(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}
    build = deb_build(f'{APP_BASE}+b1', sourceful=False)

    def respond(call: Call) -> RunResult | None:
        if call.is_deps:
            src = call.targets['/src'].source
            seen['files'] = sorted(p.name for p in src.iterdir())
            seen['inodes'] = {p.name: p.stat().st_ino for p in src.iterdir()}
            seen['changelog'] = (src / 'binnmu.changelog').read_text()
        return build(call)

    runner.respond = respond
    stored = stored_app(tmp_path)
    under = app_builder(builders, context, release('debian', 'trixie', frozen), stored)
    under.build(frozenset(), {})
    assert seen['files'] == sorted([*APP_SOURCE, 'binnmu.changelog'])
    for name in APP_SOURCE:  # copies: the build chowns /src, the state's files stay as they are
        assert seen['inodes'][name] != (stored.directory / name).stat().st_ino
    changelog = seen['changelog']
    assert changelog.startswith(
        f'hello-app ({APP_BASE}+b1) trixie; urgency=medium, binary-only=yes\n'
    )
    assert 'Binary-only non-maintainer upload for amd64;' in changelog
    packaged = Changelog((FIXTURE_PROJECT / 'hello-app/debian/changelog').read_text())
    assert f' -- {fixture_signer().uid}  {packaged.date}\n' in changelog
    assert changelog.endswith('\n\n')  # a blank line before the stored changelog
    args = build_call(runner).args
    assert '-B' in args and '-sa' not in args


def test_unchanged_environment_is_reused(
    builders: ModuleType,
    context: BuildContext,
    runner: FakeRunner,
    frozen: date,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner.respond = deb_build(unchanged=True)
    stored = stored_app(tmp_path, rebuild=2, env_key=ENV_KEY)
    under = app_builder(builders, context, release('debian', 'trixie', frozen), stored)
    monkeypatch.setattr(type(under._gpg), 'clearsign', lambda *a: pytest.fail('signed'))
    assert under.build(frozenset(), {}) == Reused()
    assert not [c for c in runner.calls if c.is_build]
    assert under.outdir == stored.directory
    assert (under.rebuild, under.version) == (2, f'{APP_BASE}+b2')
    assert under.env_key == ENV_KEY
    assert (tuple(under.source_files), tuple(under.arch_all_files)) == (
        stored.source_files,
        stored.arch_all_files,
    )


def test_binnmu_output_keeps_the_stored_source(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date, tmp_path: Path
) -> None:
    runner.respond = deb_build(f'{APP_BASE}+b1', sourceful=False)
    stored = stored_app(tmp_path)
    stored_dsc = (stored.directory / APP_SOURCE[0]).read_bytes()
    under = app_builder(builders, context, release('debian', 'trixie', frozen), stored)
    assert under.build(frozenset(), {}) is None
    out = under.outdir
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [
            *APP_SOURCE,
            APP_ALL,
            f'hello-app_{APP_BASE}+b1_amd64.deb',
            f'hello-app_{APP_BASE}+b1_amd64.buildinfo',
            f'hello-app_{APP_BASE}+b1_amd64.changes',
        ]
    )
    for name in [*APP_SOURCE, APP_ALL]:
        assert (out / name).stat().st_ino == (stored.directory / name).stat().st_ino
    assert (out / APP_SOURCE[0]).read_bytes() == stored_dsc  # not signed again
    assert (under.rebuild, under.version) == (1, f'{APP_BASE}+b1')
    assert (tuple(under.source_files), tuple(under.arch_all_files)) == (APP_SOURCE, (APP_ALL,))


def test_missing_stored_file_builds_sourceful(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date, tmp_path: Path
) -> None:
    """A stored file deleted by hand: the package is absent from the state, built sourceful."""
    stored = stored_app(tmp_path)
    (stored.directory / APP_SOURCE[1]).unlink()
    reread = state.ReleaseState(tmp_path / 'state', STABLE, 'debian-13-trixie')
    assert reread.package('hello-app') is None
    runner.respond = deb_build()
    under = app_builder(builders, context, release('debian', 'trixie', frozen), None)
    assert under.build(frozenset(), {}) is None
    assert '-sa' in build_call(runner).args and under.rebuild == 0


def test_no_environment_report_fails(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date
) -> None:
    runner.respond = lambda call: None
    under = app_builder(builders, context, release('debian', 'trixie', frozen))
    with pytest.raises(RuntimeError, match='the builder image reported no environment'):
        under.build(frozenset(), {})
    assert not [c for c in runner.calls if c.is_build]


def test_arch_all_only_rebuild_candidate_is_reused(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date, tmp_path: Path
) -> None:
    """hello-lib builds Architecture: all packages only, which a binNMU never rebuilds."""
    trixie = release('debian', 'trixie', frozen)
    under = builders.DebPackageBuilder(
        context,
        trixie,
        FIXTURE_PROJECT / 'hello-lib',
        fixture_signer(),
        'hello-lib',
        channel=STABLE,
    )
    tarball_path = context.workdir / 'hello-lib-1.0.tar.gz'
    tarball_path.write_bytes(b'upstream')
    orig = context.workdir / 'hello-lib_1.0.orig.tar.gz'
    orig.symlink_to(tarball_path.name)
    under.orig = OrigFiles(tarball_path, orig, True)
    key = state.static_key(FIXTURE_PROJECT / 'hello-lib' / 'debian', {orig.name: orig}, STABLE)
    built = tmp_path / 'built'
    built.mkdir()
    (built / 'hello-lib_1.0-1~bpo13_all.deb').write_bytes(b'lib deb')
    root = tmp_path / 'state'
    writer = state.ReleaseStateWriter(root, STABLE, trixie.uid)
    writer.add(
        'hello-lib',
        static_key=key,
        env_key='old',
        base_version='1.0-1~bpo13',
        rebuild=0,
        version='1.0-1~bpo13',
        snapshot_commit=None,
        packaging_hash='p',
        outdir=built,
        source_files=[],
        arch_all_files=['hello-lib_1.0-1~bpo13_all.deb'],
    )
    writer.commit()
    under.stored = state.ReleaseState(root, STABLE, trixie.uid).package('hello-lib')
    assert under.build(frozenset(), {}) == Reused()
    assert runner.calls == []
    assert under.outdir == under.stored.directory and under.version == '1.0-1~bpo13'


def test_binnmu_check_ignores_the_stored_source(
    builders: ModuleType, runner: FakeRunner, frozen: date, tmp_path: Path
) -> None:
    """The varied build of a binNMU moves back its debs only, as the first: no false difference."""
    workdir = tmp_path / 'checked'
    workdir.mkdir()
    checked = BuildContext(cast(Any, None), runner, Cancellation(), workdir, 1, True)
    runner.respond = deb_build(f'{APP_BASE}+b1', sourceful=False)
    under = app_builder(
        builders, checked, release('debian', 'trixie', frozen), stored_app(tmp_path)
    )
    assert under.build(frozenset(), {}) is None
    assert len([c for c in runner.calls if c.is_build]) == 2


# The build modes of an rpm: hello-app on Fedora 44, as builder() makes it.
APP_SPEC = FIXTURE_PROJECT / 'hello-app' / 'hello-app.spec'
APP_RPM_BASE = '1.0-1.fc44'


def rpm_build(
    release_: str = '1.fc44', *, unchanged: bool = False, seen: dict[str, str] | None = None
) -> Callable[[Call], RunResult | None]:
    """deps reports INSTALLED (unchanged if asked) and keeps the spec; build writes the rpms."""

    def respond(call: Call) -> RunResult | None:
        if call.is_deps:
            buildenv = call.targets['/buildenv'].source
            (buildenv / 'installed').write_text(INSTALLED)
            if unchanged:
                (buildenv / 'unchanged').touch()
            if seen is not None:
                (spec,) = call.targets['/src'].source.glob('*.spec')
                seen['spec'] = spec.read_text()
        if call.is_build:
            out = call.targets['/output'].source
            (out / f'hello-app-1.0-{release_}.src.rpm').write_bytes(b'srpm')
            (out / f'hello-app-1.0-{release_}.noarch.rpm').write_bytes(b'rpm')
        return None

    return respond


def stored_rpm(tmp_path: Path, spec: Path = APP_SPEC, *, key: str | None = None) -> Any:
    """hello-app as a committed state of Fedora 44 stores it, read back."""
    built = tmp_path / 'stored-rpm'
    built.mkdir()
    (built / f'hello-app-{APP_RPM_BASE}.src.rpm').write_bytes(b'stored srpm')
    (built / f'hello-app-{APP_RPM_BASE}.noarch.rpm').write_bytes(b'stored rpm')
    root = tmp_path / 'state'
    writer = state.ReleaseStateWriter(root, STABLE, 'fedora-44')
    writer.add(
        'hello-app',
        static_key=state.static_key(spec, {}, STABLE) if key is None else key,
        env_key='stored-environment',
        base_version=APP_RPM_BASE,
        rebuild=0,
        version=APP_RPM_BASE,
        snapshot_commit=None,
        packaging_hash='packaging',
        outdir=built,
        source_files=[f'hello-app-{APP_RPM_BASE}.src.rpm'],
        arch_all_files=[],
    )
    writer.commit()
    return state.ReleaseState(root, STABLE, 'fedora-44').package('hello-app')


def rpm_signs(runner: FakeRunner) -> list[Call]:
    return [c for c in runner.calls if 'rpm_sign:' in c.image]


def test_rpm_no_state_builds_as_written(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date
) -> None:
    seen: dict[str, str] = {}
    runner.respond = rpm_build(seen=seen)
    under = builder(builders, context, release('fedora', '44', frozen))
    assert under.build(frozenset(), {}) is None
    assert seen['spec'] == APP_SPEC.read_text()
    assert 'DISTROPACK_KNOWN_ENV' not in builder_call(runner).env
    assert (under.base_version, under.rebuild, under.version) == (APP_RPM_BASE, 0, APP_RPM_BASE)
    assert under.static_key == state.static_key(APP_SPEC, {}, STABLE)
    assert under.packaging_hash == state.packaging_key(APP_SPEC)
    assert under.source_files == [f'hello-app-{APP_RPM_BASE}.src.rpm']
    assert under.arch_all_files == []
    assert len(rpm_signs(runner)) == 1


def test_rpm_rebuild_rewrites_release(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date, tmp_path: Path
) -> None:
    seen: dict[str, str] = {}
    runner.respond = rpm_build('1.fc44.1', seen=seen)
    under = builder(builders, context, release('fedora', '44', frozen))
    under.stored = stored_rpm(tmp_path)
    assert under.build(frozenset(), {}) is None
    assert builder_call(runner).env['DISTROPACK_KNOWN_ENV'] == 'stored-environment'
    assert 'Release:        1.fc44.1\n' in seen['spec']
    uid = fixture_signer().uid
    assert (
        '%changelog\n'
        f'* Thu Nov 05 2026 {uid} - 1.0-1.fc44.1\n'
        '- Rebuilt against updated build dependencies\n'
        '\n'
        '* Thu Nov 05 2026 distropack test <test@example.invalid> - 1.0-1\n'
    ) in seen['spec']
    assert (under.rebuild, under.version) == (1, '1.0-1.fc44.1')
    assert under.source_files == ['hello-app-1.0-1.fc44.1.src.rpm']
    assert len(rpm_signs(runner)) == 1  # a full build: signed as any


def test_rpm_without_changelog_bumps_release_only(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date, tmp_path: Path
) -> None:
    project = tmp_path / 'project'
    (project / 'hello-app').mkdir(parents=True)
    spec = project / 'hello-app' / 'hello-app.spec'
    spec.write_text(APP_SPEC.read_text().partition('%changelog')[0])
    seen: dict[str, str] = {}
    runner.respond = rpm_build('1.fc44.1', seen=seen)
    info = SpecInfo(frozenset({'hello-lib'}), frozenset({'hello-app'}), (), '1.0', '1.fc44')
    under = builders.RpmPackageBuilder(
        context,
        release('fedora', '44', frozen),
        project / 'hello-app',
        fixture_signer(),
        'hello-app',
        channel=STABLE,
        spec=info,
    )
    under.stored = stored_rpm(tmp_path, spec)
    under.build(frozenset(), {})
    assert seen['spec'] == spec.read_text().replace('1%{?dist}\n', '1.fc44.1\n')


def test_rpm_changed_spec_refused(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date, tmp_path: Path
) -> None:
    under = builder(builders, context, release('fedora', '44', frozen))
    under.stored = stored_rpm(tmp_path, key='another spec')
    with pytest.raises(builders.UnversionedChange, match='bump Release in the spec'):
        under.build(frozenset(), {})
    assert runner.calls == []


def test_rpm_reused_not_signed(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date, tmp_path: Path
) -> None:
    runner.respond = rpm_build(unchanged=True)
    under = builder(builders, context, release('fedora', '44', frozen))
    under.stored = stored_rpm(tmp_path)
    assert under.build(frozenset(), {}) == Reused()
    assert rpm_signs(runner) == [] and not [c for c in runner.calls if c.is_build]
    assert under.outdir == under.stored.directory and under.version == APP_RPM_BASE


# A release keeps its last complete build (Phase 12): runs of hello-lib and hello-app.
BOTH = {'debian': ['trixie'], 'fedora': ['44']}
APPS = ['hello-lib', 'hello-app']


def environment(
    installed: str = INSTALLED, *, fail: tuple[str, str] | None = None, outputs: bool = True
) -> Callable[[Call], RunResult | None]:
    """The tools, deps reporting installed (unchanged when known), builds writing a package.

    fail, a (package, image name) pair, fails that build.
    """

    def respond(call: Call) -> RunResult | None:
        if call.is_deps:
            buildenv = call.targets['/buildenv'].source
            (buildenv / 'installed').write_text(installed)
            known = hashlib.sha256(installed.encode()).hexdigest()
            if call.env.get('DISTROPACK_KNOWN_ENV') == known:
                (buildenv / 'unchanged').touch()
            return None
        if call.is_build:
            package = call.targets['/src'].source.parent.name.split('.')[0]
            if fail is not None and package == fail[0] and fail[1] in call.image:
                raise ContainerFailed(1, 'build failed')
            if not outputs:
                pass
            elif '/output' in call.targets:  # an rpm builder: an rpm its repository indexes
                tag = call.image.rpartition(':')[2].removesuffix(f'-p{PROTOCOL}')
                rpm = call.targets['/output'].source / f'{package}-1.0-1.fc{tag}.noarch.rpm'
                rpm.write_bytes(RPM)
            else:
                (call.targets['/src'].source / f'{package}-{len(installed)}.pkg').write_text(
                    installed
                )
        return tools_respond(call)

    return respond


def kept_state(state_dir: Path, uid: str) -> dict[str, bytes]:
    """Every file of a release's state, by path: what must not change."""
    directory = state_dir / STABLE / uid
    return {
        str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()
    }


def stored_version(state_dir: Path, uid: str, package: str) -> str | None:
    found = state.ReleaseState(state_dir, STABLE, uid).package(package)
    return None if found is None else found.version


def fresh(tmp_path: Path) -> None:
    shutil.rmtree(tmp_path / 'project', ignore_errors=True)


def test_second_run_reuses(
    run_main: Callable[..., int], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    states = str(tmp_path / 'states')
    assert run_main(FakeRunner(environment()), BOTH, APPS, '--state-dir', states) == 0
    capsys.readouterr()
    fresh(tmp_path)
    runner = FakeRunner(environment())
    status = run_main(runner, BOTH, APPS, '--state-dir', states)
    out = capsys.readouterr().out
    assert status == 0, (out, capsys.readouterr().err)
    assert out.count('reused ') == 4 and 'built ' not in out
    assert not [c for c in runner.calls if c.is_build]


def test_new_environment_increments_the_rebuild(
    run_main: Callable[..., int], tmp_path: Path
) -> None:
    states = tmp_path / 'states'
    versions = []
    for installed in (INSTALLED, INSTALLED + 'libfoo 2\n', INSTALLED + 'libfoo 3\n'):
        fresh(tmp_path)
        assert (
            run_main(FakeRunner(environment(installed)), BOTH, APPS, '--state-dir', str(states))
            == 0
        )
        versions.append(
            (
                stored_version(states, 'debian-13-trixie', 'hello-app'),
                stored_version(states, 'debian-13-trixie', 'hello-lib'),
                stored_version(states, 'fedora-44', 'hello-app'),
            )
        )
    assert versions == [
        ('1.0~bpo13', '1.0-1~bpo13', '1.0-1%{?dist}'),
        ('1.0~bpo13+b1', '1.0-1~bpo13', '1.0-1%{?dist}.1'),  # hello-lib: Architecture: all
        ('1.0~bpo13+b2', '1.0-1~bpo13', '1.0-1%{?dist}.2'),
    ]


def test_failed_release_keeps_its_state(
    run_main: Callable[..., int], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    states = tmp_path / 'states'
    assert run_main(FakeRunner(environment()), BOTH, APPS, '--state-dir', str(states)) == 0
    before = kept_state(states, 'debian-13-trixie')
    capsys.readouterr()
    fresh(tmp_path)
    failing = environment(INSTALLED + 'libfoo 2\n', fail=('hello-app', 'debian_builder'))
    assert run_main(FakeRunner(failing), BOTH, APPS, '--state-dir', str(states)) == 1
    assert kept_state(states, 'debian-13-trixie') == before
    assert stored_version(states, 'fedora-44', 'hello-app') == '1.0-1%{?dist}.1'
    out = capsys.readouterr().out
    assert 'kept the previous state of Debian GNU/Linux 13 (Trixie)' in out
    assert 'Fedora' not in ''.join(line for line in out.splitlines() if 'kept the' in line)


def test_removed_package_not_carried_over(run_main: Callable[..., int], tmp_path: Path) -> None:
    states = tmp_path / 'states'
    assert run_main(FakeRunner(environment()), BOTH, APPS, '--state-dir', str(states)) == 0
    fresh(tmp_path)
    assert run_main(FakeRunner(environment()), BOTH, ['hello-lib'], '--state-dir', str(states)) == 0
    for uid in ('debian-13-trixie', 'fedora-44'):
        stored = state.ReleaseState(states, STABLE, uid).packages()
        assert sorted(stored) == ['hello-lib']


def test_reused_dependency_feeds_its_dependent(
    run_main: Callable[..., int], tmp_path: Path
) -> None:
    """hello-lib reused (Architecture: all), hello-app rebuilt against the stored hello-lib."""
    states = tmp_path / 'states'
    assert run_main(FakeRunner(environment()), BOTH, APPS, '--state-dir', str(states)) == 0
    lib = state.ReleaseState(states, STABLE, 'debian-13-trixie').package('hello-lib')
    assert lib is not None
    files = sorted(lib.directory.iterdir())
    assert files
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
    fresh(tmp_path)
    runner = FakeRunner(environment(INSTALLED + 'libfoo 2\n'))
    assert run_main(runner, BOTH, APPS, '--state-dir', str(states)) == 0
    added = [
        c.targets['/data'].source
        for c in runner.calls
        if c.args[2:4] == ('repo', 'add') and '/localrepo' not in c.targets
    ]
    assert lib.directory in added  # the local repository of hello-app's binNMU, and publication
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files} == before


def test_interrupt_commits_nothing_more(
    run_main: Callable[..., int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    states = tmp_path / 'states'
    assert run_main(FakeRunner(environment()), BOTH, APPS, '--state-dir', str(states)) == 0
    before = {uid: kept_state(states, uid) for uid in ('debian-13-trixie', 'fedora-44')}
    fresh(tmp_path)
    changed = environment(INSTALLED + 'libfoo 2\n')

    def respond(call: Call) -> RunResult | None:
        if call.is_build and 'fedora_builder' in call.image:
            signal.raise_signal(signal.SIGTERM)
            raise Cancelled()
        return changed(call)

    assert run_main(FakeRunner(respond), BOTH, APPS, '--state-dir', str(states), '-j', '1') == 130
    assert {uid: kept_state(states, uid) for uid in before} == before
    assert sorted(p.name for p in (states / STABLE).iterdir()) == [
        'debian-13-trixie',
        'fedora-44',
    ]


# Publishing from the states (Phase 12).
def debian_dists(tmp_path: Path) -> list[str]:
    dists = tmp_path / 'project' / 'repositories' / 'debian' / 'dists'
    return sorted(d.name for d in dists.iterdir()) if dists.is_dir() else []


def test_no_publish_writes_no_repository(run_main: Callable[..., int], tmp_path: Path) -> None:
    states = tmp_path / 'states'
    runner = FakeRunner(environment())
    assert run_main(runner, BOTH, APPS, '--state-dir', str(states), '--no-publish') == 0
    assert stored_version(states, 'debian-13-trixie', 'hello-app') == '1.0~bpo13'
    assert not (tmp_path / 'project' / 'repositories').exists()
    repositories = tmp_path / 'project' / 'repositories'
    assert not [
        c
        for c in runner.calls
        if 'publish' in c.args and c.targets['/aptlyout'].source.is_relative_to(repositories)
    ]


def test_publish_only_runs_no_build(run_main: Callable[..., int], tmp_path: Path) -> None:
    states = tmp_path / 'states'
    assert (
        run_main(FakeRunner(environment()), BOTH, APPS, '--state-dir', str(states), '--no-publish')
        == 0
    )
    fresh(tmp_path)
    runner = FakeRunner(environment())
    assert run_main(runner, BOTH, APPS, '--state-dir', str(states), '--publish-only') == 0
    assert not [c for c in runner.calls if '_builder:' in c.image]
    assert debian_dists(tmp_path) == ['stable', 'trixie']
    assert indexed(tmp_path / 'project') == ['44']
    stored = state.ReleaseState(states, STABLE, 'debian-13-trixie').packages()
    added = {c.targets['/data'].source for c in runner.calls if c.args[2:4] == ('repo', 'add')}
    assert added == {package.directory for package in stored.values()}


def test_publish_only_skips_a_release_without_state(
    run_main: Callable[..., int], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    states = str(tmp_path / 'states')
    assert (
        run_main(
            FakeRunner(environment()),
            BOTH,
            APPS,
            '--state-dir',
            states,
            '-D',
            'fedora',
            '--no-publish',
        )
        == 0
    )
    fresh(tmp_path)
    capsys.readouterr()
    assert (
        run_main(FakeRunner(environment()), BOTH, APPS, '--state-dir', states, '--publish-only')
        == 0
    )
    assert 'no state for Debian GNU/Linux 13 (Trixie)' in capsys.readouterr().out
    assert indexed(tmp_path / 'project') == ['44']
    assert debian_dists(tmp_path) == []


def test_publish_only_needs_state_dir(
    run_main: Callable[..., int], capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner(environment())
    assert run_main(runner, BOTH, APPS, '--publish-only') == 2
    assert '--publish-only needs --state-dir' in capsys.readouterr().err
    assert runner.calls == []


def test_publish_modes_exclusive(
    run_main: Callable[..., int], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner(environment())
    args = ('--state-dir', str(tmp_path / 's'), '--publish-only', '--no-publish')
    assert run_main(runner, BOTH, APPS, *args) == 2
    assert '--publish-only and --no-publish' in capsys.readouterr().err
    assert runner.calls == []


def test_readme_lists_the_releases_with_state(run_main: Callable[..., int], tmp_path: Path) -> None:
    """A -D run publishes every release with a state, not only those it built (§8's defect)."""
    states = str(tmp_path / 'states')
    two = {'debian': ['bookworm', 'trixie']}
    assert run_main(FakeRunner(environment()), two, ['hello-app'], '--state-dir', states) == 0
    fresh(tmp_path)
    runner = FakeRunner(environment())
    assert run_main(runner, two, ['hello-app'], '--state-dir', states, '-D', 'debian:trixie') == 0
    assert not [c for c in runner.calls if c.is_deps and 'bookworm' in c.image]
    readme = (tmp_path / 'project' / 'repositories' / 'debian' / 'index.html').read_text()
    assert 'codename bookworm' in readme and 'codename trixie' in readme
    assert debian_dists(tmp_path) == ['bookworm', 'oldstable', 'stable', 'trixie']


def test_state_dir_rebuilds_the_repository(run_main: Callable[..., int], tmp_path: Path) -> None:
    """With --state-dir, what the repository held before is gone: it is the states' alone."""

    def stale(project: Path) -> None:
        old = project / 'repositories' / 'debian' / 'dists' / 'buster'
        old.mkdir(parents=True)
        (old / 'Release').write_text('a release no longer configured\n')

    states = str(tmp_path / 'states')
    runner = FakeRunner(environment())
    assert run_main(runner, BOTH, APPS, '--state-dir', states, prepare=stale) == 0
    assert debian_dists(tmp_path) == ['stable', 'trixie']


def test_temporary_state_keeps_failed_release_repository(
    run_main: Callable[..., int], tmp_path: Path
) -> None:
    """Without --state-dir there is no previous state: a failed release keeps its repository."""

    def previous_run(project: Path) -> None:
        forky = project / 'repositories' / 'debian' / 'dists' / 'forky'
        forky.mkdir(parents=True)
        (forky / 'Release').write_text('previous run\n')

    failing = environment(fail=('hello-app', ':forky'))
    two = {'debian': ['trixie', 'forky']}
    assert run_main(FakeRunner(failing), two, APPS, prepare=previous_run) == 1
    forky = tmp_path / 'project' / 'repositories' / 'debian' / 'dists' / 'forky' / 'Release'
    assert forky.read_text() == 'previous run\n'
    readme = (tmp_path / 'project' / 'repositories' / 'debian' / 'index.html').read_text()
    assert 'codename forky' in readme and 'codename trixie' in readme


def rebuilt_spec(
    builders: ModuleType,
    context: BuildContext,
    runner: FakeRunner,
    frozen: date,
    tmp_path: Path,
    text: str,
) -> str:
    """The spec a rebuild of hello-app hands its build, its packaging being text."""
    project = tmp_path / 'project'
    (project / 'hello-app').mkdir(parents=True)
    spec = project / 'hello-app' / 'hello-app.spec'
    spec.write_text(text)
    seen: dict[str, str] = {}
    runner.respond = rpm_build('1.fc44.1', seen=seen)
    info = SpecInfo(frozenset(), frozenset({'hello-app'}), (), '1.0', '1.fc44')
    under = builders.RpmPackageBuilder(
        context,
        release('fedora', '44', frozen),
        project / 'hello-app',
        fixture_signer(),
        'hello-app',
        channel=STABLE,
        spec=info,
    )
    under.stored = stored_rpm(tmp_path, spec)
    under.build(frozenset(), {})
    return seen['spec']


@pytest.mark.parametrize(
    'release_lines',
    [
        'Release: %autorelease\n',
        '%if 0%{?rhel}\nRelease: 3%{?dist}\n%else\nRelease: 1%{?dist}\n%endif\n',
    ],
)
def test_rpm_rebuild_writes_the_expanded_release(
    builders: ModuleType,
    context: BuildContext,
    runner: FakeRunner,
    frozen: date,
    tmp_path: Path,
    release_lines: str,
) -> None:
    """Every Release line becomes the release the base was read from, `.<n>` after it."""
    text = f'Name: hello-app\nVersion: 1.0\n{release_lines}Summary: s\n'
    rebuilt = rebuilt_spec(builders, context, runner, frozen, tmp_path, text)
    releases = re.findall(r'(?m)^Release:\s*(.*)$', rebuilt)
    assert releases and set(releases) == {'1.fc44.1'}


def test_rpm_rebuild_dates_its_entry_as_the_top_one(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date, tmp_path: Path
) -> None:
    """A top entry in the long date form: its date, not an older entry's in the short form."""
    text = (
        'Name: hello-app\nVersion: 1.0\nRelease: 1%{?dist}\n%changelog\n'
        '* Fri Nov 06 12:00:00 UTC 2026 a <a@b.c> - 1.0-1\n- New.\n\n'
        '* Thu Nov 05 2026 a <a@b.c> - 0.9-1\n- Old.\n'
    )
    rebuilt = rebuilt_spec(builders, context, runner, frozen, tmp_path, text)
    entry = f'%changelog\n* Fri Nov 06 12:00:00 UTC 2026 {fixture_signer().uid} - 1.0-1.fc44.1\n'
    assert entry in rebuilt


def test_lower_version_refused(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date, tmp_path: Path
) -> None:
    """A base version below the stored one: users have the higher one already."""
    stored = stored_app(tmp_path, base='1.1~bpo13', key='other')
    under = app_builder(builders, context, release('debian', 'trixie', frozen), stored)
    with pytest.raises(builders.LowerVersion) as raised:
        under.build(frozenset(), {})
    assert str(raised.value) == 'hello-app: version 1.0~bpo13 is below the stored 1.1~bpo13'
    assert runner.calls == []


@pytest.mark.parametrize('option', ['--state-dir', '--download-dir'])
def test_kept_directory_inside_a_repository_refused(
    run_main: Callable[..., int], capsys: pytest.CaptureFixture[str], option: str
) -> None:
    """The repositories are removed before they are written again: never what a run keeps."""
    runner = FakeRunner(environment())
    assert (
        run_main(runner, BOTH, APPS, '--state-dir', 'elsewhere', option, 'repositories/debian/kept')
        == 2
    )
    assert (
        'repositories/debian/kept must not be inside a repository directory'
        in capsys.readouterr().err
    )
    assert runner.calls == []


def test_rpm_lower_release_refused(
    builders: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date, tmp_path: Path
) -> None:
    """rpm's order: 1.0-1.fc44 is below a stored 1.0-2^1.fc44, which Debian's rejects."""
    under = builder(builders, context, release('fedora', '44', frozen))
    stored = stored_rpm(tmp_path, key='other')
    under.stored = dataclasses.replace(stored, base_version='1.0-2^1.fc44')
    with pytest.raises(builders.LowerVersion, match='below the stored 1.0-2\\^1.fc44'):
        under.build(frozenset(), {})


# Snapshot channels (Phase 13).


def weekly(project: Path) -> None:
    use_channels(project)
    add_snapshot_package(project)
    add_upstream_commit(project / 'upstream' / 'hello-lib.git', 'one')


def built_packages(runner: FakeRunner) -> set[tuple[str, str]]:
    """The (package, builder image) of each build container."""
    return {
        (
            c.targets['/src'].source.parent.name.split('.')[0],
            c.image.rpartition('/')[2].split('_builder')[0],
        )
        for c in runner.calls
        if c.is_build
    }


def stable_then(
    run_main: Callable[..., int],
    tmp_path: Path,
    runner: FakeRunner,
    *args: str,
    versions: dict[str, list[str]] = BOTH,
    prepare: Callable[[Path], None] = weekly,
    stable_prepare: Callable[[Path], None] = lambda project: None,
) -> int:
    """Build hello-lib in the release channel, then the weekly channel with runner."""
    states = str(tmp_path / 'states')
    assert (
        run_main(
            FakeRunner(environment()),
            BOTH,
            ['hello-lib'],
            '--state-dir',
            states,
            prepare=stable_prepare,
        )
        == 0
    )
    fresh(tmp_path)
    return run_main(
        runner,
        versions,
        ['hello-lib'],
        '--state-dir',
        states,
        '--channel',
        'weekly',
        *args,
        prepare=prepare,
    )


def test_snapshot_builds_only_declaring_packages(
    run_main: Callable[..., int], tmp_path: Path
) -> None:
    runner = FakeRunner(environment())
    assert stable_then(run_main, tmp_path, runner) == 0
    assert built_packages(runner) == {(SNAP, 'debian'), (SNAP, 'fedora')}


def seen_at_build(
    runner_respond: Callable[[Call], RunResult | None], seen: dict[str, Any]
) -> Callable[[Call], RunResult | None]:
    """runner_respond, recording what each build container's /src holds, by image family."""

    def respond(call: Call) -> RunResult | None:
        if call.is_build or call.is_deps:
            src = call.targets['/src'].source
            family = call.image.rpartition('/')[2].split('_builder')[0]
            seen[f'{family}.{call.args[0]}'] = {
                str(p.relative_to(src)): p.read_bytes() for p in src.rglob('*') if p.is_file()
            }
        return runner_respond(call)

    return respond


def test_snapshot_uses_channel_directory(run_main: Callable[..., int], tmp_path: Path) -> None:
    def prepare(project: Path) -> None:
        weekly(project)
        own = project / f'{SNAP}-weekly'
        shutil.copytree(project / SNAP, own)
        control = own / 'debian' / 'control'
        control.write_text(control.read_text().replace('hello-lib', 'hello-lib, weekly-only'))

    seen: dict[str, Any] = {}
    runner = FakeRunner(seen_at_build(environment(), seen))
    assert stable_then(run_main, tmp_path, runner, prepare=prepare) == 0
    controls = [v for k, v in seen['debian.deps'].items() if k.endswith('debian/control')]
    assert controls and b'weekly-only' in controls[0]


def test_snapshot_deb_version_and_entry(run_main: Callable[..., int], tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    runner = FakeRunner(seen_at_build(environment(), seen))
    assert stable_then(run_main, tmp_path, runner) == 0
    head = git_head(tmp_path / 'project' / 'upstream' / 'hello-lib.git')
    upstream = f'1.0+main.1.g{head[:7]}'
    (changelog_path,) = (k for k in seen['debian.build'] if k.endswith('debian/changelog'))
    assert changelog_path == f'{SNAP}-{upstream}/debian/changelog'
    changelog = Changelog(seen['debian.build'][changelog_path].decode())
    assert str(changelog.version) == f'1:{upstream}-1~bpo13'
    assert f'* Snapshot of main at {head}, not verified' in [
        line.strip() for line in changelog[0].changes()
    ]
    assert (
        changelog.date
        == Changelog((FIXTURE_PROJECT / 'hello-app' / 'debian' / 'changelog').read_text()).date
    )
    assert f'{SNAP}_{upstream}.orig.tar.gz' in seen['debian.build']


def git_head(bare: Path) -> str:
    return subprocess.run(
        ['git', '-C', str(bare), 'rev-parse', 'main'], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_snapshot_rpm_spec_and_tarball(run_main: Callable[..., int], tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    runner = FakeRunner(seen_at_build(environment(), seen))
    download = tmp_path / 'download'
    assert stable_then(run_main, tmp_path, runner, '--download-dir', str(download)) == 0
    head = git_head(tmp_path / 'project' / 'upstream' / 'hello-lib.git')
    files = seen['fedora.build']
    assert files[f'{SNAP}.spec'].decode().startswith(f'%global commit {head}\n')
    tarball = download / 'snapshots' / f'{SNAP}-{head}.tar.gz'
    assert files[f'{SNAP}-1.0+main.1.g{head[:7]}.tar.gz'] == tarball.read_bytes()


def test_dependencies_from_fallback_state(run_main: Callable[..., int], tmp_path: Path) -> None:
    runner = FakeRunner(environment())
    assert stable_then(run_main, tmp_path, runner) == 0
    states = tmp_path / 'states'
    lib = state.ReleaseState(states, STABLE, 'debian-13-trixie').package('hello-lib')
    assert lib is not None
    local = [
        c.targets['/data'].source
        for c in runner.calls
        if c.args[2:4] == ('repo', 'add')
        and not c.targets['/aptlyout'].source.is_relative_to(tmp_path / 'project')
    ]
    assert lib.directory in local


def test_no_fallback_state_fails_the_release(
    run_main: Callable[..., int], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    states = str(tmp_path / 'states')
    debian = {'debian': ['trixie']}
    assert run_main(FakeRunner(environment()), debian, ['hello-lib'], '--state-dir', states) == 0
    fresh(tmp_path)
    capsys.readouterr()
    runner = FakeRunner(environment())
    status = run_main(
        runner, BOTH, ['hello-lib'], '--state-dir', states, '--channel', 'weekly', prepare=weekly
    )
    out = capsys.readouterr()
    assert status == 1
    assert 'no stable state for Fedora 44: build the stable channel first' in (out.out + out.err)
    assert built_packages(runner) == {(SNAP, 'debian')}
    assert not [c for c in runner.calls if 'fedora_builder' in c.image and c.is_deps]


def test_snapshot_failure_leaves_fallback_state(
    run_main: Callable[..., int], tmp_path: Path
) -> None:
    states = tmp_path / 'states'
    assert run_main(FakeRunner(environment()), BOTH, ['hello-lib'], '--state-dir', str(states)) == 0
    before = {uid: kept_state(states, uid) for uid in ('debian-13-trixie', 'fedora-44')}
    fresh(tmp_path)
    failing = environment(fail=(SNAP, 'debian_builder'))
    status = run_main(
        FakeRunner(failing),
        BOTH,
        ['hello-lib'],
        '--state-dir',
        str(states),
        '--channel',
        'weekly',
        prepare=weekly,
    )
    assert status == 1
    assert {uid: kept_state(states, uid) for uid in before} == before


def test_snapshot_state_records_commit(run_main: Callable[..., int], tmp_path: Path) -> None:
    assert stable_then(run_main, tmp_path, FakeRunner(environment())) == 0
    head = git_head(tmp_path / 'project' / 'upstream' / 'hello-lib.git')
    stored = state.ReleaseState(tmp_path / 'states', 'weekly', 'debian-13-trixie').package(SNAP)
    assert stored is not None
    assert stored.snapshot_commit == head
    assert stored.version == f'1:1.0+main.1.g{head[:7]}-1~bpo13'


def same_commit(project: Path) -> None:
    """The weekly channel, upstream unchanged since the last run."""
    use_channels(project)
    add_snapshot_package(project)


def test_unchanged_snapshot_reused(
    run_main: Callable[..., int], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    download = str(tmp_path / 'download')
    assert (
        stable_then(
            run_main,
            tmp_path,
            FakeRunner(environment()),
            '--download-dir',
            download,
            prepare=same_commit,  # upstream's tag commit, the same in every fresh project
        )
        == 0
    )
    fresh(tmp_path)
    capsys.readouterr()
    runner = FakeRunner(environment())
    status = run_main(
        runner,
        BOTH,
        ['hello-lib'],
        '--state-dir',
        str(tmp_path / 'states'),
        '--download-dir',
        download,
        '--channel',
        'weekly',
        prepare=same_commit,
    )
    out = capsys.readouterr().out
    assert status == 0, out
    assert out.count('reused ') == 2 and 'built ' not in out
    assert not [c for c in runner.calls if c.is_build]


def test_channels_named_freely(run_main: Callable[..., int], tmp_path: Path) -> None:
    def rel(project: Path) -> None:
        use_channels(project, 'rel')

    def snapshot(project: Path) -> None:
        use_channels(project, 'rel')
        add_snapshot_package(project)
        add_upstream_commit(project / 'upstream' / 'hello-lib.git', 'one')

    runner = FakeRunner(environment())
    assert stable_then(run_main, tmp_path, runner, prepare=snapshot, stable_prepare=rel) == 0
    assert built_packages(runner) == {(SNAP, 'debian'), (SNAP, 'fedora')}
    assert sorted(p.name for p in (tmp_path / 'states').iterdir()) == ['rel', 'weekly']


# Publishing every channel (Phase 13).
def repo_adds(runner: FakeRunner, repo: str, project: Path) -> list[Path]:
    """The directories added to repo of the project's repositories, not a local one."""
    return [
        c.targets['/data'].source
        for c in runner.calls
        if c.args[2:5] == ('repo', 'add', repo)
        and c.targets['/aptlyout'].source.is_relative_to(project / 'repositories')
    ]


def published_by_project(runner: FakeRunner, project: Path) -> list[tuple[str, ...]]:
    return [
        c.args
        for c in aptly_publishes_calls(runner)
        if c.targets['/aptlyout'].source.is_relative_to(project / 'repositories')
    ]


def aptly_publishes_calls(runner: FakeRunner) -> list[Call]:
    return [c for c in runner.calls if 'publish' in c.args and 'repo' in c.args]


def test_deb_release_publishes_every_component(
    run_main: Callable[..., int], tmp_path: Path
) -> None:
    runner = FakeRunner(environment())
    assert stable_then(run_main, tmp_path, runner) == 0
    published = published_by_project(runner, tmp_path / 'project')
    assert published
    for args in published:
        assert '-component=main,weekly' in args
        assert args[-3:-1] == ('debian-13-trixie.main', 'debian-13-trixie.weekly')
    weekly_state = state.ReleaseState(tmp_path / 'states', 'weekly', 'debian-13-trixie')
    snap = weekly_state.package(SNAP)
    assert snap is not None
    assert repo_adds(runner, 'debian-13-trixie.weekly', tmp_path / 'project') == [snap.directory]
    lib = state.ReleaseState(tmp_path / 'states', STABLE, 'debian-13-trixie').package('hello-lib')
    assert lib is not None
    assert repo_adds(runner, 'debian-13-trixie.main', tmp_path / 'project') == [lib.directory]


def test_component_override(run_main: Callable[..., int], tmp_path: Path) -> None:
    def prepare(project: Path) -> None:
        weekly(project)
        conf_path = project / 'build.conf'
        conf = yaml.safe_load(conf_path.read_text())
        conf['distribs']['debian']['repository']['channels'] = {'weekly': {'component': 'nightly'}}
        conf_path.write_text(yaml.safe_dump(conf))

    runner = FakeRunner(environment())
    assert stable_then(run_main, tmp_path, runner, prepare=prepare) == 0
    published = published_by_project(runner, tmp_path / 'project')
    assert published
    for args in published:
        assert '-component=main,nightly' in args


def test_rpm_channel_repository(run_main: Callable[..., int], tmp_path: Path) -> None:
    runner = FakeRunner(environment())
    assert stable_then(run_main, tmp_path, runner) == 0
    channel = tmp_path / 'project' / 'repositories' / 'weekly.fedora' / '44'
    rpms = sorted(p.name for p in (channel / 'noarch').iterdir())
    assert rpms == ['hello-lib-1.0-1.fc44.noarch.rpm', 'hello-snap-1.0-1.fc44.noarch.rpm']
    repo = (channel / 'weekly.fedora.repo').read_text()
    assert 'baseurl=http://repo.example.invalid/weekly.fedora/44' in repo
    stable = tmp_path / 'project' / 'repositories' / 'fedora' / '44' / 'noarch'
    assert sorted(p.name for p in stable.iterdir()) == ['hello-lib-1.0-1.fc44.noarch.rpm']


def test_missing_snapshot_state(
    run_main: Callable[..., int], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Before any snapshot build: an empty deb component, no rpm channel repository."""
    states = str(tmp_path / 'states')
    assert run_main(FakeRunner(environment()), BOTH, ['hello-lib'], '--state-dir', states) == 0
    fresh(tmp_path)
    capsys.readouterr()
    runner = FakeRunner(environment())
    status = run_main(
        runner, BOTH, ['hello-lib'], '--state-dir', states, '--publish-only', prepare=weekly
    )
    out = capsys.readouterr().out
    assert status == 0, out
    for args in published_by_project(runner, tmp_path / 'project'):
        assert '-component=main,weekly' in args
    assert repo_adds(runner, 'debian-13-trixie.weekly', tmp_path / 'project') == []
    assert 'no weekly state for Fedora 44' in out
    assert not (tmp_path / 'project' / 'repositories' / 'weekly.fedora' / '44').exists()


def test_run_of_one_channel_republishes_all(
    run_main: Callable[..., int], tmp_path: Path, recorded_http: respx.MockRouter
) -> None:
    recorded_http.get(f'{ORIG_BASE}/{SNAP}-1.0.tar.gz').respond(200, content=tarball())
    assert stable_then(run_main, tmp_path, FakeRunner(environment())) == 0
    fresh(tmp_path)
    runner = FakeRunner(environment())
    states = str(tmp_path / 'states')
    assert run_main(runner, BOTH, ['hello-lib'], '--state-dir', states, prepare=same_commit) == 0
    for args in published_by_project(runner, tmp_path / 'project'):
        assert '-component=main,weekly' in args
    snap = state.ReleaseState(tmp_path / 'states', 'weekly', 'debian-13-trixie').package(SNAP)
    assert snap is not None
    assert repo_adds(runner, 'debian-13-trixie.weekly', tmp_path / 'project') == [snap.directory]
    channel = tmp_path / 'project' / 'repositories' / 'weekly.fedora' / '44' / 'noarch'
    assert 'hello-snap-1.0-1.fc44.noarch.rpm' in [p.name for p in channel.iterdir()]


def test_rpm_channel_holds_fallback_packages_it_does_not_build(
    run_main: Callable[..., int], tmp_path: Path
) -> None:
    """hello-lib declares weekly with a deb-only weekly directory: its stable rpm stays there."""

    def prepare(project: Path) -> None:
        weekly(project)
        conf_path = project / 'build.conf'
        conf = yaml.safe_load(conf_path.read_text())
        conf['packages']['hello-lib'] = {
            'channels': {'weekly': conf['packages'][SNAP]['channels']['weekly']}
        }
        conf_path.write_text(yaml.safe_dump(conf))
        shutil.copytree(project / 'hello-lib', project / 'hello-lib-weekly')
        (project / 'hello-lib-weekly' / 'hello-lib.spec').unlink()

    assert stable_then(run_main, tmp_path, FakeRunner(environment()), prepare=prepare) == 0
    channel = tmp_path / 'project' / 'repositories' / 'weekly.fedora' / '44' / 'noarch'
    assert 'hello-lib-1.0-1.fc44.noarch.rpm' in [p.name for p in channel.iterdir()]
