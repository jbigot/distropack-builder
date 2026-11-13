# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Bake the images the golden and end-to-end tests run, from this checkout, into the local engine.

The images the registry publishes are those of `main`: a branch that changes an image would
otherwise be tested against the previous one. The runner asks for protocol tags, which a local
image answers without any pull.

    uv run python tests/bake_fixture_images.py [--releases [DISTRIB:RELEASE ...]]

`--releases` bakes the tools and the builders of those releases only (none: the tools only), as
each job of the reusable workflow needs with its `bake-images` input.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from importlib.resources import files
from pathlib import Path

from distropack_builder import bake
from support import FIXTURE_RELEASES


def fixture_targets(
    definition: dict[str, object], wanted_releases: dict[str, list[str]] | None = None
) -> list[str]:
    """The targets of the tools and of the builders of wanted_releases (FIXTURE_RELEASES's)."""
    names = set(definition['target'])  # type: ignore[call-overload]
    wanted = list(bake.TOOLS)
    chosen = FIXTURE_RELEASES if wanted_releases is None else wanted_releases
    for dist, releases in chosen.items():
        for release in releases:
            name = bake.target_name(dist, release)
            if name not in names:
                raise SystemExit(f'no bake target {name}: is its base image published?')
            wanted.append(name)
    return wanted


def main() -> None:
    """Write the bake definition and bake the fixture's targets with --load."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--releases', nargs='*', metavar='DISTRIB:RELEASE')
    arguments = parser.parse_args()
    wanted: dict[str, list[str]] | None = None
    if arguments.releases is not None:
        wanted = {}
        for value in arguments.releases:
            dist, _, release = value.partition(':')
            wanted.setdefault(dist, []).append(release)
    images = Path(str(files('distropack_builder') / 'images'))
    definition = bake.bake_definition(images, bake.registry_has)
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / 'bake.json'
        path.write_text(json.dumps(definition))
        command = ['docker', 'buildx', 'bake', '-f', str(path), '--load']
        command += [f'--allow=fs.read={images}', *fixture_targets(definition, wanted)]
        sys.exit(subprocess.run(command).returncode)


if __name__ == '__main__':
    main()
