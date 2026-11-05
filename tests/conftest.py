"""Fixtures shared by every test layer of the characterization suite."""

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
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import ModuleType

import pytest
import time_machine
import yaml
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.json import JSONSnapshotExtension

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / 'tests' / 'characterization' / 'data'
SCRIPTS = ('distinfo', 'downloader', 'pkgbuild', 'build')
FROZEN_DATE = date(2026, 11, 5)
FIXTURE_PROJECT = REPO_ROOT / 'tests' / 'e2e' / 'project'
FIXTURE_PASSPHRASE = 'test-passphrase'


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
    """Load one of the scripts at the repository root as a fresh module.

    The scripts and the modules they import are dropped from sys.modules first, so that distinfo
    fetches its data again, from the recordings, at each test.
    """
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    for name in SCRIPTS:
        monkeypatch.delitem(sys.modules, name, raising=False)

    def load(filename: str) -> ModuleType:
        name = Path(filename).stem
        loader = SourceFileLoader(name, str(REPO_ROOT / filename))
        spec = spec_from_loader(name, loader)
        assert spec is not None
        module = module_from_spec(spec)
        sys.modules[name] = module
        loader.exec_module(module)
        return module

    yield load
    for name in SCRIPTS:
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
def rootful_docker() -> None:
    """Skip unless a rootful Docker engine answers: master's pkgbuild fails on rootless ones."""
    try:
        info = subprocess.run(['docker', 'info', '--format', '{{json .SecurityOptions}}'],
                              capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip('no Docker engine answers')
    if 'name=rootless' in info:
        pytest.skip("master's pkgbuild needs a rootful engine (note 164)")


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


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Callable[[dict[str, list[str]], list[str]], Path]]:
    """Make a copy of the fixture project limited to some releases and packages.

    The upstream tarballs are served on 127.0.0.1 for the lifetime of the test, and every *.in file
    of the copy is rendered with @ORIG_BASE@ set to that server's URL.
    """
    served = tmp_path / 'served'
    served.mkdir()
    for upstream in (FIXTURE_PROJECT / 'upstream').iterdir():
        write_tarball(upstream, served / f'{upstream.name}.tar.gz')
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=served))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def make(versions: dict[str, list[str]], packages: list[str]) -> Path:
        project_dir = Path(shutil.copytree(FIXTURE_PROJECT, tmp_path / 'project',
                                           ignore=shutil.ignore_patterns('upstream')))
        for template in project_dir.rglob('*.in'):
            template.with_suffix('').write_text(template.read_text().replace(
                '@ORIG_BASE@', f'http://127.0.0.1:{server.server_port}'))
            template.unlink()
        conf_path = project_dir / 'build.conf'
        conf = yaml.safe_load(conf_path.read_text())
        conf['distribs'] = {dist: conf['distribs'][dist] | {'versions': rels}
                            for dist, rels in versions.items()}
        conf['packages'] = {name: conf['packages'][name] for name in packages}
        conf_path.write_text(yaml.safe_dump(conf))
        return project_dir

    yield make
    server.shutdown()
    thread.join()
    server.server_close()


@pytest.fixture(scope='session')
def pkgbuild_env() -> dict[str, str]:
    """The environment of the subprocesses running master's scripts.

    tests/e2e/hermetic/sitecustomize.py serves them the recorded release data, so that the release
    list is the one of the characterization tests, whatever the date and the network.
    """
    paths = [str(REPO_ROOT / 'tests' / 'e2e' / 'hermetic'), str(REPO_ROOT)]
    if os.environ.get('PYTHONPATH'):
        paths.append(os.environ['PYTHONPATH'])
    return os.environ | {'PYTHONPATH': os.pathsep.join(paths),
                         'DISTROPACK_TEST_RECORDINGS': str(DATA)}


@pytest.fixture
def run_pkgbuild(
        pkgbuild_env: dict[str, str]) -> Callable[[Path], subprocess.CompletedProcess[str]]:
    """Run master's pkgbuild in a project directory, as pdidev/pkgs does, output merged."""
    def run(project_dir: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / 'pkgbuild'), '-j', '3', '-p', FIXTURE_PASSPHRASE,
             'build.conf'],
            cwd=project_dir, env=pkgbuild_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
    return run
