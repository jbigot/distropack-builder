# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The reusable workflow, read as data: what only GitHub runs, checked as far as YAML goes."""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from support import REPO_ROOT

WORKFLOW = REPO_ROOT / '.github' / 'workflows' / 'distropack.yml'
INPUTS = {
    'channel': '',
    'config': 'build.conf',
    'working-directory': '.',
    'distributions': '',
    'snapshots': '',
    'dispatch-payload': '',
    'build-if': 'always',
    'state-registry': '',
    'keep-untagged': 50,
    'max-site-size': '',
    'site-extra': '',
    'check-install': 'upgrade',
    'version-guard': True,
    'publish': True,
    'project-artifact': '',
    'download-dir': '',
    'bake-images': False,
}


def load(path: Path = WORKFLOW) -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load(path.read_text())
    return data


def jobs() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = load()['jobs']
    return result


def call() -> dict[str, Any]:
    result: dict[str, Any] = load()[True]['workflow_call']  # YAML reads `on` as True
    return result


def test_inputs_and_their_defaults() -> None:
    inputs = call()['inputs']
    for name, default in INPUTS.items():
        assert name in inputs, name
        assert inputs[name].get('default') == default, name
        assert inputs[name]['description'].strip(), name
    assert call()['secrets']['passphrase']['required'] is True
    assert set(call()['outputs']) >= {'site-artifact', 'changed'}


def test_every_input_is_used() -> None:
    text = WORKFLOW.read_text()
    for name in call()['inputs']:
        assert re.search(rf'inputs\.{re.escape(name)}\b', text), name


@pytest.mark.parametrize(
    'path',
    [
        WORKFLOW,
        REPO_ROOT / 'action.yml',
        REPO_ROOT / 'plan' / 'action.yml',
        REPO_ROOT / 'notify' / 'action.yml',
    ],
    ids=lambda p: p.relative_to(REPO_ROOT).as_posix(),
)
def test_no_expression_in_run(path: Path) -> None:
    data = load(path)
    if 'runs' in data:
        all_steps = data['runs']['steps']
    else:
        all_steps = [s for job in data['jobs'].values() for s in job.get('steps', [])]
    for step in all_steps:
        assert '${{' not in step.get('run', ''), step


def test_permissions_per_job() -> None:
    assert load()['permissions'] == {}
    writing = set()
    for name, job in jobs().items():
        permissions = job['permissions']
        assert permissions.get('contents') == 'read', name
        if permissions.get('packages') == 'write':
            writing.add(name)
    assert writing == {'build', 'prune'}


def test_channel_runs_serialized() -> None:
    group = load()['concurrency']
    assert 'inputs.channel' in group['group'] and 'inputs.state-registry' in group['group']
    assert 'github.workflow' not in group['group']
    assert group['cancel-in-progress'] is False
    publish = jobs()['publish']['concurrency']
    assert publish['group'].startswith('distropack-publish-')
    assert 'inputs.state-registry' in publish['group']
    assert publish['cancel-in-progress'] is False
    assert publish['group'] != group['group']


def step_named(job: str, name: str) -> dict[str, Any]:
    (step,) = [s for s in jobs()[job]['steps'] if s.get('name') == name]
    found: dict[str, Any] = step
    return found


def test_push_only_on_success() -> None:
    condition = step_named('build', 'push the state')['if']
    assert 'success()' in condition
    assert "github.event_name != 'pull_request'" in condition
    assert "inputs.state-registry != ''" in condition


def test_publish_runs_after_failed_builds() -> None:
    condition = jobs()['publish']['if']
    assert 'always()' in condition
    assert "needs.plan.result == 'success'" in condition
    assert 'inputs.publish' in condition


def test_builder_checkout_pinned() -> None:
    for name, job in jobs().items():
        checkouts = [
            s.get('with', {})
            for s in job['steps']
            if str(s.get('uses', '')).startswith('actions/checkout@')
        ]
        builder = [c for c in checkouts if c.get('path') == '.distropack-builder']
        assert len(builder) == 1, name
        assert builder[0]['repository'] == '${{ job.workflow_repository }}', name
        assert builder[0]['ref'] == '${{ job.workflow_sha }}', name
        assert all(c.get('persist-credentials') is False for c in checkouts), name


def test_actions_are_pinned() -> None:
    lines = [line for line in WORKFLOW.read_text().splitlines() if re.match(r'\s*-?\s*uses:', line)]
    assert lines
    for line in lines:
        assert re.search(
            r'uses: (\./\.distropack-builder[\w/]*|[\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d)', line
        ), line


def extra(spec: str, base: str = 'extras') -> subprocess.CompletedProcess[str]:
    """action/patterns.sh's extra_options: the tags, a line, then the options, NUL-separated."""
    script = (
        '. action/patterns.sh; extra_options "$1" "$2";'
        ' printf "%s\\0" "${extra_tags[@]}"; printf "\\n"; printf "%s\\0" "${options[@]}"'
    )
    return subprocess.run(
        ['bash', '-euc', script, 'bash', spec, base], cwd=REPO_ROOT, capture_output=True, text=True
    )


def test_extra_options() -> None:
    result = extra('a=t1\nb=t1\n c=t2 ')
    assert result.returncode == 0, result.stderr
    tags, options = result.stdout.split('\n')
    assert tags.split('\0')[:-1] == ['t1', 't2']
    assert options.split('\0')[:-1] == [
        '--extra',
        'a=extras/t1/a',
        '--extra',
        'b=extras/t1/b',
        '--extra',
        'c=extras/t2/c',
    ]


def test_extra_options_empty() -> None:
    result = extra('  \n')
    assert result.returncode == 0, result.stderr
    assert result.stdout == '\0\n\0'  # printf prints its format once, with no argument


def test_extra_options_refuses_a_tag_that_is_no_tag() -> None:
    result = extra('a=../t1')
    assert result.returncode != 0
    assert '../t1' in result.stdout + result.stderr


def test_extra_options_without_equals_fails_naming_it() -> None:
    result = extra('a=t1\nfrozen')
    assert result.returncode != 0
    assert 'frozen' in result.stdout + result.stderr


def test_extra_options_never_expanded() -> None:
    result = extra('$(touch pwned)=t1')
    assert result.returncode == 0, result.stderr
    assert not (REPO_ROOT / 'pwned').exists()
    assert '--extra\0$(touch pwned)=extras/t1/$(touch pwned)' in result.stdout


def test_artifacts_are_per_call() -> None:
    """Two calls in one run (two channels, say) never share an artifact."""
    for name, job in jobs().items():
        for step in job['steps']:
            uses = str(step.get('uses', ''))
            if uses.startswith(('actions/upload-artifact@', 'actions/upload-pages-artifact@')):
                assert '.key }}' in step['with']['name'], (name, step)
    assert 'needs.plan.outputs.key' in jobs()['publish']['outputs']['site-artifact']


def test_project_artifact_is_a_tar_extracted_in_place() -> None:
    for name, job in jobs().items():
        if name == 'prune':
            continue
        (step,) = [s for s in job['steps'] if s.get('name') == 'the project, from its artifact']
        assert step['if'] == "inputs.project-artifact != ''"
        assert 'project.tar' in step['run'] and 'WORKING_DIRECTORY' in step['run']


def test_bake_images_in_every_job_running_containers() -> None:
    for name in ('plan', 'build', 'publish'):
        (step,) = [s for s in jobs()[name]['steps'] if s.get('name') == 'bake the images']
        assert step['if'] == 'inputs.bake-images', name
        assert 'bake_fixture_images.py --releases' in step['run'], name


def test_signing_key_secret_reaches_the_signing_jobs() -> None:
    assert call()['secrets']['signing-key']['required'] is False
    for name in ('build', 'publish'):
        assert jobs()[name]['env']['DISTROPACK_GPG_KEY'] == '${{ secrets.signing-key }}', name
    assert 'DISTROPACK_GPG_KEY' not in load().get('env', {})


def documented_workflows() -> list[tuple[str, dict[Any, Any], str]]:
    """Every complete workflow (`on:` and `jobs:`) in a yaml block of the pages that show one."""
    found = []
    pages = [
        REPO_ROOT / 'docs' / 'how-to' / 'triggers.md',
        REPO_ROOT / 'docs' / 'tutorial' / 'first-project.md',
        REPO_ROOT / 'README.md',
    ]
    for path in pages:
        for n, block in enumerate(
            re.findall(r'^```yaml\n(.*?)^```', path.read_text(), re.M | re.S)
        ):
            data = yaml.safe_load(block)
            if isinstance(data, dict) and True in data and 'jobs' in data:
                found.append((f'{path.name}#{n}', data, block))
    return found


def test_documented_callers_match_the_workflow() -> None:
    callers = 0
    inputs, secrets = set(call()['inputs']), set(call()['secrets'])
    for name, data, _ in documented_workflows():
        for job_name, job in data['jobs'].items():
            uses = job.get('uses', '')
            if uses.startswith('jbigot/distropack-builder/.github/workflows/distropack.yml@'):
                callers += 1
                assert set(job.get('with', {})) <= inputs, (name, job_name)
                assert set(job.get('secrets', {})) <= secrets, (name, job_name)
                assert 'passphrase' in job['secrets'], (name, job_name)
                assert job['permissions'] == {'contents': 'read', 'packages': 'write'}, name
                assert 'channel' in job['with'], (name, job_name)  # one concurrency group each
            for step in job.get('steps', []):
                if str(step.get('uses', '')).startswith('actions/deploy-pages@'):
                    assert step['with']['artifact_name'].endswith('.outputs.site-artifact }}')
                    # a failed release fails the called workflow: the deploy runs all the same
                    assert '!cancelled()' in job['if'], (name, job_name)
    assert callers >= 7  # every trigger of the page, the tutorial's and the README's


def test_documented_callers_lint(tmp_path: Path) -> None:
    actionlint = shutil.which('actionlint')
    if actionlint is None:
        pytest.skip('actionlint is not installed')
    workflows = tmp_path / '.github' / 'workflows'
    workflows.mkdir(parents=True)
    paths = []
    for n, (_, _, block) in enumerate(documented_workflows()):
        paths.append(workflows / f'caller{n}.yml')
        paths[-1].write_text(block)
    result = subprocess.run([actionlint, *map(str, paths)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
