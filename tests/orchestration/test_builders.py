"""What the builders ask of the engine: images, mounts, users; no container runs."""

from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import respx

from distro_releases import Release, distribution
from distropack_builder.docker import Cancelled, RunResult
from fake_runner import Call, FakeRunner, StopBuild
from support import FIXTURE_PASSPHRASE, FIXTURE_PROJECT

REGISTRY = 'ghcr.io/jbigot/distropack-builder/'


def release(dist: str, name: str, on: date) -> Release:
    (found,) = (r for r in distribution(dist).releases(on) if name in (r.codename, r.version))
    return found


def respond(call: Call) -> RunResult | None:
    """Stop at the builder's container; dpkg-source says native; other tools succeed silently."""
    if '_builder:' in call.image:
        raise StopBuild()
    if 'dpkg-source' in call.args:
        return RunResult(0, '3.0 (native)\n', '3.0 (native)\n')
    return None


@pytest.fixture
def fake(
    load_script: Callable[[str], ModuleType],
    recorded_http: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ModuleType, FakeRunner]:
    cli = load_script('pkgbuild')
    runner = FakeRunner(respond)
    monkeypatch.setattr(cli, 'RUN', runner)
    return cli, runner


def builder(cli: ModuleType, rel: Release) -> Any:
    """The fixture project's builder for rel, ready to build."""
    gpg = cli.GpgInfo(
        FIXTURE_PROJECT / 'key.gpg', 'ABCDEF0123456789', FIXTURE_PASSPHRASE, 'Test <t@t>'
    )
    family = (
        cli.DebPackageBuilder if cli.targets.is_deb(rel.distribution) else cli.RpmPackageBuilder
    )
    found = family(rel, FIXTURE_PROJECT, gpg, 'hello-app', None, None, 1)
    cli.order_packages([found])  # as the run does: a builder depends on itself until then
    return found


def build(cli: ModuleType, rel: Release, tmp_path: Path) -> None:
    with pytest.raises(StopBuild):
        builder(cli, rel).build(tmp_path)


def builder_call(runner: FakeRunner) -> Call:
    (call,) = (c for c in runner.calls if '_builder:' in c.image)
    return call


def test_deb_builder_image_and_mounts(
    fake: tuple[ModuleType, FakeRunner], frozen: date, tmp_path: Path
) -> None:
    cli, runner = fake
    build(cli, release('debian', 'trixie', frozen), tmp_path)
    call = builder_call(runner)
    assert call.image == REGISTRY + 'debian_builder:trixie'
    assert call.as_root
    assert sorted(call.targets) == ['/deps', '/localrepo', '/src']


def test_rpm_builder_mounts_read_only(
    fake: tuple[ModuleType, FakeRunner], frozen: date, tmp_path: Path
) -> None:
    cli, runner = fake
    build(cli, release('fedora', '44', frozen), tmp_path)
    call = builder_call(runner)
    assert call.image == REGISTRY + 'fedora_builder:44'
    assert call.as_root
    mounts = call.targets
    assert (mounts['/src'].read_only, mounts['/localrepo'].read_only) == (True, True)
    assert mounts['/output'].read_only is False


@pytest.mark.parametrize(('dist', 'name'), [('debian', 'trixie'), ('fedora', '44')])
def test_no_secret_key_in_a_builder(
    fake: tuple[ModuleType, FakeRunner], frozen: date, tmp_path: Path, dist: str, name: str
) -> None:
    cli, runner = fake
    build(cli, release(dist, name, frozen), tmp_path)
    call = builder_call(runner)
    assert '/gnupghome' not in call.targets
    assert not [m for m in call.mounts if m.source.name.startswith('GNUPG_HOME.')]


@pytest.mark.parametrize(('dist', 'name'), [('debian', 'trixie'), ('fedora', '44')])
def test_tools_run_as_the_runner_chooses(
    fake: tuple[ModuleType, FakeRunner], frozen: date, tmp_path: Path, dist: str, name: str
) -> None:
    cli, runner = fake
    build(cli, release(dist, name, frozen), tmp_path)
    tools = [c for c in runner.calls if '_builder:' not in c.image]
    assert tools and not [c for c in tools if c.as_root]


@pytest.mark.parametrize(('dist', 'name'), [('debian', 'trixie'), ('fedora', '44')])
def test_cancelled_builder_reports_cancel(
    fake: tuple[ModuleType, FakeRunner],
    frozen: date,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    dist: str,
    name: str,
) -> None:
    """A builder whose container is cancelled says so, rather than reporting a failure."""
    cli, runner = fake

    def cancelled(call: Call) -> RunResult | None:
        if '_builder:' in call.image:
            raise Cancelled()
        return respond(call)

    runner.respond = cancelled
    with pytest.raises(cli.CancellationException):
        builder(cli, release(dist, name, frozen)).build(tmp_path)
    out = capsys.readouterr().out
    assert '[CANCEL]' in out
    assert '[FAILURE]' not in out
