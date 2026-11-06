"""Record the docker calls of the image build script: Phase 2's bake file must produce the same."""

import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.mark.parametrize('argv', [[], ['--publish']], ids=['build', 'publish'])
def test_docker_calls(
    load_script: Callable[[str], ModuleType],
    frozen: date,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    snapshot: object,
    tmp_path: Path,
) -> None:
    build = load_script('build')
    calls: list[list[str]] = []

    def run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        if cmd[1:4] != ['buildx', 'imagetools', 'inspect']:  # the base image probes are not builds
            calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build, 'run', run)
    monkeypatch.setattr(sys, 'argv', ['build', *argv])
    monkeypatch.chdir(tmp_path)  # build changes into the images directory; restored afterwards
    build.main()
    assert by_image(calls) == snapshot


def test_unavailable_base_image_is_not_built(
    load_script: Callable[[str], ModuleType],
    frozen: date,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    build = load_script('build')
    builds: list[str] = []
    probed: list[str] = []

    def run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        if cmd[1:4] == ['buildx', 'imagetools', 'inspect']:
            probed.append(cmd[4])
            return SimpleNamespace(returncode=1 if cmd[4] == 'ubuntu:tenacious' else 0)
        if cmd[1] == 'build':
            builds.append(cmd[cmd.index('-t') + 1])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build, 'run', run)
    monkeypatch.setattr(sys, 'argv', ['build'])
    monkeypatch.chdir(tmp_path)
    build.main()
    out = capsys.readouterr().out
    assert 'ubuntu:tenacious' in probed and 'debian:forky-slim' in probed
    assert 'ghcr.io/jbigot/pkg_builder/ubuntu_builder:tenacious' not in builds
    assert 'ghcr.io/jbigot/pkg_builder/ubuntu_builder:stonking' in builds
    assert (
        '===> UNAVAILABLE ubuntu:tenacious for ghcr.io/jbigot/pkg_builder/ubuntu_builder:tenacious'
        in out
    )
    assert 'SKIPPED' not in out


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
