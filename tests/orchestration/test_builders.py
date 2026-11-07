"""What the builders ask of the engine: images, mounts, users; no container runs."""

import signal
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import httpx
import pytest
import respx

from distro_releases import Release, distribution
from distropack_builder.bake import PROTOCOL
from distropack_builder.context import BuildContext
from distropack_builder.docker import Cancellation, Cancelled, ContainerFailed, RunResult
from distropack_builder.scheduler import Interrupted, schedule
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
    if '_builder:' in call.image:
        raise StopBuild()
    return None


@pytest.fixture
def cli(load_script: Callable[[str], ModuleType]) -> ModuleType:
    return load_script('pkgbuild')


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner(respond)


@pytest.fixture
def context(runner: FakeRunner, tmp_path: Path) -> BuildContext:
    workdir = tmp_path / 'work'
    workdir.mkdir()
    return BuildContext(cast(Any, None), runner, Cancellation(), workdir, 1)


def builder(cli: ModuleType, context: BuildContext, rel: Release) -> Any:
    """The fixture project's builder of hello-app for rel."""
    gpg = cli.GpgInfo(
        context, FIXTURE_PROJECT / 'key.gpg', 'ABCDEF0123456789', FIXTURE_PASSPHRASE, 'Test <t@t>'
    )
    family = (
        cli.DebPackageBuilder if cli.targets.is_deb(rel.distribution) else cli.RpmPackageBuilder
    )
    return family(context, rel, FIXTURE_PROJECT, gpg, 'hello-app')


def build(cli: ModuleType, context: BuildContext, rel: Release) -> None:
    with pytest.raises(StopBuild):
        builder(cli, context, rel).build(frozenset(), {})


def builder_call(runner: FakeRunner) -> Call:
    (call,) = (c for c in runner.calls if '_builder:' in c.image)
    return call


def test_deb_builder_image_and_mounts(
    cli: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date
) -> None:
    build(cli, context, release('debian', 'trixie', frozen))
    call = builder_call(runner)
    assert call.image == REGISTRY + f'debian_builder:trixie-p{PROTOCOL}'
    assert call.as_root
    assert sorted(call.targets) == ['/deps', '/localrepo', '/src']


def test_rpm_builder_mounts_read_only(
    cli: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date
) -> None:
    build(cli, context, release('fedora', '44', frozen))
    call = builder_call(runner)
    assert call.image == REGISTRY + f'fedora_builder:44-p{PROTOCOL}'
    assert call.as_root
    mounts = call.targets
    assert (mounts['/src'].read_only, mounts['/localrepo'].read_only) == (True, True)
    assert mounts['/output'].read_only is False


@pytest.mark.parametrize(('dist', 'name'), [('debian', 'trixie'), ('fedora', '44')])
def test_no_secret_key_in_a_builder(
    cli: ModuleType,
    context: BuildContext,
    runner: FakeRunner,
    frozen: date,
    dist: str,
    name: str,
) -> None:
    build(cli, context, release(dist, name, frozen))
    call = builder_call(runner)
    assert '/gnupghome' not in call.targets
    assert not [m for m in call.mounts if m.source.name.startswith('GNUPG_HOME.')]


@pytest.mark.parametrize(('dist', 'name'), [('debian', 'trixie'), ('fedora', '44')])
def test_tools_run_as_the_runner_chooses(
    cli: ModuleType,
    context: BuildContext,
    runner: FakeRunner,
    frozen: date,
    dist: str,
    name: str,
) -> None:
    build(cli, context, release(dist, name, frozen))
    tools = [c for c in runner.calls if '_builder:' not in c.image]
    assert tools and not [c for c in tools if c.as_root]


@pytest.mark.parametrize(('dist', 'name'), [('debian', 'trixie'), ('fedora', '44')])
def test_cancelled_builder_is_interrupted(
    cli: ModuleType,
    context: BuildContext,
    runner: FakeRunner,
    frozen: date,
    dist: str,
    name: str,
) -> None:
    """A builder whose container is cancelled is interrupted, not failed."""

    def cancelled(call: Call) -> RunResult | None:
        if '_builder:' in call.image:
            raise Cancelled()
        return None

    runner.respond = cancelled
    found = builder(cli, context, release(dist, name, frozen))
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
    ) -> int:
        project = make_project(tmp_path / 'project', ORIG_BASE, versions, packages)
        prepare(project)
        monkeypatch.chdir(project)
        monkeypatch.setattr(
            sys, 'argv', ['distropack-build', 'run', '-j', '2', '-p', 'x', *args, 'build.conf']
        )
        try:
            cli.main(runner=runner, connect=lambda host: cast(Any, None))
        except SystemExit as exit:
            return int(exit.code or 0)
        return 0

    yield call


def test_local_repository_is_the_closure(run_main: Callable[..., int]) -> None:
    runner = FakeRunner(tools_respond)
    assert run_main(runner, {'debian': ['trixie']}, ['hello-lib', 'hello-app']) == 0
    included = [
        c
        for c in runner.calls
        if 'include' in c.args
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
    recorded_http.routes['orig'].return_value = httpx.Response(404)
    runner = FakeRunner(tools_respond)
    status = run_main(
        runner, {'debian': ['trixie']}, ['hello-lib', 'hello-app', 'hello-undeclared']
    )
    assert status == 1
    built = {
        p
        for p in ('hello-lib', 'hello-app', 'hello-undeclared')
        for c in runner.calls
        if c.build_of(p)
    }
    assert built == {'hello-undeclared'}
    out = capsys.readouterr().out
    assert (
        f'failed hello-lib on Debian GNU/Linux 13 (Trixie): while downloading {HELLO_LIB_ORIG}'
        in out
    )
    assert 'skipped hello-app on Debian GNU/Linux 13 (Trixie): hello-lib failed' in out


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
        if '_builder:' in call.image:
            raise ContainerFailed(2, [call.image])
        return tools_respond(call)

    runner = FakeRunner(tools_respond if outcome == 'success' else fail)
    status = run_main(runner, {'debian': ['trixie']}, ['hello-lib', 'hello-app'])
    assert status == (0 if outcome == 'success' else 1)
    assert list(temp.iterdir()) == []


def rpm_builds(fail_on: tuple[str, str] | None = None) -> Callable[[Call], RunResult | None]:
    """Builders that write an rpm, but for fail_on, a (package, image tag) pair."""

    def respond(call: Call) -> RunResult | None:
        if '_builder:' in call.image:
            tag = call.image.rpartition(':')[2].removesuffix(f'-p{PROTOCOL}')
            output = call.targets['/output'].source
            package = output.parent.name.split('.')[0]
            if (package, tag) == fail_on:
                raise ContainerFailed(1, [call.image], output='the build broke')
            (output / f'{package}-1.0-1.fc{tag}.noarch.rpm').touch()
            return None
        return tools_respond(call)

    return respond


def createrepo_dirs(runner: FakeRunner) -> list[str]:
    return [c.targets['/data'].source.name for c in runner.calls if 'createrepo_c' in c.args]


def test_failing_release_publishes_nothing(run_main: Callable[..., int]) -> None:
    """hello-undeclared builds on 44, but hello-needs-root does not: 44 is not published."""
    runner = FakeRunner(rpm_builds(fail_on=('hello-needs-root', '44')))
    packages = ['hello-undeclared', 'hello-needs-root']
    assert run_main(runner, {'fedora': ['43', '44']}, packages) == 1
    assert createrepo_dirs(runner) == ['43']


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
    run_main: Callable[..., int], capsys: pytest.CaptureFixture[str]
) -> None:
    builds = rpm_builds()

    def respond(call: Call) -> RunResult | None:
        if 'createrepo_c' in call.args and call.targets['/data'].source.name == '43':
            raise ContainerFailed(1, [call.image, *call.args], output='createrepo broke')
        return builds(call)

    runner = FakeRunner(respond)
    assert run_main(runner, {'fedora': ['43', '44']}, ['hello-undeclared']) == 1
    assert sorted(createrepo_dirs(runner)) == ['43', '44']
    out = capsys.readouterr().out
    assert 'publication failed for Fedora 43:' in out
    assert 'createrepo broke' in out


def test_sigterm_cancels(
    run_main: Callable[..., int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp = tmp_path / 'temp'
    temp.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(temp))
    previous = signal.getsignal(signal.SIGTERM)
    cancellations: list[Cancellation] = []

    def respond(call: Call) -> RunResult | None:
        if '_builder:' in call.image:
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
    with pytest.raises(KeyboardInterrupt):
        run_main(runner, {'debian': ['trixie']}, ['hello-undeclared'])
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
