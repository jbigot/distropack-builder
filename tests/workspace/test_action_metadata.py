# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The two actions: their inputs reach the shell safely, their outputs come from the CLI."""

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from support import REPO_ROOT

ACTIONS = [REPO_ROOT / 'action.yml', REPO_ROOT / 'plan' / 'action.yml']


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text())
    return data


def steps(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = load(path)['runs']['steps']
    return result


@pytest.mark.parametrize('path', ACTIONS, ids=['build', 'plan'])
def test_inputs_reach_the_shell_through_env(path: Path) -> None:
    for step in steps(path):
        assert '${{' not in step.get('run', ''), step


def test_outputs_come_from_the_cli() -> None:
    build = load(ACTIONS[0])['outputs']
    assert build['failed']['value'] == '${{ steps.run.outputs.failed }}'
    assert build['output-dirs']['value'] == '${{ steps.run.outputs.output-dirs }}'
    plan = load(ACTIONS[1])['outputs']
    assert plan['matrix']['value'] == '${{ steps.plan.outputs.releases }}'


@pytest.mark.parametrize('path', ACTIONS, ids=['build', 'plan'])
def test_actions_are_pinned(path: Path) -> None:
    lines = [line for line in path.read_text().splitlines() if re.match(r'\s*-?\s*uses:', line)]
    assert lines
    for line in lines:
        assert re.search(r'uses: [\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d', line), line


def options(patterns: str) -> list[str]:
    """The options action/patterns.sh makes of patterns, as the actions call it."""
    script = '. action/patterns.sh; distribution_options "$1"; printf "%s\\0" "${options[@]}"'
    out = subprocess.run(
        ['bash', '-euc', script, 'bash', patterns],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return out.split('\0')[:-1] if out != '\0' else []


def test_patterns() -> None:
    expected = ['-D', 'debian', '-D', 'fedora:44', '-D', 'ubuntu']
    assert options('debian  fedora:44\nubuntu') == expected
    assert options('') == []
    assert options('  \n ') == []
    # a pattern is never expanded as a file name
    assert options('*') == ['-D', '*']


def test_dependabot_updates_every_action() -> None:
    config = load(REPO_ROOT / '.github' / 'dependabot.yml')
    (entry,) = (u for u in config['updates'] if u['package-ecosystem'] == 'github-actions')
    directories = set(entry.get('directories', [entry.get('directory')]))
    for path in ACTIONS:
        assert '/' + str(path.parent.relative_to(REPO_ROOT)).removeprefix('.') in directories


def setup_uv_steps(path: Path) -> list[dict[str, Any]]:
    """The setup-uv steps of a workflow or an action."""
    data = load(path)
    if 'runs' in data:
        all_steps = data['runs']['steps']
    else:
        all_steps = [step for job in data['jobs'].values() for step in job.get('steps', [])]
    return [s for s in all_steps if str(s.get('uses', '')).startswith('astral-sh/setup-uv@')]


def test_uv_is_pinned_once() -> None:
    (line,) = (REPO_ROOT / '.tool-versions').read_text().splitlines()
    assert re.fullmatch(r'uv \d+\.\d+\.\d+', line)
    expected = {
        ACTIONS[0]: '${{ github.action_path }}/.tool-versions',
        ACTIONS[1]: '${{ github.action_path }}/../.tool-versions',
    }
    paths = [*ACTIONS, *(REPO_ROOT / '.github' / 'workflows').glob('*.yml')]
    for path in paths:
        for step in setup_uv_steps(path):
            settings = step.get('with', {})
            if settings.get('version') == 'latest':
                # the Upgrade workflow, which checks the next uv
                assert path.name == 'upgrade.yml', path
                continue
            assert settings.get('version-file') == expected.get(path, '.tool-versions'), path


def run_step_arguments(tmp_path: Path, env: dict[str, str]) -> list[str]:
    """The arguments the build action's run step passes to action/run-cli, with env as its env."""
    action_path = tmp_path / 'action-path'
    (action_path / 'action').mkdir(parents=True, exist_ok=True)
    (action_path / 'action' / 'patterns.sh').write_text(
        (REPO_ROOT / 'action' / 'patterns.sh').read_text()
    )
    stub = action_path / 'action' / 'run-cli'
    stub.write_text('#!/bin/bash\nprintf "%s\\0" "$@"\n')
    stub.chmod(0o755)
    [run] = [step for step in steps(ACTIONS[0]) if step.get('id') == 'run']
    defaults = {
        'CONFIG': 'build.conf',
        'DISTRIBUTIONS': '',
        'JOBS': '',
        'VERBOSE': 'false',
        'DOWNLOAD_DIR': '',
        'CHECK_REPRODUCIBLE': 'false',
    }
    out = subprocess.run(
        ['bash', '-euc', run['run']],
        env={'PATH': '/usr/bin:/bin', 'GITHUB_ACTION_PATH': str(action_path), **defaults, **env},
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return out.split('\0')[:-1]


def test_verbose_input_reaches_the_cli(tmp_path: Path) -> None:
    run = [step for step in steps(ACTIONS[0]) if step.get('id') == 'run'][0]
    assert run['env']['VERBOSE'] == '${{ inputs.verbose }}'
    assert load(ACTIONS[0])['inputs']['verbose']['default'] == 'false'
    assert run_step_arguments(tmp_path, {'VERBOSE': 'true'}) == ['run', '--verbose', 'build.conf']
    assert run_step_arguments(tmp_path, {'VERBOSE': 'false'}) == ['run', 'build.conf']


def test_download_dir_input_reaches_the_cli(tmp_path: Path) -> None:
    args = run_step_arguments(tmp_path, {'DOWNLOAD_DIR': 'cache/sources'})
    assert args == ['run', '--download-dir', 'cache/sources', 'build.conf']


def test_check_reproducible_input_reaches_the_cli(tmp_path: Path) -> None:
    args = run_step_arguments(tmp_path, {'CHECK_REPRODUCIBLE': 'true'})
    assert args == ['run', '--check-reproducible', 'build.conf']
