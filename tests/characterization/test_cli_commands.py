"""The sub-commands of distropack-build: run, which builds, and images."""

import sys
from collections.abc import Callable
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
