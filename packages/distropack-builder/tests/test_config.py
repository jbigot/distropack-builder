"""build.conf, read through the configuration models."""

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.json import JSONSnapshotExtension

from distropack_builder import config
from distropack_builder.config import BuildConfig, ConfigError, Package

DATA = Path(__file__).parent / 'data'
FIXTURE = Path(__file__).parents[3] / 'tests' / 'e2e' / 'project'


def package_dir(root: Path, name: str, *, deb: bool = True, spec: bool = True) -> None:
    """A package directory under root, with deb and rpm packaging as asked."""
    (root / name).mkdir()
    if deb:
        (root / name / 'debian').mkdir()
    if spec:
        (root / name / f'{name}.spec').touch()


def test_load_pdidev(tmp_path: Path) -> None:
    conf = tmp_path / 'build.conf'
    shutil.copy(DATA / 'pdidev-build.conf', conf)
    for name in ('paraconf', 'pdi', 'pdidev-archive-keyring', 'pdiplugin-decl-hdf5'):
        package_dir(tmp_path, name)
    for name in (
        'pdiplugin-decl-netcdf',
        'pdiplugin-mpi',
        'pdiplugin-pycall',
        'pdiplugin-serialize',
        'pdiplugin-set-value',
        'pdiplugin-timer',
        'pdiplugin-trace',
        'pdiplugin-user-code',
    ):
        package_dir(tmp_path, name)

    loaded = config.load(conf)

    assert loaded.distribs['debian'].versions == ('bookworm', 'supported', 'testing', 'sid')
    assert loaded.distribs['ubuntu'].versions == ('supported',)
    assert loaded.distribs['fedora'].repository.name == 'pdidev'
    assert loaded.distribs['fedora'].repository.path == Path('fedora')
    assert loaded.distribs['fedora'].gpg.file == Path('key.gpg')
    assert loaded.packages['pdidev-archive-keyring'].disable == {'fedora': ('all',)}
    assert loaded.packages['pdi'].orig is not None
    assert loaded.root == tmp_path


@pytest.fixture
def fixture_project(tmp_path: Path) -> Iterator[Path]:
    """A copy of the fixture project, with its build.conf written from build.conf.in."""
    project = tmp_path / 'project'
    shutil.copytree(FIXTURE, project)
    conf = project / 'build.conf'
    conf.write_text(
        (project / 'build.conf.in').read_text().replace('@ORIG_BASE@', 'http://localhost')
    )
    yield conf


def test_load_fixture(fixture_project: Path) -> None:
    loaded = config.load(fixture_project)

    assert loaded.distribs['fedora'].versions == ('44',)
    assert loaded.packages['hello-app'] == Package()
    assert (
        loaded.packages['hello-lib'].orig == 'http://localhost/hello-lib-{upstream_version}.tar.gz'
    )


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        pytest.param('', 'expected a mapping', id='empty'),
        pytest.param('[]', 'expected a mapping', id='list'),
        pytest.param(
            'distribs: {debian: {versionz: []}}\npackages: {}\n',
            'distribs.debian.versionz',
            id='misspelt',
        ),
        pytest.param(
            'distribs: {rhel: {}}\npackages: {}\n', 'rhel is not a build target', id='not-a-target'
        ),
        pytest.param(
            'distribs: {}\npackages: {p: {disable: {debian: [available]}}}\n',
            'packages.p.disable.debian',
            id='available-in-disable',
        ),
        pytest.param('distribs: {}\npackages:\n', 'packages', id='null-packages'),
        pytest.param(
            'distribs: {debian: {repository: 3}}\npackages: {}\n',
            'distribs.debian.repository',
            id='number-for-mapping',
        ),
        pytest.param(
            'distribs: {ubuntu: {versions: [24.04]}}\npackages: {}\n',
            'distribs.ubuntu.versions',
            id='float-version',
        ),
        pytest.param('distribs: {\npackages: [}\n', 'line 2', id='yaml-syntax'),
        pytest.param(
            'distribs: {}\npackages: {spec-only: {orig: "http://x/{upstream_version}"}}\n',
            'packages.spec-only.orig',
            id='orig-without-debian',
        ),
        pytest.param(
            'distribs: {}\npackages: {nothing: {}}\n', 'packages.nothing', id='no-packaging'
        ),
        pytest.param('distribs: {}\npackages: {missing: {}}\n', 'packages.missing', id='no-dir'),
    ],
)
def test_load_rejects(tmp_path: Path, text: str, expected: str) -> None:
    package_dir(tmp_path, 'spec-only', deb=False)
    package_dir(tmp_path, 'nothing', deb=False, spec=False)
    conf = tmp_path / 'build.conf'
    conf.write_text(text)

    with pytest.raises(ConfigError) as caught:
        config.load(conf)

    message = str(caught.value)
    assert message.startswith(f'{conf}: ')
    assert expected in message
    assert '\n' not in message


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match='build.conf'):
        config.load(tmp_path / 'build.conf')


def test_hidden_keys_ignored(tmp_path: Path) -> None:
    conf = tmp_path / 'build.conf'
    conf.write_text('.anchors: &a {versions: [trixie]}\ndistribs: {debian: *a}\npackages: {}\n')

    assert config.load(conf).distribs['debian'].versions == ('trixie',)


def test_paths_relative_to_config(
    tmp_path: Path, fixture_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    loaded = config.load(fixture_project.relative_to(elsewhere, walk_up=True))

    assert loaded.root == fixture_project.parent.resolve()


def test_models_are_frozen() -> None:
    built = BuildConfig(distribs={}, packages={}, root=Path('/'))
    with pytest.raises(ValueError, match='frozen'):
        built.root = Path('/tmp')


def test_schema_snapshot(snapshot: SnapshotAssertion) -> None:
    schema = config.schema()

    assert 'root' not in schema['properties']

    def properties(node: Any) -> Iterator[tuple[str, Any]]:
        if isinstance(node, dict):
            for name, value in node.get('properties', {}).items():
                yield name, value
            for value in node.values():
                yield from properties(value)

    for name, value in properties(schema):
        assert value.get('description'), name
    assert schema == snapshot.use_extension(JSONSnapshotExtension)
