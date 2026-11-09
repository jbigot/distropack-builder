# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""What the builders ask of the engine: images, mounts, users; no container runs."""

import hashlib
import importlib
import shutil
import signal
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
from distropack_builder.bake import PROTOCOL
from distropack_builder.context import BuildContext
from distropack_builder.docker import Cancellation, Cancelled, ContainerFailed, RunResult
from distropack_builder.rpmquery import SpecInfo
from distropack_builder.scheduler import Interrupted, schedule
from distropack_builder.signing import Signer
from fake_runner import Call, FakeRunner, StopBuild, tools_respond
from support import FIXTURE_PASSPHRASE, FIXTURE_PROJECT, make_project, write_tarball

REGISTRY = 'ghcr.io/jbigot/distropack-builder/'
ORIG_BASE = 'https://orig.test'
HELLO_LIB_ORIG = f'{ORIG_BASE}/hello-lib-1.0.tar.gz'


def release(dist: str, name: str, on: date) -> Release:
    (found,) = (r for r in distribution(dist).releases(on) if name in (r.codename, r.version))
    return found


def respond(call: Call) -> RunResult | None:
    """Stop at the builder's container; other tools succeed silently."""
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
        return builders.DebPackageBuilder(context, rel, FIXTURE_PROJECT, signer, 'hello-app')
    spec = SpecInfo(frozenset({'hello-lib'}), frozenset({'hello-app'}), ())
    return builders.RpmPackageBuilder(context, rel, FIXTURE_PROJECT, signer, 'hello-app', spec=spec)


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
    assert sorted(call.targets) == ['/localrepo', '/src']
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
        return None

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
