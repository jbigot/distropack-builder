# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The tests "Why distropack-builder" cites as the evidence of its goals exist."""

import ast
import re
import subprocess

from support import REPO_ROOT

PAGE = REPO_ROOT / 'docs' / 'explanation' / 'why.md'
# {source}`path` or {source}`title <path>`
SOURCE = re.compile(r'\{source\}`(?:([^`<]*?)\s*<([^`>]+)>|([^`]+))`')


def links(text: str) -> list[tuple[str | None, str]]:
    """The (title, path) of every {source} link of text."""
    return [(m[1], m[2]) if m[2] else (None, m[3]) for m in SOURCE.finditer(text)]


def goals(text: str) -> dict[int, str]:
    """The text of each numbered goal section (`## 1. …`)."""
    parts = re.split(r'^## (\d+)\. ', text, flags=re.MULTILINE)
    return {int(number): body for number, body in zip(parts[1::2], parts[2::2], strict=True)}


def test_linked_files_and_functions_exist() -> None:
    tracked = set(
        subprocess.run(
            ['git', 'ls-files'], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.split()
    )
    found = links(PAGE.read_text())
    assert found, 'the page links no test'
    for title, path in found:
        assert path in tracked, path
        if title is not None and title.isidentifier():
            tree = ast.parse((REPO_ROOT / path).read_text())
            names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
            assert title in names, f'{title} is not a function of {path}'


def test_each_tested_goal_cites_a_test() -> None:
    sections = goals(PAGE.read_text())
    assert set(sections) == {1, 2, 3, 4, 5, 6}
    for number in (1, 2, 3, 4):
        assert links(sections[number]), f'goal {number} cites no test'


def test_link_pattern() -> None:
    assert links('{source}`a.py` and {source}`test_x <b/c.py>`') == [
        (None, 'a.py'),
        ('test_x', 'b/c.py'),
    ]
