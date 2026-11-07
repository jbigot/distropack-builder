"""Fixtures shared by every test layer of the characterization suite.

The suite runs the code moved into the packages, unchanged in behaviour, and compares it with what
master's scripts gave when it was recorded.
"""

import os

# typer decides once, when it is imported, to colour its output whenever one of these is set, as
# GITHUB_ACTIONS is in CI; the tests read that output as plain text, as a run without a terminal
# prints it.
os.environ.pop('FORCE_COLOR', None)
os.environ.pop('GITHUB_ACTIONS', None)
os.environ.pop('PY_COLORS', None)

import importlib
import json
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from datetime import date
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import respx
import stamina
import time_machine
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.json import JSONSnapshotExtension

from distro_releases import catalog
from support import (
    FIXTURE_PASSPHRASE,
    FIXTURE_PROJECT,
    FIXTURE_RELEASES,
    FROZEN_DATE,
    MODULES,
    RELEASE_DATA,
    REPO_ROOT,
    SCRIPT_MODULES,
    make_project,
    write_tarball,
)


@pytest.fixture
def recordings() -> dict[str, str]:
    """The URL -> recorded file mapping; a test may remove entries before loading a script."""
    urls: dict[str, str] = json.loads((RELEASE_DATA / 'urls.json').read_text())
    return urls


@pytest.fixture
def recorded_http(recordings: dict[str, str]) -> Iterator[respx.MockRouter]:
    """Serve httpx requests from the recordings, failing on any other URL, with a fresh catalog.

    Retries happen without waiting, as in distro-releases' own tests.
    """
    catalog._fetch.cache_clear()
    with (
        stamina.set_testing(True, attempts=5),
        respx.mock(assert_all_called=False, assert_all_mocked=True) as router,
    ):
        for url, name in recordings.items():
            router.get(url, name=url).mock(
                return_value=httpx.Response(200, content=(RELEASE_DATA / name).read_bytes())
            )
        yield router
    catalog._fetch.cache_clear()


@pytest.fixture
def load_script(
    recorded_http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[str], ModuleType]]:
    """Load a fresh copy of the module a script of master became, by the script's name.

    'pkgbuild' and 'build' load the modules they became (SCRIPT_MODULES), dropped from sys.modules
    first; the release data come from the recordings, fetched again at each test.
    """
    for name in MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
        # and the package's attribute, which `from distropack_builder import bake` reads first
        package, _, module = name.rpartition('.')
        if package in sys.modules:
            monkeypatch.delattr(sys.modules[package], module, raising=False)

    def load(filename: str) -> ModuleType:
        return importlib.import_module(SCRIPT_MODULES[filename])

    yield load
    for name in MODULES:
        sys.modules.pop(name, None)


@pytest.fixture
def frozen(request: pytest.FixtureRequest) -> Iterator[date]:
    """Freeze the clock on request.param (indirect parametrization), by default 2026-11-05."""
    day: date = getattr(request, 'param', FROZEN_DATE)
    with time_machine.travel(day, tick=False):
        yield day


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Snapshots as JSON files, readable in a diff."""
    return snapshot.use_extension(JSONSnapshotExtension)


@pytest.fixture(scope='session')
def docker_engine() -> None:
    """Skips when no container engine answers the builder (Docker or Podman, rootful or rootless).

    Asked in a subprocess, as the builder runs in the tests that need it.
    """
    result = subprocess.run(
        [sys.executable, '-c', 'from distropack_builder.docker import Engine; Engine.connect()'],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f'no container engine answers: {result.stderr.strip().splitlines()[-1:]}')


class QuietHandler(SimpleHTTPRequestHandler):
    """Serves the upstream tarballs without logging every request to stderr."""

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope='session')
def orig_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Serve the fixture's upstream tarballs on 127.0.0.1 for the session; yields the base URL."""
    served = tmp_path_factory.mktemp('served')
    for upstream in (FIXTURE_PROJECT / 'upstream').iterdir():
        write_tarball(upstream, served / f'{upstream.name}.tar.gz')
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=served))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()
    thread.join()
    server.server_close()


@pytest.fixture
def project(tmp_path: Path, orig_server: str) -> Callable[[dict[str, list[str]], list[str]], Path]:
    """Copies of the fixture project limited to some releases and packages (see make_project)."""
    return lambda versions, packages: make_project(
        tmp_path / 'project', orig_server, versions, packages
    )


@pytest.fixture(scope='session')
def pkgbuild_env() -> dict[str, str]:
    """The environment of the subprocesses running the builder.

    tests/e2e/hermetic/sitecustomize.py serves them the recorded release data, so that the release
    list is the one of the characterization tests, whatever the date and the network.
    """
    paths = [str(REPO_ROOT / 'tests' / 'e2e' / 'hermetic')]
    if os.environ.get('PYTHONPATH'):
        paths.append(os.environ['PYTHONPATH'])
    return os.environ | {
        'PYTHONPATH': os.pathsep.join(paths),
        'DISTROPACK_TEST_RECORDINGS': str(RELEASE_DATA),
    }


@pytest.fixture
def run_pkgbuild(
    pkgbuild_env: dict[str, str], docker_engine: None
) -> Callable[[Path], subprocess.CompletedProcess[str]]:
    """Run the builder in a project directory, as pdidev/pkgs runs pkgbuild, output merged."""
    return partial(pkgbuild_run, pkgbuild_env)


def pkgbuild_run(env: dict[str, str], project_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run the builder in project_dir, on whatever engine answers it."""
    return subprocess.run(
        [
            sys.executable,
            '-m',
            'distropack_builder',
            'run',
            '-j',
            '3',
            'build.conf',
        ],
        cwd=project_dir,
        env={**env, 'DISTROPACK_GPG_PASSPHRASE': FIXTURE_PASSPHRASE},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


@pytest.fixture(scope='session')
def fixture_build(
    tmp_path_factory: pytest.TempPathFactory,
    orig_server: str,
    pkgbuild_env: dict[str, str],
    docker_engine: None,
) -> Path:
    """hello-lib and hello-app built once per session for FIXTURE_RELEASES; the project directory.

    Shared by the golden-master and install tests, so that one run builds the fixture once.
    """
    project_dir = make_project(
        tmp_path_factory.mktemp('fixture-build') / 'project',
        orig_server,
        FIXTURE_RELEASES,
        ['hello-lib', 'hello-app'],
    )
    result = pkgbuild_run(pkgbuild_env, project_dir)
    assert result.returncode == 0, result.stdout
    return project_dir
