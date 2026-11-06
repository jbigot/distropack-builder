"""distro-releases list."""

import json

import respx
from typer.testing import CliRunner, Result

from distro_releases.cli import app

ON = ['--on', '2026-11-05']


def invoke(*args: str) -> Result:
    return CliRunner().invoke(app, ['list', *ON, *args])


def rows(result: Result) -> list[list[str]]:
    """The table's rows, header aside, split into columns."""
    assert result.exit_code == 0, result.output
    header, *lines = result.stdout.splitlines()
    assert header.split() == [
        'DISTRIBUTION',
        'VERSION',
        'CODENAME',
        'SUITE',
        'RELEASED',
        'EOL',
        'STATUS',
    ]
    return [line.split() for line in lines]


def test_list_default(recorded: respx.MockRouter) -> None:
    table = rows(invoke())
    assert {row[0] for row in table} == {'almalinux', 'debian', 'fedora', 'rhel', 'rocky', 'ubuntu'}
    assert {row[6] for row in table} == {'supported'}
    assert ['debian', '13', 'trixie', 'stable', '2025-08-09', '2028-08-09', 'supported'] in table
    assert ['rhel', '10', '-', '-', '2025-05-20', '2035-05-31', 'supported'] in table
    assert not [row for row in table if row[2] == 'bookworm']  # EOL since 2026-07-11


def test_list_selector(recorded: respx.MockRouter) -> None:
    table = rows(invoke('debian:bookworm,+'))
    assert [row[2] for row in table] == ['bookworm', 'trixie', 'forky', 'sid', 'rc-buggy']
    assert [row[6] for row in table] == [
        'eol',
        'supported',
        'unreleased',
        'unreleased',
        'unreleased',
    ]


def test_list_rhel(recorded: respx.MockRouter) -> None:
    assert [row[1] for row in rows(invoke('rhel'))] == ['8', '9', '10']


def test_list_json(recorded: respx.MockRouter) -> None:
    result = invoke('--json', 'debian:trixie')
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {
            'distribution': 'debian',
            'version': '13',
            'codename': 'trixie',
            'name': '13 (Trixie)',
            'suite': 'stable',
            'release_date': '2025-08-09',
            'eol_date': '2028-08-09',
            'status': 'supported',
        }
    ]


def test_list_json_null(recorded: respx.MockRouter) -> None:
    (sid,) = json.loads(invoke('--json', 'debian:sid').stdout)
    assert (sid['version'], sid['release_date'], sid['eol_date']) == (None, None, None)


def test_list_unknown(recorded: respx.MockRouter) -> None:
    result = invoke('debian:trixy')
    assert result.exit_code == 2
    assert 'trixy' in result.stderr
    result = invoke('nosuch')
    assert result.exit_code == 2
    assert 'unknown distribution nosuch' in result.stderr
