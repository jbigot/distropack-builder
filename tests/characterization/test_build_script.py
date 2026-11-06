"""Record the docker calls of the image build script: Phase 2's bake file must produce the same."""

import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.mark.parametrize('argv', [[], ['--publish']], ids=['build', 'publish'])
def test_docker_calls(load_script: Callable[[str], ModuleType], frozen: date,
                      monkeypatch: pytest.MonkeyPatch, argv: list[str], snapshot: object,
                      tmp_path: Path) -> None:
    build = load_script('build')
    calls: list[list[str]] = []

    def run(cmd: list[str]) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(build, 'run', run)
    monkeypatch.setattr(sys, 'argv', ['build', *argv])
    monkeypatch.chdir(tmp_path)  # build changes into the images directory; restored afterwards
    build.main()
    assert by_image(calls) == snapshot


def by_image(calls: list[list[str]]) -> dict[str, list[list[str]]]:
    """The calls cut into one block per image, each starting with its build.

    distributions() iterates a set, so the order of the distributions changes from one process to
    the next: only the order inside a block (build, then tags and pushes) is behaviour.
    """
    blocks: dict[str, list[list[str]]] = {}
    for cmd in calls:
        if cmd[1] == 'build':
            image = cmd[cmd.index('-t') + 1]
            assert image not in blocks, f'{image} built twice'
            blocks[image] = []
        blocks[image].append(cmd)
    return dict(sorted(blocks.items()))
