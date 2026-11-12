# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The typer command line: its commands, its options, the passphrase, its errors."""

import json
import os
import re
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
import respx
import yaml
from typer.testing import CliRunner, Result

from distropack_builder import cli, config
from distropack_builder.bake import PROTOCOL
from distropack_builder.docker import RunResult
from fake_runner import Call, FakeRunner, tools_respond
from support import (
    FIXTURE_PASSPHRASE,
    SNAP,
    add_snapshot_package,
    add_upstream_commit,
    make_project,
    use_channels,
)

PASSPHRASE_ENV = 'DISTROPACK_GPG_PASSPHRASE'


def invoke(
    *args: str, runner: FakeRunner | None = None, env: dict[str, str] | None = None
) -> Result:
    """distropack-build with args, on a fake runner, no engine; the outcome."""
    app = cli.make_app(runner=runner or FakeRunner(), connect=lambda host: cast(Any, None))
    return CliRunner().invoke(app, list(args), env={'COLUMNS': '200', **(env or {})})


def test_help_lists_commands() -> None:
    result = invoke('--help')
    assert result.exit_code == 0
    for command in ('run', 'images', 'config', 'releases'):
        assert re.search(rf'\b{command}\b', result.stdout), command


def test_no_command_shows_help() -> None:
    result = invoke()
    assert 'run' in result.output and 'images' in result.output


def test_run_options() -> None:
    result = invoke('run', '--help')
    assert result.exit_code == 0
    out = result.stdout
    for option in ('--passphrase-file', '--distributions', '--jobs', '--verbose', '--docker-host'):
        assert option in out, option
    assert '--passphrase ' not in out
    assert not re.search(r'(^|\s)-p\b', out)
    assert '--interactive' not in out


def test_passphrase_file_wins(tmp_path: Path) -> None:
    (tmp_path / 'p').write_text('from-file')
    assert cli.read_passphrase(tmp_path / 'p', {PASSPHRASE_ENV: 'from-env'}) == 'from-file'


def test_passphrase_env() -> None:
    assert cli.read_passphrase(None, {PASSPHRASE_ENV: 'from-env'}) == 'from-env'
    assert cli.read_passphrase(None, {}) is None


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('s3cret\r\n', 's3cret'),
        ('s3cret\n', 's3cret'),
        ('s3cret\n\n', 's3cret\n'),
        ('s3cret', 's3cret'),
    ],
)
def test_passphrase_file_line_ending(tmp_path: Path, text: str, expected: str) -> None:
    (tmp_path / 'p').write_bytes(text.encode())
    assert cli.read_passphrase(tmp_path / 'p', {}) == expected


@pytest.fixture
def project(
    recorded_http: respx.MockRouter,
    unpublished: set[str],
    frozen: date,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """The fixture project, Debian trixie and hello-undeclared alone, as the current directory."""
    project = make_project(
        tmp_path / 'project', 'https://orig.test', {'debian': ['trixie']}, ['hello-undeclared']
    )
    monkeypatch.chdir(project)
    yield project


def test_passphrase_from_file_builds(project: Path) -> None:
    (project / 'passphrase').write_bytes(FIXTURE_PASSPHRASE.encode() + b'\r\n')
    runner = FakeRunner(tools_respond)
    result = invoke('run', '--passphrase-file', 'passphrase', runner=runner)
    assert result.exit_code == 0, result.output
    assert runner.calls


SECRET = 'n0t-the-Passphrase'


@pytest.mark.parametrize(
    ('args', 'env', 'file', 'status'),
    [
        pytest.param(['--passphrase-file', 'p'], {}, SECRET, 1, id='wrong-in-file'),
        pytest.param([], {PASSPHRASE_ENV: SECRET}, None, 1, id='wrong-in-environment'),
        pytest.param(['--passphrase-file', 'p'], {PASSPHRASE_ENV: SECRET}, '', 1, id='empty-file'),
        pytest.param(
            ['--passphrase-file', 'missing'], {PASSPHRASE_ENV: SECRET}, None, 2, id='missing-file'
        ),
        pytest.param(['--jobs', 'x'], {PASSPHRASE_ENV: SECRET}, None, 2, id='usage-error'),
    ],
)
def test_passphrase_never_printed(
    project: Path, args: list[str], env: dict[str, str], file: str | None, status: int
) -> None:
    if file is not None:
        (project / 'p').write_text(file)
    runner = FakeRunner(tools_respond)
    result = invoke('run', *args, runner=runner, env=env)
    assert result.exit_code == status, result.output
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr
    assert runner.calls == []


def test_wrong_passphrase_is_one_line(project: Path) -> None:
    result = invoke('run', env={PASSPHRASE_ENV: SECRET})
    assert result.exit_code == 1
    (line,) = result.stderr.splitlines()
    assert line.startswith('distropack-build: error: the passphrase does not unlock')


def test_verbose_keeps_jobs(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, 'cpu_count', lambda: 3)
    runner = FakeRunner(tools_respond)
    result = invoke('run', '-v', runner=runner, env={PASSPHRASE_ENV: FIXTURE_PASSPHRASE})
    assert result.exit_code == 0, result.output
    (build,) = (call for call in runner.calls if call.is_build)
    assert '-j4' in build.args


def test_config_schema(tmp_path: Path) -> None:
    expected = json.dumps(config.schema(), indent=2) + '\n'
    assert invoke('config', 'schema').stdout == expected
    assert invoke('config', 'schema', '-o', str(tmp_path / 's.json')).exit_code == 0
    assert (tmp_path / 's.json').read_text() == expected


def write_conf(directory: Path, text: str) -> None:
    (directory / 'build.conf').write_text(text)


def test_releases_list(
    recorded_http: respx.MockRouter, frozen: date, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    write_conf(
        tmp_path,
        'distribs: {debian: {versions: [bookworm, trixie]}, fedora: {versions: [supported]}}\n'
        'packages: {}\n',
    )

    text = invoke('releases', 'list', '-D', 'debian')
    assert text.exit_code == 0, text.output
    assert text.stdout == 'debian:bookworm\ndebian:trixie\n'

    both = invoke('releases', 'list', 'build.conf', '-D', 'debian:trixie', '-D', 'fedora', '--json')
    assert both.exit_code == 0, both.output
    assert json.loads(both.stdout) == [
        {'distrib': 'debian', 'release': 'trixie'},
        {'distrib': 'fedora', 'release': '43'},
        {'distrib': 'fedora', 'release': '44'},
        {'distrib': 'fedora', 'release': '45'},
    ]


@pytest.mark.parametrize(
    ('text', 'args', 'expected'),
    [
        pytest.param(
            'distribs: {debian: {versionz: []}}\npackages: {}\n',
            [],
            'distribs.debian.versionz',
            id='misspelt',
        ),
        pytest.param(
            'distribs: {debian: {versions: [trixie]}}\npackages: {}\n',
            ['-D', 'fedra'],
            '-D fedra names no distribution of build.conf',
            id='unknown-pattern',
        ),
        pytest.param(
            'distribs: {debian: {versions: [trixie]}}\npackages: {}\n',
            ['-D', 'debian:bookworm'],
            '-D debian:bookworm names no release build.conf selects',
            id='unselected-release',
        ),
    ],
)
@pytest.mark.parametrize('command', [['run'], ['releases', 'list']], ids=['run', 'releases'])
def test_config_error_exit_2(
    recorded_http: respx.MockRouter,
    frozen: date,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    text: str,
    args: list[str],
    expected: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    write_conf(tmp_path, text)
    runner = FakeRunner()
    result = invoke(*command, *args, runner=runner)
    assert result.exit_code == 2
    (line,) = result.stderr.splitlines()
    assert line.startswith('distropack-build: error: ')
    assert expected in line
    assert runner.calls == []


def test_missing_build_conf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = invoke('run')
    assert result.exit_code == 2
    assert 'build.conf' in result.stderr


def test_nothing_to_build(
    recorded_http: respx.MockRouter,
    unpublished: set[str],
    frozen: date,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that selects no release says so, and touches no engine."""
    monkeypatch.chdir(tmp_path)
    write_conf(tmp_path, 'distribs: {debian: {versions: [trixie, available]}}\npackages: {}\n')
    unpublished.add(f'trixie-p{PROTOCOL}')

    def no_engine(host: str | None) -> Any:
        raise AssertionError('connected to an engine')

    app = cli.make_app(runner=FakeRunner(), connect=no_engine)
    result = CliRunner().invoke(app, ['run'], env={'COLUMNS': '200'})
    assert result.exit_code == 0, result.output
    assert result.stderr == 'distropack-build: nothing to build: build.conf selects no release\n'


def read_outputs(path: Path) -> dict[str, str]:
    """The name=value lines of a GITHUB_OUTPUT file."""
    return dict(line.split('=', 1) for line in path.read_text().splitlines())


def fail_builds(call: Call) -> RunResult | None:
    """The tools answer, every build fails."""
    if call.is_build:
        raise RuntimeError('the build failed')
    return tools_respond(call)


def test_run_writes_outputs(project: Path, tmp_path: Path) -> None:
    output = tmp_path / 'output'
    env = {PASSPHRASE_ENV: FIXTURE_PASSPHRASE, 'GITHUB_OUTPUT': str(output)}
    result = invoke('run', runner=FakeRunner(tools_respond), env=env)
    assert result.exit_code == 0, result.output
    outputs = read_outputs(output)
    assert outputs['failed'] == 'false'
    assert json.loads(outputs['output-dirs']) == {'debian': str(project / 'repositories/debian')}


def test_failed_run_writes_failed_true(project: Path, tmp_path: Path) -> None:
    output = tmp_path / 'output'
    env = {PASSPHRASE_ENV: FIXTURE_PASSPHRASE, 'GITHUB_OUTPUT': str(output)}
    result = invoke('run', runner=FakeRunner(fail_builds), env=env)
    assert result.exit_code == 1, result.output
    assert read_outputs(output)['failed'] == 'true'


def test_output_dirs_are_absolute(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project.parent)
    output = tmp_path / 'output'
    env = {PASSPHRASE_ENV: FIXTURE_PASSPHRASE, 'GITHUB_OUTPUT': str(output)}
    result = invoke('run', 'project/build.conf', runner=FakeRunner(tools_respond), env=env)
    assert result.exit_code == 0, result.output
    (directory,) = json.loads(read_outputs(output)['output-dirs']).values()
    assert Path(directory).is_absolute()
    assert Path(directory) == project.resolve() / 'repositories/debian'


def test_unwritable_github_output(project: Path, tmp_path: Path) -> None:
    env = {PASSPHRASE_ENV: FIXTURE_PASSPHRASE, 'GITHUB_OUTPUT': str(tmp_path / 'no' / 'output')}
    result = invoke('run', runner=FakeRunner(tools_respond), env=env)
    assert result.exit_code == 1
    (line,) = result.stderr.splitlines()
    assert line.startswith('distropack-build: error: cannot write the outputs to')


def test_nothing_selected_writes_outputs(
    recorded_http: respx.MockRouter,
    unpublished: set[str],
    frozen: date,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    write_conf(tmp_path, 'distribs: {debian: {versions: [trixie, available]}}\npackages: {}\n')
    unpublished.add(f'trixie-p{PROTOCOL}')
    output = tmp_path / 'output'
    result = invoke('run', env={'GITHUB_OUTPUT': str(output)})
    assert result.exit_code == 0, result.output
    assert read_outputs(output) == {'failed': 'false', 'output-dirs': '{}'}


def test_releases_list_writes_releases(
    recorded_http: respx.MockRouter, frozen: date, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    write_conf(tmp_path, 'distribs: {debian: {versions: [bookworm, trixie]}}\npackages: {}\n')
    output = tmp_path / 'output'
    result = invoke('releases', 'list', '--json', env={'GITHUB_OUTPUT': str(output)})
    assert result.exit_code == 0, result.output
    expected = json.dumps(json.loads(result.stdout), separators=(',', ':'))
    assert read_outputs(output) == {'releases': expected}
    # the plain text listing writes the same output
    output.unlink()
    assert invoke('releases', 'list', env={'GITHUB_OUTPUT': str(output)}).exit_code == 0
    assert read_outputs(output) == {'releases': expected}


def test_run_error_writes_failed_true(project: Path, tmp_path: Path) -> None:
    output = tmp_path / 'output'
    result = invoke('run', env={PASSPHRASE_ENV: SECRET, 'GITHUB_OUTPUT': str(output)})
    assert result.exit_code == 1
    assert read_outputs(output) == {'failed': 'true', 'output-dirs': '{}'}


def test_usage_error_writes_failed_true(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    output = tmp_path / 'output'
    result = invoke('run', env={'GITHUB_OUTPUT': str(output)})
    assert result.exit_code == 2
    assert read_outputs(output) == {'failed': 'true', 'output-dirs': '{}'}


def run_with_bookworm(
    project: Path, monkeypatch: pytest.MonkeyPatch, *args: str, available: bool = False
) -> tuple[Result, FakeRunner]:
    """run on the fixture project selecting bookworm and trixie; its result and its runner."""
    conf = yaml.safe_load((project / 'build.conf').read_text())
    versions = ['bookworm', 'trixie', *(['available'] if available else [])]
    conf['distribs']['debian']['versions'] = versions
    (project / 'build.conf').write_text(yaml.safe_dump(conf))
    runner = FakeRunner(tools_respond)
    env = {PASSPHRASE_ENV: FIXTURE_PASSPHRASE}
    return invoke('run', *args, runner=runner, env=env), runner


def test_missing_builder_image_fails_its_release_once(
    project: Path, unpublished: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    unpublished.add(f'bookworm-p{PROTOCOL}')
    result, runner = run_with_bookworm(project, monkeypatch)
    assert result.exit_code == 1, result.output
    lines = [line for line in result.output.splitlines() if 'no builder image' in line]
    assert lines == ['no builder image for Debian GNU/Linux 12 (Bookworm): not published yet']
    assert not [call for call in runner.calls if 'bookworm' in call.image]
    assert (project / 'repositories/debian/dists/trixie/InRelease').is_file()
    assert not (project / 'repositories/debian/dists/bookworm').exists()


@pytest.mark.parametrize('frozen', [date(2028, 7, 1)], indirect=True)
def test_missing_image_of_a_release_out_of_support(
    project: Path, unpublished: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    unpublished.add(f'bookworm-p{PROTOCOL}')
    result, _ = run_with_bookworm(project, monkeypatch, '-D', 'debian:bookworm')
    assert result.exit_code == 1, result.output
    assert (
        'no builder image for Debian GNU/Linux 12 (Bookworm): its extended support ended on'
        ' 2028-06-30' in result.output
    )


def test_available_still_drops_silently(
    project: Path, unpublished: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    unpublished.add(f'bookworm-p{PROTOCOL}')
    result, _ = run_with_bookworm(project, monkeypatch, available=True)
    assert result.exit_code == 0, result.output
    assert 'no builder image' not in result.output


def test_registry_error_fails_the_run(project: Path, unpublished: set[str]) -> None:
    """A registry that does not answer is an error, never read as a missing image."""
    unpublished.add('ERROR')
    runner = FakeRunner(tools_respond)
    result = invoke('run', runner=runner, env={PASSPHRASE_ENV: FIXTURE_PASSPHRASE})
    assert result.exit_code == 1, result.output
    assert 'cannot tell whether the registry publishes' in result.output
    assert 'no builder image' not in result.output
    assert not runner.calls


def test_unpublished_release_stays_on_the_page(
    project: Path, unpublished: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release failed for its missing image stays listed on the page, as a failed build does."""
    unpublished.add(f'bookworm-p{PROTOCOL}')
    release = project / 'repositories' / 'debian' / 'dists' / 'bookworm' / 'Release'
    release.parent.mkdir(parents=True)
    release.write_text('Codename: bookworm\n')
    result, _ = run_with_bookworm(project, monkeypatch)
    assert result.exit_code == 1, result.output
    page = (project / 'repositories' / 'debian' / 'index.html').read_text()
    assert 'Bookworm' in page and 'Trixie' in page


class LocalImages:
    """An engine that has some images locally, as after `images bake` or a pull."""

    def __init__(self, *references: str) -> None:
        self.references = set(references)

    def has_image(self, reference: str) -> bool:
        return reference in self.references


def test_image_the_engine_has_is_not_missing(
    project: Path, unpublished: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An image baked locally builds its release, though the registry does not publish it."""
    unpublished.add('bookworm-p2')
    conf = yaml.safe_load((project / 'build.conf').read_text())
    conf['distribs']['debian']['versions'] = ['bookworm', 'trixie']
    (project / 'build.conf').write_text(yaml.safe_dump(conf))
    local = LocalImages('ghcr.io/jbigot/distropack-builder/debian_builder:bookworm-p2')
    app = cli.make_app(runner=FakeRunner(tools_respond), connect=lambda host: cast(Any, local))
    result = CliRunner().invoke(
        app, ['run'], env={'COLUMNS': '200', PASSPHRASE_ENV: FIXTURE_PASSPHRASE}
    )
    assert result.exit_code == 0, result.output
    assert 'no builder image' not in result.output


def test_publish_only_ignores_missing_builder_images(
    project: Path, unpublished: set[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Publishing from the states builds nothing: no builder image is needed, nor asked for."""
    states = str(tmp_path / 'states')
    result, _ = run_with_bookworm(project, monkeypatch, '--state-dir', states, '--no-publish')
    assert result.exit_code == 0, result.output
    unpublished.add(f'bookworm-p{PROTOCOL}')
    result, _ = run_with_bookworm(project, monkeypatch, '--state-dir', states, '--publish-only')
    assert result.exit_code == 0, result.output
    assert 'no builder image' not in result.output
    assert (project / 'repositories/debian/dists/bookworm/InRelease').is_file()


# Channels (Phase 13).
def test_run_unknown_channel(project: Path) -> None:
    result = invoke('run', '--channel', 'nope', env={PASSPHRASE_ENV: FIXTURE_PASSPHRASE})
    assert result.exit_code == 2
    assert 'unknown channel nope' in result.output


@pytest.fixture
def snapshot_project(
    recorded_http: respx.MockRouter,
    unpublished: set[str],
    frozen: date,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """hello-lib, and hello-snap in channel weekly, Debian trixie, as the current directory."""
    project = make_project(
        tmp_path / 'project', 'https://orig.test', {'debian': ['trixie']}, ['hello-lib']
    )
    use_channels(project)
    add_snapshot_package(project)
    monkeypatch.chdir(project)
    yield project


def upstream(project: Path) -> Path:
    return project / 'upstream' / 'hello-lib.git'


def built(call: Call) -> RunResult | None:
    """The tools, and builders that report an environment and build a package."""
    if call.is_deps:
        (call.targets['/buildenv'].source / 'installed').write_text('base-files 13.8\n')
    if call.is_build:
        (call.targets['/src'].source / 'x.pkg').write_text('built')
    return tools_respond(call)


def run_channel(*args: str) -> Result:
    return invoke('run', *args, runner=FakeRunner(built), env={PASSPHRASE_ENV: FIXTURE_PASSPHRASE})


def snapshots_of(download: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((download / 'snapshots.json').read_text()))


def test_fetch_writes_snapshots_and_run_uses_them(snapshot_project: Path, tmp_path: Path) -> None:
    download, states = tmp_path / 'download', tmp_path / 'states'
    fetched = add_upstream_commit(upstream(snapshot_project), 'one')
    result = invoke(
        'fetch', '--channel', 'weekly', '--download-dir', str(download), runner=FakeRunner(built)
    )
    assert result.exit_code == 0, result.output
    assert snapshots_of(download)['weekly'][SNAP]['commit'] == fetched
    add_upstream_commit(upstream(snapshot_project), 'two')  # the branch moved since
    assert run_channel('--state-dir', str(states)).exit_code == 0
    result = run_channel(
        '--channel', 'weekly', '--state-dir', str(states), '--download-dir', str(download)
    )
    assert result.exit_code == 0, result.output
    stored = json.loads((states / 'weekly' / 'debian-13-trixie' / 'manifest.json').read_text())
    assert stored['packages'][SNAP]['snapshot_commit'] == fetched


def test_fetch_snapshot_option(snapshot_project: Path, tmp_path: Path) -> None:
    download = tmp_path / 'download'
    first = add_upstream_commit(upstream(snapshot_project), 'one')
    add_upstream_commit(upstream(snapshot_project), 'two')
    result = invoke(
        'fetch',
        '--channel',
        'weekly',
        '--download-dir',
        str(download),
        '--snapshot',
        f'{SNAP}={first}',
        runner=FakeRunner(built),
    )
    assert result.exit_code == 0, result.output
    assert snapshots_of(download)['weekly'][SNAP]['commit'] == first


@pytest.mark.parametrize(
    ('value', 'message'),
    [
        pytest.param('hello-lib=abc', 'hello-lib: ', id='undeclared'),
        pytest.param('hello-snap', 'PACKAGE=COMMIT', id='malformed'),
    ],
)
def test_fetch_snapshot_refused(
    snapshot_project: Path, tmp_path: Path, value: str, message: str
) -> None:
    result = invoke(
        'fetch',
        '--channel',
        'weekly',
        '--download-dir',
        str(tmp_path / 'download'),
        '--snapshot',
        value,
        runner=FakeRunner(built),
    )
    assert result.exit_code == 2
    assert message in result.output
    assert not (tmp_path / 'download' / 'snapshots.json').exists()


def fetch_changed(tmp_path: Path, env: dict[str, str] | None = None) -> Result:
    return invoke(
        'fetch',
        '--channel',
        'weekly',
        '--download-dir',
        str(tmp_path / 'download'),
        '--state-dir',
        str(tmp_path / 'states'),
        runner=FakeRunner(built),
        env=env,
    )


def built_weekly(project: Path, tmp_path: Path) -> None:
    """The release channel, then the weekly channel, built into tmp_path/states."""
    add_upstream_commit(upstream(project), 'one')
    states = str(tmp_path / 'states')
    assert run_channel('--state-dir', states).exit_code == 0
    result = run_channel(
        '--channel', 'weekly', '--state-dir', states, '--download-dir', str(tmp_path / 'download')
    )
    assert result.exit_code == 0, result.output


def test_changed_false_when_nothing_moved(snapshot_project: Path, tmp_path: Path) -> None:
    built_weekly(snapshot_project, tmp_path)
    result = fetch_changed(tmp_path)
    assert result.exit_code == 0, result.output
    assert 'changed=false' in result.stdout


def test_changed_true_after_a_commit(snapshot_project: Path, tmp_path: Path) -> None:
    built_weekly(snapshot_project, tmp_path)
    add_upstream_commit(upstream(snapshot_project), 'two')
    result = fetch_changed(tmp_path)
    assert result.exit_code == 0, result.output
    assert 'changed=true' in result.stdout


def test_changed_true_after_a_packaging_change(snapshot_project: Path, tmp_path: Path) -> None:
    built_weekly(snapshot_project, tmp_path)
    control = snapshot_project / SNAP / 'debian' / 'control'
    control.write_text(control.read_text() + '\n')
    result = fetch_changed(tmp_path)
    assert result.exit_code == 0, result.output
    assert 'changed=true' in result.stdout


def test_changed_true_without_a_state(snapshot_project: Path, tmp_path: Path) -> None:
    add_upstream_commit(upstream(snapshot_project), 'one')
    result = fetch_changed(tmp_path)
    assert result.exit_code == 0, result.output
    assert 'changed=true' in result.stdout


def test_changed_written_to_github_output(snapshot_project: Path, tmp_path: Path) -> None:
    built_weekly(snapshot_project, tmp_path)
    output = tmp_path / 'github-output'
    output.touch()
    result = fetch_changed(tmp_path, env={'GITHUB_OUTPUT': str(output)})
    assert result.exit_code == 0, result.output
    assert read_outputs(output) == {'changed': 'false'}


def test_failed_snapshot_fails_fetch(snapshot_project: Path, tmp_path: Path) -> None:
    conf_path = snapshot_project / 'build.conf'
    conf = yaml.safe_load(conf_path.read_text())
    conf['packages'][SNAP]['channels']['weekly']['git'] = 'file:///nonexistent.git'
    conf_path.write_text(yaml.safe_dump(conf))
    result = invoke(
        'fetch',
        '--channel',
        'weekly',
        '--download-dir',
        str(tmp_path / 'download'),
        runner=FakeRunner(built),
    )
    assert result.exit_code == 1
    assert SNAP in result.output
    assert 'error' in snapshots_of(tmp_path / 'download')['weekly'][SNAP]


def test_output_dirs_list_channel_repositories(
    recorded_http: respx.MockRouter,
    unpublished: set[str],
    frozen: date,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(
        tmp_path / 'project',
        'https://orig.test',
        {'debian': ['trixie'], 'fedora': ['44']},
        ['hello-lib'],
    )
    use_channels(project)
    add_snapshot_package(project)
    monkeypatch.chdir(project)
    output = tmp_path / 'output'
    output.touch()
    env = {PASSPHRASE_ENV: FIXTURE_PASSPHRASE, 'GITHUB_OUTPUT': str(output)}
    states = str(tmp_path / 'states')
    result = invoke(
        'run', '--publish-only', '--state-dir', states, runner=FakeRunner(built), env=env
    )
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )
    dirs = json.loads(read_outputs(output)['output-dirs'])
    assert dirs == {
        'debian': str(project.resolve() / 'repositories/debian'),
        'fedora': str(project.resolve() / 'repositories/fedora'),
        'fedora.weekly': str(project.resolve() / 'repositories/weekly.fedora'),
    }
