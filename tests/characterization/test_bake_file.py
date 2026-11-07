"""The bake definition of the images, and its equivalence with the docker calls build made."""

import json
import subprocess
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from distro_releases import distribution
from support import DATA, FIXTURE_RELEASES

# The docker calls of `build --publish` (Phase 0), cut into one block per image, each starting
# with its build.
RECORDED_CALLS = DATA / 'build-calls.json'

# The image description: its context directory, its tags and its build arguments.
Image = tuple[str, frozenset[str], dict[str, str]]


@pytest.fixture
def bake(load_script: Callable[[str], ModuleType], frozen: date) -> ModuleType:
    """distropack_builder.bake, reading the recorded release data at the frozen date."""
    return load_script('build')


def everything_exists(image: str) -> bool:
    return True


def test_bake_file_snapshot(bake: ModuleType, snapshot: object) -> None:
    assert bake.bake_definition(Path('/images'), everything_exists) == snapshot


def test_bake_file_matches_the_recorded_build_calls(bake: ModuleType) -> None:
    definition = bake.bake_definition(Path('/images'), everything_exists)
    generated = {
        target['tags'][0]: (
            Path(target['context']).name,
            frozenset(target['tags']),
            target.get('args', {}),
        )
        for target in definition['target'].values()
    }
    recorded = recorded_images(bake.REGISTRY, bake.PROTOCOL)
    assert len(recorded) == 20
    # rpm_sign is new (Phase 6): what remains of rpm_tools and gpg2, rpmsign, on Fedora
    assert {k: v for k, v in generated.items() if '/rpm_sign:' not in k} == recorded
    assert sorted(definition['group']['default']['targets']) == sorted(definition['target'])


def recorded_images(registry: str, protocol: int) -> dict[str, Image]:
    """The images build made, with the intended differences of the bake file applied.

    These are the only differences:
    - the registry, ghcr.io/jbigot/pkg_builder/ before;
    - a tag build left implicit is spelled out as latest;
    - each image gains its protocol tag: <release>-p<N> on a builder, p<N> on a tool;
    - build's --force-rm has no bake equivalent, and building in CI leaves nothing behind anyway;
    - the tool images shrink (Phase 6): deb_tools, whose aptly alone remains, is the aptly image;
      rpm_tools and gpg2 go.
    """
    tools = {'deb_tools': 'aptly'}

    def rename(image: str) -> str:
        image = registry + image.removeprefix('ghcr.io/jbigot/pkg_builder/')
        image = image if ':' in image.rsplit('/', 1)[-1] else image + ':latest'
        repository, tag = image.rsplit(':', 1)
        name = repository.rsplit('/', 1)[-1]
        return f'{registry}{tools.get(name, name)}:{tag}'

    images: dict[str, Image] = {}
    blocks: dict[str, list[list[str]]] = json.loads(RECORDED_CALLS.read_text())
    for block in blocks.values():
        build, *others = block
        if build[-1] in ('rpm_tools', 'gpg2'):
            continue
        assert build[:3] == ['docker', 'build', '--force-rm']
        primary = rename(build[build.index('-t') + 1])
        args = dict(
            build[i + 1].split('=', 1) for i, word in enumerate(build) if word == '--build-arg'
        )
        repository, tag = primary.rsplit(':', 1)
        protocol_tag = f'p{protocol}' if tag == 'latest' else f'{tag}-p{protocol}'
        tags = {primary, f'{repository}:{protocol_tag}'}
        tags |= {rename(cmd[3]) for cmd in others if cmd[1] == 'tag'}
        images[primary] = (tools.get(build[-1], build[-1]), frozenset(tags), args)
    return images


def test_unavailable_base_image_is_left_out(
    bake: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    definition = bake.bake_definition(Path('/images'), lambda image: image != 'ubuntu:tenacious')
    assert 'ubuntu_builder-tenacious' not in definition['target']
    assert 'ubuntu_builder-tenacious' not in definition['group']['default']['targets']
    assert 'ubuntu_builder-stonking' in definition['target']
    assert 'UNAVAILABLE ubuntu:tenacious for ubuntu_builder-tenacious' in capsys.readouterr().err


@pytest.mark.parametrize(
    ('returncode', 'stderr', 'exists'),
    [
        (0, '', True),
        (1, 'ERROR: docker.io/library/debian:trixie-slim: not found', False),
        (1, 'ERROR: ghcr.io/x/y:z: manifest unknown', False),
    ],
)
def test_registry_has(
    bake: ModuleType, monkeypatch: pytest.MonkeyPatch, returncode: int, stderr: str, exists: bool
) -> None:
    commands: list[list[str]] = []

    def run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        commands.append(cmd)
        assert kwargs.get('capture_output') is True
        return SimpleNamespace(returncode=returncode, stderr=stderr)

    monkeypatch.setattr(subprocess, 'run', run)
    assert bake.registry_has('debian:trixie-slim') is exists
    assert commands == [['docker', 'buildx', 'imagetools', 'inspect', 'debian:trixie-slim']]


@pytest.mark.parametrize(
    'stderr',
    [
        'ERROR: failed to do request: Head "https://registry-1.docker.io/v2/": i/o timeout',
        'ERROR: unexpected status from HEAD request: 429 Too Many Requests',
    ],
)
def test_registry_probe_failure_is_an_error(
    bake: ModuleType, monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    """A probe that fails for another reason than a missing image does not read as missing."""
    monkeypatch.setattr(
        subprocess, 'run', lambda cmd, **kwargs: SimpleNamespace(returncode=1, stderr=stderr)
    )
    with pytest.raises(RuntimeError, match='debian:trixie-slim'):
        bake.registry_has('debian:trixie-slim')


def test_runner_pulls_baked_images(
    bake: ModuleType, load_script: Callable[[str], ModuleType]
) -> None:
    """Every image the runner asks for, builders of FIXTURE_RELEASES and tools, is baked."""
    cli = load_script('pkgbuild')
    definition = bake.bake_definition(Path('/images'), everything_exists)
    tags = {tag for target in definition['target'].values() for tag in target['tags']}
    pulled = [
        bake.REGISTRY + cli.builder_image(release)
        for distrib in cli.targets.targets()
        for release in distrib.releases()
        if not release.is_eol()
        if {release.version, release.codename} & set(FIXTURE_RELEASES.get(distrib.id, []))
    ]
    assert len(pulled) == sum(len(releases) for releases in FIXTURE_RELEASES.values())
    pulled += [bake.REGISTRY + bake.tool_image(tool) for tool in bake.TOOLS]
    assert set(pulled) <= tags


def test_bake_file_has_no_rhel(bake: ModuleType) -> None:
    """distro-releases lists RHEL, which is not a build target."""
    definition = bake.bake_definition(Path('/images'), everything_exists)
    assert not [name for name in definition['target'] if name.startswith('rhel')]


def test_runner_asks_protocol_tags(
    bake: ModuleType, load_script: Callable[[str], ModuleType], frozen: date
) -> None:
    """The runner asks the tags of the protocol it drives, never the moving ones."""
    cli = load_script('pkgbuild')
    (trixie,) = (r for r in distribution('debian').releases() if r.codename == 'trixie')
    (f44,) = (r for r in distribution('fedora').releases() if r.version == '44')
    assert cli.builder_image(trixie) == f'debian_builder:trixie-p{bake.PROTOCOL}'
    assert cli.builder_image(f44) == f'fedora_builder:44-p{bake.PROTOCOL}'
    assert bake.tool_image('aptly') == f'aptly:p{bake.PROTOCOL}'
