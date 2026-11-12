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


def test_extra_repository_key_must_not_be_secret(tmp_path: Path) -> None:
    secret = str(pysequoia.Tsk.generate('Secret <secret@example.invalid>')).encode()
    conf = write_extra(tmp_path, 'debian', '{url: "https://x/", key: key.asc}', key=secret)
    with pytest.raises(ConfigError, match='holds a secret key'):
        config.load(conf)


def test_extra_repository_name_is_a_file_name(tmp_path: Path) -> None:
    extra = '{url: "https://x/", key: key.asc, name: "my repo"}'
    conf = write_extra(tmp_path, 'debian', extra, key=public_key())
    with pytest.raises(ConfigError, match='extra_repositories.0.name'):
        config.load(conf)


def test_extra_repository_needs_key_url_for_a_published_page(tmp_path: Path) -> None:
    (tmp_path / 'key.asc').write_bytes(public_key())
    conf = tmp_path / 'build.conf'
    conf.write_text(
        'distribs: {debian: {repository: {url: "https://example.org/debian"},'
        ' extra_repositories: [{url: "https://x/", key: key.asc}]}}\npackages: {}\n'
    )
    with pytest.raises(ConfigError, match='key_url'):
        config.load(conf)


@pytest.mark.parametrize('path', ['.', '..', '../elsewhere', '/srv/repo', 'a/../..'])
def test_repository_path_outside_project_refused(tmp_path: Path, path: str) -> None:
    """Publishing from the states removes the repository first: never the project, nor outside."""
    project = tmp_path / 'project'
    project.mkdir()
    conf = project / 'build.conf'
    conf.write_text(f'distribs: {{debian: {{repository: {{path: "{path}"}}}}}}\npackages: {{}}\n')
    with pytest.raises(ConfigError) as caught:
        config.load(conf)
    assert str(caught.value) == (
        f'{conf}: distribs.debian.repository.path: {path} must be a directory inside the project'
    )


def test_repository_path_inside_project_accepted(tmp_path: Path) -> None:
    conf = tmp_path / 'build.conf'
    conf.write_text(
        'distribs: {debian: {repository: {path: "out/../repos/debian"}}}\npackages: {}\n'
    )
    assert config.load(conf).distribs['debian'].repository.path == Path('out/../repos/debian')


SHA = 'a' * 40


def channel_project(
    tmp_path: Path, text: str, *, native: tuple[str, ...] = (), key: bool = False
) -> Path:
    """A build.conf of text beside packages pdi and pdiplugin-x (quilt, watched, with a spec).

    The packages named in native are 3.0 (native) instead; key writes a public key to key.asc.
    """
    for name in ('pdi', 'pdiplugin-x'):
        package_dir(tmp_path, name)
        (tmp_path / name / 'debian' / 'source').mkdir()
        fmt = '3.0 (native)' if name in native else '3.0 (quilt)'
        (tmp_path / name / 'debian' / 'source' / 'format').write_text(f'{fmt}\n')
        (tmp_path / name / 'debian' / 'watch').write_text('version=4\n')
    if key:
        (tmp_path / 'key.asc').write_bytes(public_key())
    conf = tmp_path / 'build.conf'
    conf.write_text(text)
    return conf


CHANNELS_EXAMPLE = """\
channels:
    stable: { kind: release }
    weekly:
        kind: snapshot
        epoch_bump: 1
release:
    author: "Julien Bigot <julien.bigot@cea.fr>"
    packages: [ "pdi", "pdiplugin-*" ]
distribs:
    debian:
        repository:
            path: "debian"
            url: "https://repo.pdi.dev/debian/"
            channels: { weekly: { component: "weekly" } }
    fedora:
        repository:
            path: "fedora"
            url: "https://repo.pdi.dev/fedora/"
            channels:
                weekly: { path: "weekly.fedora", url: "https://repo.pdi.dev/weekly.fedora/" }
packages:
    pdi:
        channels:
            weekly:
                git: "https://github.com/pdidev/pdi.git"
                ref: "main"
                verify: { keys: key.asc }
    pdiplugin-x: {}
"""


def test_channels_example_loads(tmp_path: Path) -> None:
    loaded = config.load(channel_project(tmp_path, CHANNELS_EXAMPLE, key=True))
    assert loaded.release_channel == 'stable'
    assert loaded.snapshot_channels() == ['weekly']
    assert loaded.channels['weekly'].epoch_bump == 1
    assert loaded.fallback('weekly') == 'stable'
    assert loaded.channel_packages('stable') == ['pdi', 'pdiplugin-x']
    assert loaded.channel_packages('weekly') == ['pdi']
    entry = loaded.packages['pdi'].channels['weekly']
    assert entry.verify == config.VerifyKeys(keys=Path('key.asc'))
    assert loaded.release.packages == ('pdi', 'pdiplugin-*')
    weekly = loaded.channel_repository('fedora', 'weekly')
    assert (weekly.path, weekly.url) == (
        Path('weekly.fedora'),
        'https://repo.pdi.dev/weekly.fedora/',
    )
    assert loaded.channel_repository('fedora', 'stable') == loaded.distribs['fedora'].repository


def test_no_channels_is_one_stable(tmp_path: Path) -> None:
    loaded = config.load(channel_project(tmp_path, 'distribs: {}\npackages: {pdi: {}}\n'))
    assert loaded.release_channel == 'stable'
    assert loaded.snapshot_channels() == []
    assert loaded.packaging_dir('pdi', 'stable') == tmp_path / 'pdi'
    assert loaded.channel_packages('stable') == ['pdi']


def channel_error(tmp_path: Path, text: str, **kwargs: Any) -> str:
    with pytest.raises(ConfigError) as caught:
        config.load(channel_project(tmp_path, text, **kwargs))
    message = str(caught.value)
    assert '\n' not in message
    return message


def test_two_release_channels_refused(tmp_path: Path) -> None:
    text = 'channels: {a: {kind: release}, b: {kind: release}}\ndistribs: {}\npackages: {}\n'
    assert 'channels: exactly one channel has kind: release' in channel_error(tmp_path, text)


def test_no_release_channel_refused(tmp_path: Path) -> None:
    text = 'channels: {b: {kind: snapshot}}\ndistribs: {}\npackages: {}\n'
    assert 'channels: exactly one channel has kind: release' in channel_error(tmp_path, text)


def test_release_channel_takes_no_snapshot_setting(tmp_path: Path) -> None:
    text = 'channels: {a: {kind: release, epoch_bump: 1}}\ndistribs: {}\npackages: {}\n'
    assert 'channels.a' in channel_error(tmp_path, text)


@pytest.mark.parametrize(
    ('text', 'where'),
    [
        pytest.param(
            'channels: {s: {kind: release}}\ndistribs: {}\n'
            f'packages: {{pdi: {{channels: {{w: {{git: x, commit: "{SHA}"}}}}}}}}\n',
            'packages.pdi.channels.w: unknown channel w',
            id='package',
        ),
        pytest.param(
            'channels: {s: {kind: release}}\n'
            'distribs: {debian: {repository: {channels: {w: {component: w}}}}}\npackages: {}\n',
            'distribs.debian.repository.channels.w: unknown channel w',
            id='repository',
        ),
        pytest.param(
            'channels: {s: {kind: release}, w: {kind: snapshot, fallback: x}}\n'
            'distribs: {}\npackages: {}\n',
            'channels.w.fallback: unknown channel x',
            id='fallback',
        ),
    ],
)
def test_undeclared_channel_refused(tmp_path: Path, text: str, where: str) -> None:
    assert where in channel_error(tmp_path, text)


def test_release_channel_entry_refused(tmp_path: Path) -> None:
    text = (
        'distribs: {}\n'
        f'packages: {{pdi: {{channels: {{stable: {{git: x, commit: "{SHA}"}}}}}}}}\n'
    )
    assert (
        'packages.pdi.channels.stable: the release channel builds every package from its own'
        ' packaging' in channel_error(tmp_path, text)
    )


@pytest.mark.parametrize(
    'entry',
    [
        pytest.param(f'{{commit: "{SHA}"}}', id='no-git'),
        pytest.param(f'{{git: x, commit: "{SHA}", ref: main}}', id='commit-and-ref'),
        pytest.param('{git: x}', id='neither'),
        pytest.param('{git: x, ref: main}', id='ref-without-verify'),
        pytest.param(f'{{git: x, commit: "{SHA}", verify: none}}', id='commit-with-verify'),
        pytest.param('{git: x, commit: "abc"}', id='short-commit'),
    ],
)
def test_snapshot_entry_needs_git_and_commit_or_ref(tmp_path: Path, entry: str) -> None:
    text = (
        'channels: {s: {kind: release}, w: {kind: snapshot}}\ndistribs: {}\n'
        f'packages: {{pdi: {{channels: {{w: {entry}}}}}}}\n'
    )
    assert 'packages.pdi.channels.w' in channel_error(tmp_path, text)


@pytest.mark.parametrize('name', ['Weekly', '-x', 'a_b'])
def test_invalid_channel_name_refused(tmp_path: Path, name: str) -> None:
    text = (
        f'channels: {{s: {{kind: release}}, "{name}": {{kind: snapshot}}}}\n'
        'distribs: {}\npackages: {}\n'
    )
    assert 'channels' in channel_error(tmp_path, text)


SNAPSHOT_PDI = (
    'channels: {s: {kind: release}, w: {kind: snapshot}}\n'
    '{distribs}'
    f'packages: {{pdi: {{channels: {{w: {{git: x, commit: "{SHA}"}}}}}}}}\n'
)


def test_native_deb_snapshot_refused(tmp_path: Path) -> None:
    text = SNAPSHOT_PDI.replace('{distribs}', 'distribs: {}\n')
    message = channel_error(tmp_path, text, native=('pdi',))
    assert 'packages.pdi.channels.w: ' in message
    assert 'native' in message


def test_rpm_channel_needs_path(tmp_path: Path) -> None:
    text = SNAPSHOT_PDI.replace('{distribs}', 'distribs: {fedora: {}}\n')
    assert 'distribs.fedora.repository.channels.w.path' in channel_error(tmp_path, text)


def test_rpm_channel_needs_url_when_published(tmp_path: Path) -> None:
    text = SNAPSHOT_PDI.replace(
        '{distribs}',
        'distribs: {fedora: {repository: {url: "https://r/f/", channels: {w: {path: wf}}}}}\n',
    )
    assert 'distribs.fedora.repository.channels.w.url' in channel_error(tmp_path, text)


def test_rpm_channel_path_inside_project(tmp_path: Path) -> None:
    text = SNAPSHOT_PDI.replace(
        '{distribs}', 'distribs: {fedora: {repository: {channels: {w: {path: ".."}}}}}\n'
    )
    assert 'distribs.fedora.repository.channels.w.path' in channel_error(tmp_path, text)


def test_component_on_rpm_refused(tmp_path: Path) -> None:
    text = SNAPSHOT_PDI.replace(
        '{distribs}',
        'distribs: {fedora: {repository: {channels: {w: {path: wf, component: w}}}}}\n',
    )
    assert 'distribs.fedora.repository.channels.w' in channel_error(tmp_path, text)


def test_path_on_deb_refused(tmp_path: Path) -> None:
    text = SNAPSHOT_PDI.replace(
        '{distribs}', 'distribs: {debian: {repository: {channels: {w: {path: wd}}}}}\n'
    )
    assert 'distribs.debian.repository.channels.w' in channel_error(tmp_path, text)


def test_verify_keys_checked(tmp_path: Path) -> None:
    text = (
        'channels: {s: {kind: release}, w: {kind: snapshot}}\ndistribs: {}\n'
        'packages: {pdi: {channels: {w: {git: x, ref: main, verify: {keys: missing.asc}}}}}\n'
    )
    assert 'packages.pdi.channels.w.verify.keys' in channel_error(tmp_path, text)


def test_channel_directory_checked(tmp_path: Path) -> None:
    text = SNAPSHOT_PDI.replace('{distribs}', 'distribs: {}\n')
    conf = channel_project(tmp_path, text)
    (tmp_path / 'pdi-w').mkdir()
    with pytest.raises(ConfigError, match='packages.pdi-w|pdi-w'):
        config.load(conf)


def test_packaging_dir_prefers_channel_directory(tmp_path: Path) -> None:
    text = SNAPSHOT_PDI.replace('{distribs}', 'distribs: {}\n')
    conf = channel_project(tmp_path, text)
    package_dir(tmp_path, 'pdi-w')
    loaded = config.load(conf)
    assert loaded.packaging_dir('pdi', 'w') == tmp_path / 'pdi-w'
    assert loaded.packaging_dir('pdi', 's') == tmp_path / 'pdi'
    assert loaded.packaging_dir('pdiplugin-x', 'w') == tmp_path / 'pdiplugin-x'


def test_component_defaults(tmp_path: Path) -> None:
    text = (
        'channels: {s: {kind: release}, w: {kind: snapshot}, n: {kind: snapshot}}\n'
        'distribs: {debian: {repository: {channels: {n: {component: nightly}}}}}\n'
        'packages: {}\n'
    )
    loaded = config.load(channel_project(tmp_path, text))
    assert loaded.component('debian', 's') == 'main'
    assert loaded.component('debian', 'w') == 'w'
    assert loaded.component('debian', 'n') == 'nightly'


def test_label_defaults(tmp_path: Path) -> None:
    text = (
        'channels: {s: {kind: release}, w: {kind: snapshot},'
        ' n: {kind: snapshot, version_label: nl}}\n'
        'distribs: {}\n'
        'packages:\n'
        '  pdi: {channels: {w: {git: x, ref: release/1.x, verify: none},'
        f' n: {{git: x, commit: "{SHA}"}}}}}}\n'
        f'  pdiplugin-x: {{channels: {{w: {{git: x, commit: "{SHA}"}}}}}}\n'
    )
    loaded = config.load(channel_project(tmp_path, text))
    assert loaded.label('w', 'pdi') == 'release.1.x'
    assert loaded.label('n', 'pdi') == 'nl'
    assert loaded.label('w', 'pdiplugin-x') == 'w'


def test_release_settings_default(tmp_path: Path) -> None:
    loaded = config.load(channel_project(tmp_path, 'distribs: {}\npackages: {}\n'))
    assert loaded.release.distribution == 'unstable'
    assert loaded.release.author is None
    assert loaded.release.packages is None
