"""What the builders ask of the engine: images, mounts, users; no container runs."""

import sys
import tempfile
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import httpx
import pytest
import respx

from distro_releases import Release, distribution
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
    assert call.image == REGISTRY + 'debian_builder:trixie'
    assert call.as_root
    assert sorted(call.targets) == ['/deps', '/localrepo', '/src']


def test_rpm_builder_mounts_read_only(
    cli: ModuleType, context: BuildContext, runner: FakeRunner, frozen: date
) -> None:
    build(cli, context, release('fedora', '44', frozen))
    call = builder_call(runner)
    assert call.image == REGISTRY + 'fedora_builder:44'
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
        runner: FakeRunner, versions: dict[str, list[str]], packages: list[str], *args: str
    ) -> int:
        project = make_project(tmp_path / 'project', ORIG_BASE, versions, packages)
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
