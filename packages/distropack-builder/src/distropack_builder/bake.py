# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The Docker bake definition of the images the builder runs.

The definition is the one place where the images get their names and tags. A builder image is
tagged with its release (the codename in the Debian family, the version elsewhere), with that
release's protocol tag, and with its codename, suite and `latest` where they apply. The protocol
version covers what the runner relies on in an image, its entrypoint and its volumes: it is bumped
when they change, so that a runner asks for an image it can drive.
"""

import re
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Final

from distropack_builder import targets

REGISTRY: Final = 'ghcr.io/jbigot/distropack-builder/'
PROTOCOL: Final = 2
# The Debian tools: aptly, and uscan with mk-origtargz. One image, not two: compressed, it is
# smaller than the base plus both tools' own layers (101.1 MB against 104.0 MB, Phase 10a).
DEB_TOOLS: Final = 'deb_tools'
TOOLS: Final = (DEB_TOOLS, 'rpm_sign')
# What `imagetools inspect` reports for a missing image: `not found` from Docker Hub, `manifest
# unknown` from registries that use the distribution specification's error code.
MISSING_IMAGE: Final = re.compile(r'not found|manifest unknown', re.IGNORECASE)


def tool_image(tool: str) -> str:
    """The image of a tool, relative to REGISTRY, at the protocol the runner drives."""
    return f'{tool}:p{PROTOCOL}'


def registry_has(image: str) -> bool:
    """Whether the registry publishes image (a reference such as `debian:trixie-slim`).

    Raises:
        RuntimeError: the registry could not tell, for another reason than a missing image (a
            network error, a rate limit): reading that as a missing image would silently drop
            the release from the images.
        subprocess.TimeoutExpired: the registry did not answer within a minute.
    """
    result = subprocess.run(
        ['docker', 'buildx', 'imagetools', 'inspect', image],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode == 0:
        return True
    if MISSING_IMAGE.search(result.stderr):
        return False
    raise RuntimeError(f'cannot probe {image}: {result.stderr.strip()}')


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
    on = date.today()
    entries: dict[str, dict[str, Any]] = {}
    for tool in TOOLS:
        entries[tool] = {
            'context': str(images_dir / tool),
            'tags': [f'{REGISTRY}{tool}:latest', f'{REGISTRY}{tool}:p{PROTOCOL}'],
        }
    for distrib in targets.targets():
        releases = distrib.releases(on)
        supported = [r for r in releases if r.is_supported(on)]
        # every release a project may name: not at its end of life yet (unreleased ones
        # included), or past it but still maintained for free (Debian's LTS)
        for release in (r for r in releases if not r.is_eol(on) or r.is_maintained(on)):
            release_tag = str(release.version)
            builder = 'rpm_builder'
            if targets.is_deb(distrib):
                release_tag = str(release.codename)
                builder = 'deb_builder'
            target = targets.TARGETS[distrib.id]
            base_image = target.base_image(release_tag)
            name = f'{distrib.id}_builder-{release_tag}'
            if not base_image_exists(base_image):
                print(f'UNAVAILABLE {base_image} for {name}', file=sys.stderr)
                continue
            repository = f'{REGISTRY}{distrib.id}_builder'
            tags = [f'{repository}:{release_tag}', f'{repository}:{release_tag}-p{PROTOCOL}']
            if release.codename is not None and release_tag != release.codename:
                tags.append(f'{repository}:{release.codename}')
            if release.suite is not None:
                tags.append(f'{repository}:{release.suite}')
            if release.is_supported(on) and not any(release < r for r in supported):
                tags.append(f'{repository}:latest')
            entries[name] = {
                'context': str(images_dir / builder),
                'tags': tags,
                'args': {
                    'DIST_ID': distrib.id,
                    'DIST_IMAGE': target.repository,
                    'DIST_RELEASE': base_image.rpartition(':')[2],
                    'DIST_CODENAME': str(release.codename),
                },
            }
    return {
        'group': {'default': {'targets': sorted(entries)}},
        'target': dict(sorted(entries.items())),
    }
