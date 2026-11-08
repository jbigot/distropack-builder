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
    lines += ['', '### Outputs', '', '| Output | Description |', '|---|---|']
    for name, spec in action.get('outputs', {}).items():
        lines.append(f'| `{name}` | {_cell(spec["description"])} |')
    return '\n'.join(lines) + '\n'
