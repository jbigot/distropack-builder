"""Fixtures shared by every test layer of the characterization suite.

The suite runs the code moved into the packages, unchanged in behaviour, and compares it with what
master's scripts gave when it was recorded.
"""

import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import urllib.request
from collections.abc import Callable, Iterator
from datetime import date
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import ModuleType

import pytest
import time_machine
import yaml
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.json import JSONSnapshotExtension

from support import (
    DATA,
    DOCKER_SHIM_DIR,
    FIXTURE_PASSPHRASE,
    FIXTURE_PROJECT,
    FIXTURE_RELEASES,
    FROZEN_DATE,
    MODULES,
    REPO_ROOT,
    SCRIPT_MODULES,
)


@pytest.fixture
def recordings() -> dict[str, str]:
    """The URL -> recorded file mapping; a test may remove entries before loading a script."""
    return json.loads((DATA / 'urls.json').read_text())


@pytest.fixture
def recorded_urlopen(monkeypatch: pytest.MonkeyPatch, recordings: dict[str, str]) -> None:
    """Serve urllib.request.urlopen from the recordings, failing on any other URL."""
    def urlopen(url: str, *args: object, **kwargs: object) -> object:
        if url not in recordings:
            raise KeyError(f'no recording for {url}')
        return (DATA / recordings[url]).open('rb')
    monkeypatch.setattr(urllib.request, 'urlopen', urlopen)


@pytest.fixture
def load_script(recorded_urlopen: None,
                monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], ModuleType]]:
    """Load a fresh copy of the module a script of master became, by the script's name.

    'distinfo.py' and 'pkgbuild' load the modules they were moved to; 'build', still a script at
    the repository root, is loaded by path. The modules are dropped from sys.modules first, so that
    distinfo fetches its data again, from the recordings, at each test. The syspath_prepend makes
    monkeypatch restore sys.path afterwards, which build extends when it is loaded.
    """
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    for name in MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)

    def load(filename: str) -> ModuleType:
        if filename in SCRIPT_MODULES:
            return importlib.import_module(SCRIPT_MODULES[filename])
        name = Path(filename).stem
        loader = SourceFileLoader(name, str(REPO_ROOT / filename))
        spec = spec_from_loader(name, loader)
        assert spec is not None
        module = module_from_spec(spec)
        sys.modules[name] = module
        loader.exec_module(module)
        return module

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
def docker_engine() -> bool:
    """Whether the Docker engine is rootless; skips when no engine answers.

    The builder, as master's pkgbuild, fails on rootless engines (note 164): run_pkgbuild then goes
    through the docker shim, which drops --user. The golden master itself is recorded on a rootful engine.
    """
    try:
        info = subprocess.run(['docker', 'info', '--format', '{{json .SecurityOptions}}'],
                              capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip('no Docker engine answers')
    return 'name=rootless' in info


class QuietHandler(SimpleHTTPRequestHandler):
    """Serves the upstream tarballs without logging every request to stderr."""

    def log_message(self, format: str, *args: object) -> None:
        pass


def write_tarball(source: Path, target: Path) -> None:
    """A reproducible .tar.gz of source, whose single top-level directory is source's name."""
    def reset(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, '', ''
        return info
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w') as tar:
        for path in [source, *sorted(source.rglob('*'))]:
            tar.add(path, arcname=str(path.relative_to(source.parent)), recursive=False,
                    filter=reset)
    with gzip.GzipFile(target, 'wb', mtime=0) as gz:
        gz.write(data.getvalue())


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


def make_project(target: Path, orig_base: str, versions: dict[str, list[str]],
                 packages: list[str]) -> Path:
    """Copy the fixture project to target, limited to some releases and packages.

    Every *.in file of the copy is rendered with @ORIG_BASE@ set to orig_base.
    """
    project_dir = Path(shutil.copytree(FIXTURE_PROJECT, target,
                                       ignore=shutil.ignore_patterns('upstream')))
    for template in project_dir.rglob('*.in'):
        template.with_suffix('').write_text(template.read_text().replace('@ORIG_BASE@', orig_base))
        template.unlink()
    conf_path = project_dir / 'build.conf'
    conf = yaml.safe_load(conf_path.read_text())
    conf['distribs'] = {dist: conf['distribs'][dist] | {'versions': rels}
                        for dist, rels in versions.items()}
    conf['packages'] = {name: conf['packages'][name] for name in packages}
    conf_path.write_text(yaml.safe_dump(conf))
    return project_dir


@pytest.fixture
def project(tmp_path: Path,
            orig_server: str) -> Callable[[dict[str, list[str]], list[str]], Path]:
    """Copies of the fixture project limited to some releases and packages (see make_project)."""
    return lambda versions, packages: make_project(tmp_path / 'project', orig_server, versions,
                                                   packages)


@pytest.fixture(scope='session')
def pkgbuild_env() -> dict[str, str]:
    """The environment of the subprocesses running the builder.

    tests/e2e/hermetic/sitecustomize.py serves them the recorded release data, so that the release
    list is the one of the characterization tests, whatever the date and the network.
    """
    paths = [str(REPO_ROOT / 'tests' / 'e2e' / 'hermetic')]
    if os.environ.get('PYTHONPATH'):
        paths.append(os.environ['PYTHONPATH'])
    return os.environ | {'PYTHONPATH': os.pathsep.join(paths),
                         'DISTROPACK_TEST_RECORDINGS': str(DATA)}


@pytest.fixture
def run_pkgbuild(pkgbuild_env: dict[str, str],
                 docker_engine: bool) -> Callable[[Path], subprocess.CompletedProcess[str]]:
    """Run the builder in a project directory, as pdidev/pkgs runs pkgbuild, output merged."""
    return partial(pkgbuild_run, pkgbuild_env, docker_engine)


def pkgbuild_run(env: dict[str, str], rootless: bool,
                 project_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run the builder in project_dir, through the docker shim on a rootless engine."""
    if rootless:
        env = env | {'PATH': os.pathsep.join([str(DOCKER_SHIM_DIR), env['PATH']])}
    return subprocess.run(
        [sys.executable, '-m', 'distropack_builder', '-j', '3', '-p', FIXTURE_PASSPHRASE,
         'build.conf'],
        cwd=project_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


@pytest.fixture(scope='session')
def fixture_build(tmp_path_factory: pytest.TempPathFactory, orig_server: str,
                  pkgbuild_env: dict[str, str], docker_engine: bool) -> Path:
    """hello-lib and hello-app built once per session for FIXTURE_RELEASES; the project directory.

    Shared by the golden-master and install tests, so that one run builds the fixture once.
    """
    project_dir = make_project(tmp_path_factory.mktemp('fixture-build') / 'project', orig_server,
                               FIXTURE_RELEASES, ['hello-lib', 'hello-app'])
    result = pkgbuild_run(pkgbuild_env, docker_engine, project_dir)
    assert result.returncode == 0, result.stdout
    return project_dir
