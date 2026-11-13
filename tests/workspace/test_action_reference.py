# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The reference of the actions, generated from their action.yml, documents all of it."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from support import REPO_ROOT

ACTIONS = [REPO_ROOT / 'action.yml', REPO_ROOT / 'plan' / 'action.yml']


def generator() -> ModuleType:
    """docs/action_reference.py, which docs/conf.py imports from its own directory."""
    spec = importlib.util.spec_from_file_location(
        'action_reference', REPO_ROOT / 'docs' / 'action_reference.py'
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def one_line(text: str) -> str:
    return ' '.join(text.split())


@pytest.mark.parametrize('path', ACTIONS, ids=['build', 'plan'])
def test_every_input_and_output_is_documented(path: Path) -> None:
    action = yaml.safe_load(path.read_text())
    page = generator().action_reference(path)
    assert one_line(action['description']) in page
    for name, spec in action['inputs'].items():
        row = next(line for line in page.splitlines() if line.startswith(f'| `{name}` |'))
        assert one_line(spec['description']) in row
        if spec['default']:
            assert f'`{spec["default"]}`' in row
    for name, spec in action['outputs'].items():
        row = next(line for line in page.splitlines() if line.startswith(f'| `{name}` |'))
        assert one_line(spec['description']) in row


def test_input_without_default(tmp_path: Path) -> None:
    path = tmp_path / 'action.yml'
    path.write_text(
        'name: x\ndescription: An action.\n'
        'inputs: {a: {description: The input., default: ""}}\n'
        'outputs: {b: {description: The output., value: x}}\n'
        'runs: {using: composite, steps: []}\n'
    )
    page = generator().action_reference(path)
    assert '| `a` | — | The input. |' in page
    assert '| `b` | The output. |' in page


def test_every_workflow_input_secret_and_output_is_documented() -> None:
    path = REPO_ROOT / '.github' / 'workflows' / 'distropack.yml'
    call = yaml.safe_load(path.read_text())[True]['workflow_call']
    page = generator().workflow_reference(path)
    for section in ('inputs', 'secrets', 'outputs'):
        for name, spec in call[section].items():
            row = next(line for line in page.splitlines() if line.startswith(f'| `{name}` |'))
            assert one_line(spec['description']) in row, name
    row = next(line for line in page.splitlines() if line.startswith('| `keep-untagged` |'))
    assert '| number | `50` |' in row
    row = next(line for line in page.splitlines() if line.startswith('| `publish` |'))
    assert '| boolean | `true` |' in row
    row = next(line for line in page.splitlines() if line.startswith('| `passphrase` |'))
    assert '| required |' in row
