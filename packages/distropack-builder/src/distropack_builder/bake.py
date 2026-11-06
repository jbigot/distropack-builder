"""The Docker bake definition of the images the builder runs.

The definition is the one place where the images get their names and tags. A builder image is
tagged with its release (the codename in the Debian family, the version elsewhere), with that
release's protocol tag, and with its codename, suite and `latest` where they apply. The protocol
version covers what the runner relies on in an image, its entrypoint and its volumes: it is bumped
when they change, so that a runner asks for an image it can drive.
"""

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

REGISTRY: Final = 'ghcr.io/jbigot/distropack-builder/'
PROTOCOL: Final = 1
TOOLS: Final = ('rpm_tools', 'deb_tools', 'gpg2')


def registry_has(image: str) -> bool:
    """Whether the registry publishes image (a reference such as `debian:trixie-slim`)."""
    result = subprocess.run(
        ['docker', 'buildx', 'imagetools', 'inspect', image], capture_output=True
    )
    return result.returncode == 0


def bake_definition(images_dir: Path, base_image_exists: Callable[[str], bool]) -> dict[str, Any]:
    """The bake definition of every image, as a JSON-ready dictionary.

    Args:
        images_dir: the directory holding one build context per image.
        base_image_exists: whether the registry publishes a base image. A release whose base image
            is missing gets no target: a release can open before its registry publishes an image,
            and that is not a failure.

    Returns:
        The definition, with one target per image and every target in the `default` group.
    """
    # Imported here: distinfo fetches the release data when it is imported.
    from distro_releases.distinfo import distributions

    targets: dict[str, dict[str, Any]] = {}
    for tool in TOOLS:
        targets[tool] = {
            'context': str(images_dir / tool),
            'tags': [f'{REGISTRY}{tool}:latest', f'{REGISTRY}{tool}:p{PROTOCOL}'],
        }
    for distrib in distributions():
        supported = list(distrib.releases(supported=True))
        for release in distrib.releases(eoled=False):
            release_tag = release.id
            builder = 'rpm_builder'
            if 'debian' in distrib.id_like:
                release_tag = release.codename
                builder = 'deb_builder'
            docker_release = release_tag
            if distrib.id == 'debian' and release.codename != 'rc-buggy':
                docker_release += '-slim'
            name = f'{distrib.id}_builder-{release_tag}'
            base_image = f'{distrib.docker_image}:{docker_release}'
            if not base_image_exists(base_image):
                print(f'UNAVAILABLE {base_image} for {name}', file=sys.stderr)
                continue
            repository = f'{REGISTRY}{distrib.id}_builder'
            tags = [f'{repository}:{release_tag}', f'{repository}:{release_tag}-p{PROTOCOL}']
            if release.codename is not None and release_tag != release.codename:
                tags.append(f'{repository}:{release.codename}')
            if release.suite is not None:
                tags.append(f'{repository}:{release.suite}')
            supported_now = release.supported()  # type: ignore[no-untyped-call]
            if supported_now and not any(release < r for r in supported):
                tags.append(f'{repository}:latest')
            targets[name] = {
                'context': str(images_dir / builder),
                'tags': tags,
                'args': {
                    'DIST_ID': distrib.id,
                    'DIST_IMAGE': distrib.docker_image,
                    'DIST_RELEASE': docker_release,
                    'DIST_CODENAME': str(release.codename),
                },
            }
    return {
        'group': {'default': {'targets': sorted(targets)}},
        'target': dict(sorted(targets.items())),
    }
