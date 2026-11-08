# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Write the fixture project, and the upstream tarballs it downloads, for the action workflow.

    uv run python tests/fixture_project.py DEST BASE_URL \
        --releases debian:trixie fedora:44 --packages hello-lib hello-app

writes DEST/project, whose build.conf downloads the upstream tarballs from BASE_URL, and
DEST/served, the tarballs to serve there (`python -m http.server -d DEST/served`).
"""

import argparse
from pathlib import Path

from support import FIXTURE_PROJECT, make_project, write_tarball


def main() -> None:
    """Write the project and the tarballs the command line asks for."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('dest', type=Path)
    parser.add_argument('base_url')
    parser.add_argument('--releases', nargs='+', required=True, metavar='DISTRIB:RELEASE')
    parser.add_argument('--packages', nargs='+', required=True, metavar='PACKAGE')
    args = parser.parse_args()
    versions: dict[str, list[str]] = {}
    for release in args.releases:
        distrib, _, name = release.partition(':')
        versions.setdefault(distrib, []).append(name)
    make_project(args.dest / 'project', args.base_url, versions, args.packages)
    served = args.dest / 'served'
    served.mkdir(parents=True)
    for upstream in (FIXTURE_PROJECT / 'upstream').iterdir():
        write_tarball(upstream, served / f'{upstream.name}.tar.gz')


if __name__ == '__main__':
    main()
