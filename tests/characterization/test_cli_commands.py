"""The sub-commands of distropack-build: run, which builds, and images."""

import json
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import respx


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


BODHI = 'https://bodhi.fedoraproject.org/releases/?exclude_archived=False&rows_per_page=100&page=1'
TWO_FAMILIES = {
    'distribs': {'debian': {'versions': ['supported']}, 'fedora': {'versions': ['supported']}},
    'packages': {'p': {'disable': {'fedora': ['rawhide']}}, 'q': 'x'},
}


def test_source_down_spares_other_distributions(
    load_script: Callable[[str], ModuleType], recorded_http: respx.MockRouter, frozen: date
) -> None:
    """-D debian reads Debian's source alone: Bodhi down changes nothing."""
    recorded_http.routes[BODHI].return_value = httpx.Response(503)
    cli = load_script('pkgbuild')
    releases = cli.configured_releases(TWO_FAMILIES, {'debian'}, frozen)
    assert {d: sorted(r.uid for r in rs) for d, rs in releases.items()} == {
        'debian': ['debian-13-trixie'],
        'fedora': [],
    }
    assert cli.disabled_releases(TWO_FAMILIES, releases, frozen) == {'p': set(), 'q': set()}
    assert recorded_http.routes[BODHI].call_count == 0


def test_disabled_releases(
    load_script: Callable[[str], ModuleType], recorded_http: respx.MockRouter, frozen: date
) -> None:
    cli = load_script('pkgbuild')
    releases = cli.configured_releases(TWO_FAMILIES, set(), frozen)
    disabled = cli.disabled_releases(TWO_FAMILIES, releases, frozen)
    assert {p: sorted(r.uid for r in rs) for p, rs in disabled.items()} == {
        'p': ['fedora-46'],
        'q': [],
    }


def write_conf(tmp_path: Path, conf: dict[str, object]) -> None:
    (tmp_path / 'build.conf').write_text(json.dumps(conf))


def test_source_down_exits_1_before_any_work(
    main: Callable[..., None],
    recorded_http: respx.MockRouter,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorded_http.routes[BODHI].return_value = httpx.Response(503)
    write_conf(tmp_path, TWO_FAMILIES)
    with pytest.raises(SystemExit) as exit:
        main('run', 'build.conf')
    assert exit.value.code == 1
    assert BODHI in capsys.readouterr().err


def test_disable_naming_a_non_target_exits_2(
    main: Callable[..., None], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_conf(
        tmp_path,
        {
            'distribs': {'debian': {'versions': ['supported']}},
            'packages': {'p': {'disable': {'rhel': ['9']}}},
        },
    )
    with pytest.raises(SystemExit) as exit:
        main('run', 'build.conf')
    assert exit.value.code == 2
    assert 'rhel is not a build target' in capsys.readouterr().err


@pytest.mark.parametrize('how', ['option', 'environment'])
def test_docker_host_option(
    main: Callable[..., None],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    how: str,
) -> None:
    """A remote endpoint is refused before any work, given by option or by environment."""
    write_conf(tmp_path, {'distribs': {'debian': {'versions': ['supported']}}, 'packages': {}})
    if how == 'option':
        args = ['run', '--docker-host', 'tcp://x:2375', 'build.conf']
    else:
        monkeypatch.setenv('DISTROPACK_DOCKER_HOST', 'tcp://x:2375')
        args = ['run', 'build.conf']
    with pytest.raises(SystemExit) as exit:
        main(*args)
    assert exit.value.code == 2
    assert 'tcp://x:2375 is not a local engine' in capsys.readouterr().err
