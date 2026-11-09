# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The sub-commands of distropack-build: run, which builds, and images."""

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

from distropack_builder.bake import PROTOCOL
from distropack_builder.config import BuildConfig
from distropack_builder.selection import configured_releases, disabled_releases


@pytest.fixture
def main(
    load_script: Callable[[str], ModuleType], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[..., Result]:
    """The CLI, invoked with the given arguments from an empty directory; its outcome."""
    cli = load_script('pkgbuild')
    # Without a sub-command, a parser falling back to build.conf fails on the missing file
    # instead of building anything.
    monkeypatch.chdir(tmp_path)

    def call(*args: str) -> Result:
        return CliRunner().invoke(cli.make_app(), list(args), env={'COLUMNS': '200'})

    return call


def test_no_command_is_a_usage_error(main: Callable[..., Result]) -> None:
    result = main()
    assert result.exit_code == 2
    assert 'run' in result.output and 'images' in result.output


def test_run_takes_the_build_options(main: Callable[..., Result]) -> None:
    result = main('run', '--help')
    assert result.exit_code == 0
    out = result.stdout
    assert 'Usage: distropack-build run ' in out
    for option in ('--passphrase-file', '--distributions', '--jobs', 'BUILD_CONF'):
        assert option in out


def test_build_conf_alone_is_no_longer_a_command(main: Callable[..., Result]) -> None:
    result = main('build.conf')
    assert result.exit_code == 2


def test_bake_file_command(
    main: Callable[..., Result],
    load_script: Callable[[str], ModuleType],
    frozen: date,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bake = load_script('build')
    monkeypatch.setattr(bake, 'registry_has', lambda image: True)
    assert main('images', 'bake-file', '-o', str(tmp_path / 'b.json')).exit_code == 0
    definition = json.loads((tmp_path / 'b.json').read_text())
    # bookworm, in free extended support, is new (Phase 10a)
    assert len(definition['target']) == 22
    for target in definition['target'].values():
        assert (Path(target['context']) / 'Dockerfile').is_file()


def test_rhel_is_not_a_target(main: Callable[..., Result], tmp_path: Path) -> None:
    (tmp_path / 'build.conf').write_text(
        'distribs:\n  rhel:\n    versions: [supported]\npackages: {}\n'
    )
    result = main('run', 'build.conf')
    assert result.exit_code == 2
    assert (
        'rhel is not a build target; the targets are almalinux, debian, fedora, rocky, ubuntu'
        in result.stderr
    )


BODHI = 'https://bodhi.fedoraproject.org/releases/?exclude_archived=False&rows_per_page=100&page=1'
TWO_FAMILIES = {
    'distribs': {'debian': {'versions': ['supported']}, 'fedora': {'versions': ['supported']}},
    'packages': {'p': {'disable': {'fedora': ['rawhide']}}, 'q': None},
}
# The same, as the configuration models read it.
TWO_FAMILIES_CONFIG = BuildConfig.model_validate({**TWO_FAMILIES, 'root': '/project'})


def test_source_down_spares_other_distributions(
    recorded_http: respx.MockRouter, frozen: date
) -> None:
    """-D debian reads Debian's source alone: Bodhi down changes nothing."""
    recorded_http.routes[BODHI].return_value = httpx.Response(503)
    releases = configured_releases(TWO_FAMILIES_CONFIG, {'debian'}, frozen, lambda r: True)
    assert {d: sorted(r.uid for r in rs) for d, rs in releases.items()} == {
        'debian': ['debian-13-trixie'],
        'fedora': [],
    }
    assert disabled_releases(TWO_FAMILIES_CONFIG, releases, frozen) == {'p': set(), 'q': set()}
    assert recorded_http.routes[BODHI].call_count == 0


def test_disabled_releases(recorded_http: respx.MockRouter, frozen: date) -> None:
    releases = configured_releases(TWO_FAMILIES_CONFIG, set(), frozen, lambda r: True)
    disabled = disabled_releases(TWO_FAMILIES_CONFIG, releases, frozen)
    assert {p: sorted(r.uid for r in rs) for p, rs in disabled.items()} == {
        'p': ['fedora-46'],
        'q': [],
    }


def write_conf(tmp_path: Path, conf: dict[str, Any]) -> None:
    """build.conf in tmp_path, beside a directory with debian/ for each of its packages."""
    (tmp_path / 'build.conf').write_text(json.dumps(conf))
    for name in conf['packages']:
        (tmp_path / name / 'debian').mkdir(parents=True)


def test_source_down_exits_1_before_any_work(
    main: Callable[..., Result],
    recorded_http: respx.MockRouter,
    tmp_path: Path,
) -> None:
    recorded_http.routes[BODHI].return_value = httpx.Response(503)
    write_conf(tmp_path, TWO_FAMILIES)
    result = main('run', 'build.conf')
    assert result.exit_code == 1
    assert BODHI in result.stderr


def test_disable_naming_a_non_target_exits_2(main: Callable[..., Result], tmp_path: Path) -> None:
    write_conf(
        tmp_path,
        {
            'distribs': {'debian': {'versions': ['supported']}},
            'packages': {'p': {'disable': {'rhel': ['9']}}},
        },
    )
    result = main('run', 'build.conf')
    assert result.exit_code == 2
    assert 'rhel is not a build target' in result.stderr


@pytest.mark.parametrize('how', ['option', 'environment'])
def test_docker_host_option(
    main: Callable[..., Result],
    tmp_path: Path,
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
    result = main(*args)
    assert result.exit_code == 2
    assert 'tcp://x:2375 is not a local engine' in result.stderr


def test_registry_error_exits_1_before_any_work(
    main: Callable[..., Result],
    recorded_http: respx.MockRouter,
    unpublished: set[str],
    tmp_path: Path,
) -> None:
    """A registry that does not answer stops the run: no release is dropped silently."""
    unpublished.add('ERROR')
    write_conf(
        tmp_path, {'distribs': {'debian': {'versions': ['trixie', 'available']}}, 'packages': {}}
    )
    result = main('run', 'build.conf')
    assert result.exit_code == 1
    assert f'debian_builder:trixie-p{PROTOCOL}' in result.stderr
