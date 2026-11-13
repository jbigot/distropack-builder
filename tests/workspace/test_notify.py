# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The notify action's request, and the reusable workflow's reading of it (action/notify.sh)."""

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

from support import REPO_ROOT

OLD = 'a' * 40
CHANNELS = json.dumps(
    [
        {'name': 'stable', 'kind': 'release', 'fallback': None, 'packages': ['pdi', 'plugin']},
        {
            'name': 'nightly',
            'kind': 'snapshot',
            'fallback': 'stable',
            'packages': ['pdi', 'plugin'],
        },
        {'name': 'weekly', 'kind': 'snapshot', 'fallback': 'stable', 'packages': ['pdi']},
    ]
)


def notify(function: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['bash', '-euc', f'. action/notify.sh; {function} "$@"', 'bash', *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_dispatch_body() -> None:
    result = notify('dispatch_body', 'nightly', 'main', OLD, 'pdi  pdiplugin-mpi\n')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        'event_type': 'distropack-build',
        'client_payload': {
            'channel': 'nightly',
            'ref': 'main',
            'commit': OLD,
            'packages': ['pdi', 'pdiplugin-mpi'],
        },
    }


def test_dispatch_body_no_packages() -> None:
    result = notify('dispatch_body', 'nightly', 'main', OLD, '')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['client_payload']['packages'] == []


def payload(**fields: Any) -> str:
    return json.dumps({'channel': 'nightly', 'ref': 'main', 'commit': OLD, **fields})


def test_payload_snapshots_listed() -> None:
    result = notify('payload_snapshots', payload(packages=['plugin']), CHANNELS, 'nightly')
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [f'plugin={OLD}']


def test_payload_snapshots_all_of_channel() -> None:
    choices: list[list[str] | None] = [[], None]
    for packages in choices:
        fields = {} if packages is None else {'packages': packages}
        result = notify('payload_snapshots', payload(**fields), CHANNELS, 'nightly')
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == [f'pdi={OLD}', f'plugin={OLD}']


def test_payload_snapshots_rejects_bad_commit() -> None:
    for commit in ('abc', 'A' * 40, f'{OLD} x', '$(touch pwned)'):
        result = notify('payload_snapshots', payload(commit=commit), CHANNELS, 'nightly')
        assert result.returncode != 0, commit
        assert 'commit' in result.stdout + result.stderr
    assert not (REPO_ROOT / 'pwned').exists()


def test_payload_snapshots_rejects_other_channel() -> None:
    result = notify('payload_snapshots', payload(channel='weekly'), CHANNELS, 'nightly')
    assert result.returncode != 0
    assert 'weekly' in result.stdout + result.stderr


def test_payload_snapshots_rejects_unknown_package() -> None:
    weekly = payload(channel='weekly', packages=['plugin'])
    result = notify('payload_snapshots', weekly, CHANNELS, 'weekly')
    assert result.returncode != 0
    assert 'plugin' in result.stdout + result.stderr


def test_payload_snapshots_rejects_what_is_no_payload() -> None:
    for text in ('', 'not json', '[]', '{"channel": "nightly"}'):
        result = notify('payload_snapshots', text, CHANNELS, 'nightly')
        assert result.returncode != 0, text


def test_notify_action_inputs() -> None:
    action = yaml.safe_load((REPO_ROOT / 'notify' / 'action.yml').read_text())
    inputs = action['inputs']
    for name in ('repository', 'channel', 'token'):
        assert inputs[name]['required'] is True, name
    assert inputs['packages']['default'] == ''
    assert inputs['dry-run']['default'] == 'false'
    for step in action['runs']['steps']:
        assert '${{' not in step.get('run', ''), step


def test_notify_dry_run_prints_the_request_not_the_token(tmp_path: Path) -> None:
    """The action's step, run as GitHub would, with dry-run: the request, never the token."""
    action = yaml.safe_load((REPO_ROOT / 'notify' / 'action.yml').read_text())
    (step,) = [s for s in action['runs']['steps'] if s.get('id') == 'notify']
    env = {
        'PATH': '/usr/bin:/bin',
        'GITHUB_ACTION_PATH': str(REPO_ROOT / 'notify'),
        'GITHUB_API_URL': 'https://api.github.com',
        'GITHUB_REF_NAME': 'main',
        'GITHUB_SHA': OLD,
        'REPOSITORY': 'pdidev/pkgs',
        'CHANNEL': 'nightly',
        'TOKEN': 'secret-token',
        'PACKAGES': 'pdi',
        'DRY_RUN': 'true',
        'GITHUB_OUTPUT': str(tmp_path / 'output'),
    }
    result = subprocess.run(
        ['bash', '-euo', 'pipefail', '-c', step['run']], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert 'secret-token' not in result.stdout + result.stderr
    first, *rest = result.stdout.splitlines()
    assert first == 'POST https://api.github.com/repos/pdidev/pkgs/dispatches'
    assert json.loads('\n'.join(rest))['client_payload']['packages'] == ['pdi']
    (line,) = (tmp_path / 'output').read_text().splitlines()
    assert line.startswith('body=')
    assert json.loads(line.removeprefix('body=')) == json.loads('\n'.join(rest))


def run_snapshots_step(
    tmp_path: Path, snapshots: str, dispatch: str
) -> subprocess.CompletedProcess[str]:
    """The plan job's snapshots step, run as GitHub would, the CLI listing CHANNELS."""
    workflow = yaml.safe_load((REPO_ROOT / '.github' / 'workflows' / 'distropack.yml').read_text())
    (step,) = [s for s in workflow['jobs']['plan']['steps'] if s.get('id') == 'snapshots']
    builder = tmp_path / '.distropack-builder' / 'action'
    builder.mkdir(parents=True)
    (builder / 'notify.sh').write_text((REPO_ROOT / 'action' / 'notify.sh').read_text())
    cli = builder / 'run-cli'
    cli.write_text(
        f"#!/bin/sh\n[ \"$*\" = 'channels list --json build.conf' ] && cat <<'X'\n{CHANNELS}\nX\n"
    )
    cli.chmod(0o755)
    output = tmp_path / 'output'
    output.touch()
    env = {
        'PATH': '/usr/bin:/bin',
        'GITHUB_WORKSPACE': str(tmp_path),
        'GITHUB_OUTPUT': str(output),
        'CONFIG': 'build.conf',
        'CHANNEL': 'nightly',
        'SNAPSHOTS': snapshots,
        'PAYLOAD': dispatch,
    }
    assert set(step['env']) == set(env) - {'PATH', 'GITHUB_WORKSPACE', 'GITHUB_OUTPUT'}
    result = subprocess.run(
        ['bash', '-eo', 'pipefail', '-c', step['run']], env=env, capture_output=True, text=True
    )
    result.stdout = output.read_text()
    return result


def test_snapshots_step_merges_the_payload(tmp_path: Path) -> None:
    result = run_snapshots_step(tmp_path, 'other=' + 'b' * 40 + '\n', payload(packages=['pdi']))
    assert result.returncode == 0, result.stderr
    assert result.stdout == f'snapshots=other={"b" * 40} pdi={OLD}\n'


def test_snapshots_step_without_payload(tmp_path: Path) -> None:
    result = run_snapshots_step(tmp_path, ' x=y\nz=w ', '')
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'snapshots=x=y z=w\n'


def test_snapshots_step_fails_on_a_bad_payload(tmp_path: Path) -> None:
    result = run_snapshots_step(tmp_path, '', payload(channel='weekly'))
    assert result.returncode != 0
    assert result.stdout == ''
