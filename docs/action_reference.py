# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The reference of a GitHub Action, in MyST Markdown, generated from its action.yml."""

from pathlib import Path

import yaml


def _cell(text: str) -> str:
    """The text on one line, fit for a table cell."""
    return ' '.join(text.split()).replace('|', r'\|')


def action_reference(path: Path) -> str:
    """The description of the action in path, then tables of its inputs and outputs."""
    action = yaml.safe_load(path.read_text())
    lines = [_cell(action['description']), '', '### Inputs', '']
    lines += ['| Input | Default | Description |', '|---|---|---|']
    for name, spec in action.get('inputs', {}).items():
        default = spec.get('default', '')
        shown = f'`{_cell(str(default))}`' if default != '' else '—'
        lines.append(f'| `{name}` | {shown} | {_cell(spec["description"])} |')
    if action.get('outputs'):
        lines += ['', '### Outputs', '', '| Output | Description |', '|---|---|']
        for name, spec in action['outputs'].items():
            lines.append(f'| `{name}` | {_cell(spec["description"])} |')
    return '\n'.join(lines) + '\n'


def _shown(value: object) -> str:
    """A default as YAML writes it, in code; a dash for none."""
    if value is None or value == '':
        return '—'
    text = str(value).lower() if isinstance(value, bool) else str(value)
    return f'`{_cell(text)}`'


def workflow_reference(path: Path) -> str:
    """Tables of the inputs, secrets and outputs of the reusable workflow in path."""
    call = yaml.safe_load(path.read_text())[True]['workflow_call']  # YAML reads `on` as True
    lines = ['### Inputs', '', '| Input | Type | Default | Description |', '|---|---|---|---|']
    for name, spec in call.get('inputs', {}).items():
        default = _shown(spec.get('default'))
        lines.append(f'| `{name}` | {spec["type"]} | {default} | {_cell(spec["description"])} |')
    lines += ['', '### Secrets', '', '| Secret | | Description |', '|---|---|---|']
    for name, spec in call.get('secrets', {}).items():
        required = 'required' if spec.get('required') else 'optional'
        lines.append(f'| `{name}` | {required} | {_cell(spec["description"])} |')
    lines += ['', '### Outputs', '', '| Output | Description |', '|---|---|']
    for name, spec in call.get('outputs', {}).items():
        lines.append(f'| `{name}` | {_cell(spec["description"])} |')
    return '\n'.join(lines) + '\n'
