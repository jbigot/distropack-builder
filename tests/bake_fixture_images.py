# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Bake the images the golden and end-to-end tests run, from this checkout, into the local engine.

The images the registry publishes are those of `main`: a branch that changes an image would
otherwise be tested against the previous one. The runner asks for protocol tags, which a local
image answers without any pull.

    uv run python tests/bake_fixture_images.py
"""

import json
import subprocess
import sys
import tempfile
from importlib.resources import files
from pathlib import Path

from distropack_builder import bake
from support import FIXTURE_RELEASES


def fixture_targets(definition: dict[str, object]) -> list[str]:
    """The targets of the tools and of the builders of FIXTURE_RELEASES."""
    names = set(definition['target'])  # type: ignore[call-overload]
    wanted = list(bake.TOOLS)
    for dist, releases in FIXTURE_RELEASES.items():
        for release in releases:
            name = bake.target_name(dist, release)
            if name not in names:
                raise SystemExit(f'no bake target {name}: is its base image published?')
            wanted.append(name)
    return wanted


def main() -> None:
    """Write the bake definition and bake the fixture's targets with --load."""
    images = Path(str(files('distropack_builder') / 'images'))
    definition = bake.bake_definition(images, bake.registry_has)
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / 'bake.json'
        path.write_text(json.dumps(definition))
        command = ['docker', 'buildx', 'bake', '-f', str(path), '--load']
        command += [f'--allow=fs.read={images}', *fixture_targets(definition)]
        sys.exit(subprocess.run(command).returncode)


if __name__ == '__main__':
    main()
