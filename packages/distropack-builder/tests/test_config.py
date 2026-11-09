# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""build.conf, read through the configuration models."""

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pysequoia
import pytest
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.json import JSONSnapshotExtension

from distro_releases import Release, distribution
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
    assert loaded.packages['hello-lib'] == Package()


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
            'distribs: {debian: {versions: [available]}}\npackages: {}\n',
            'distribs.debian.versions',
            id='available-alone',
        ),
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
            'distribs: {}\npackages: {quilt: {orig: "http://x/{upstream_version}"}}\n',
            'packages.quilt.orig: Extra inputs are not permitted',
            id='orig-is-unknown',
        ),
        pytest.param(
            'distribs: {}\npackages: {quilt-unwatched: {}}\n',
            'packages.quilt-unwatched: a 3.0 (quilt) source needs a debian/watch',
            id='quilt-without-watch',
        ),
        pytest.param(
            'distribs: {}\npackages: {nothing: {}}\n', 'packages.nothing', id='no-packaging'
        ),
        pytest.param(
            'distribs: {}\npackages: {spec-only: {}}\n',
            'packages.spec-only: ',
            id='malformed-sources',
        ),
        pytest.param('distribs: {}\npackages: {missing: {}}\n', 'packages.missing', id='no-dir'),
    ],
)
def test_load_rejects(tmp_path: Path, text: str, expected: str) -> None:
    package_dir(tmp_path, 'spec-only', deb=False)
    (tmp_path / 'spec-only' / 'sources').write_text('MD5 (x) = 00\n')
    package_dir(tmp_path, 'nothing', deb=False, spec=False)
    for name, watched in (('quilt', True), ('quilt-unwatched', False)):
        package_dir(tmp_path, name, spec=False)
        (tmp_path / name / 'debian' / 'source').mkdir()
        (tmp_path / name / 'debian' / 'source' / 'format').write_text('3.0 (quilt)\n')
        if watched:
            (tmp_path / name / 'debian' / 'watch').write_text('version=4\n')
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


def write_extra(tmp_path: Path, dist: str, extra: str, *, key: bytes | None = None) -> Path:
    """A build.conf whose dist has extra_repositories: extra (YAML flow), key.asc beside it."""
    if key is not None:
        (tmp_path / 'key.asc').write_bytes(key)
    conf = tmp_path / 'build.conf'
    conf.write_text(f'distribs: {{{dist}: {{extra_repositories: [{extra}]}}}}\npackages: {{}}\n')
    return conf


def public_key() -> bytes:
    return str(
        pysequoia.Tsk.generate('Other <other@example.invalid>').extract_certificate()
    ).encode()


def test_extra_repository(tmp_path: Path) -> None:
    conf = write_extra(
        tmp_path, 'debian', '{url: "https://repo.pdi.dev/debian/", key: key.asc}', key=public_key()
    )
    (extra,) = config.load(conf).distribs['debian'].extra_repositories
    assert (extra.url, extra.key, extra.suite, extra.components) == (
        'https://repo.pdi.dev/debian/',
        Path('key.asc'),
        '{codename}',
        ('main',),
    )


@pytest.mark.parametrize(
    ('dist', 'extra', 'key', 'expected'),
    [
        pytest.param('debian', '{url: "https://x/"}', None, 'key', id='key-missing'),
        pytest.param(
            'debian', '{url: "https://x/", key: key.asc}', None, 'key.asc', id='key-file-missing'
        ),
        pytest.param(
            'debian',
            '{url: "https://x/", key: key.asc}',
            b'not a key\n',
            'not an OpenPGP public key',
            id='not-a-key',
        ),
        pytest.param(
            'debian',
            '{url: "https://x/{nosuch}", key: key.asc}',
            b'',
            'unknown field {nosuch}',
            id='unknown-field',
        ),
        pytest.param(
            'fedora',
            '{url: "https://x/{version}", key: key.asc, suite: stable}',
            b'',
            'suite and components are for the Debian family',
            id='suite-on-rpm',
        ),
    ],
)
def test_extra_repository_rejects(
    tmp_path: Path, dist: str, extra: str, key: bytes | None, expected: str
) -> None:
    conf = write_extra(tmp_path, dist, extra, key=public_key() if key == b'' else key)
    with pytest.raises(ConfigError) as caught:
        config.load(conf)
    assert f'distribs.{dist}.extra_repositories' in str(caught.value)
    assert expected in str(caught.value)


def test_expand_per_release() -> None:
    trixie = Release(
        distribution('debian'), '13', 'trixie', '13 (Trixie)', 13, 'stable', None, None, None, None
    )
    assert (
        config.expand('https://x/{codename}/{version}/{suite}', trixie)
        == 'https://x/trixie/13/stable'
    )
