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
from typer.testing import CliRunner, Result

from distropack_builder import cli, config
from fake_runner import FakeRunner, tools_respond
from support import FIXTURE_PASSPHRASE, make_project

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
    recorded_http: respx.MockRouter, frozen: date, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    recorded_http: respx.MockRouter, frozen: date, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that selects no release says so, and touches no engine."""
    monkeypatch.chdir(tmp_path)
    write_conf(tmp_path, 'distribs: {debian: {versions: [trixie, available]}}\npackages: {}\n')
    manifest = 'https://ghcr.io/v2/jbigot/distropack-builder/debian_builder/manifests/trixie-p2'
    recorded_http.head(manifest).respond(404)

    def no_engine(host: str | None) -> Any:
        raise AssertionError('connected to an engine')

    app = cli.make_app(runner=FakeRunner(), connect=no_engine)
    result = CliRunner().invoke(app, ['run'], env={'COLUMNS': '200'})
    assert result.exit_code == 0, result.output
    assert result.stderr == 'distropack-build: nothing to build: build.conf selects no release\n'
