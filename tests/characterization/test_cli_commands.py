"""The sub-commands of distropack-build: run, which builds, and images."""

import json
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def main(
    load_script: Callable[[str], ModuleType], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[..., None]:
    """The CLI's main, called with the given arguments from an empty directory."""
    cli = load_script('pkgbuild')
    # Without a sub-command, a parser falling back to build.conf fails on the missing file
    # instead of building anything.
    monkeypatch.chdir(tmp_path)

    def call(*args: str) -> None:
        monkeypatch.setattr(sys, 'argv', ['distropack-build', *args])
        cli.main()

    return call


def test_no_command_is_a_usage_error(
    main: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit:
        main()
    assert exit.value.code == 2
    err = capsys.readouterr().err
    assert 'run' in err and 'images' in err


def test_run_takes_the_build_options(
    main: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit:
        main('run', '--help')
    assert exit.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith('usage: distropack-build run ')
    for option in ('--passphrase', '--distributions', '--jobs', 'build.conf'):
        assert option in out


def test_build_conf_alone_is_no_longer_a_command(main: Callable[..., None]) -> None:
    with pytest.raises(SystemExit) as exit:
        main('build.conf')
    assert exit.value.code == 2


def test_bake_file_command(
    main: Callable[..., None],
    load_script: Callable[[str], ModuleType],
    frozen: date,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bake = load_script('build')
    monkeypatch.setattr(bake, 'registry_has', lambda image: True)
    main('images', 'bake-file', '-o', str(tmp_path / 'b.json'))
    definition = json.loads((tmp_path / 'b.json').read_text())
    assert len(definition['target']) == 22
    for target in definition['target'].values():
        assert (Path(target['context']) / 'Dockerfile').is_file()


def test_rhel_is_not_a_target(
    main: Callable[..., None], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / 'build.conf').write_text(
        'distribs:\n  rhel:\n    versions: [supported]\npackages: {}\n'
    )
    with pytest.raises(SystemExit) as exit:
        main('run', 'build.conf')
    assert exit.value.code == 2
    assert (
        'rhel is not a build target; the targets are almalinux, debian, fedora, rocky, ubuntu'
        in capsys.readouterr().err
    )
