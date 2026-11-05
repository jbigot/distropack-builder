"""Fixtures shared by every test layer of the characterization suite."""

import json
import sys
import urllib.request
from collections.abc import Callable, Iterator
from datetime import date
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import ModuleType

import pytest
import time_machine
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.json import JSONSnapshotExtension

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / 'tests' / 'characterization' / 'data'
SCRIPTS = ('distinfo', 'downloader', 'pkgbuild', 'build')
FROZEN_DATE = date(2026, 11, 5)


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
