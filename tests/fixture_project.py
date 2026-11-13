# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Write the fixture project, and the upstream tarballs it downloads, for the action workflow.

    uv run python tests/fixture_project.py DEST BASE_URL \
        --releases debian:trixie fedora:44 --packages hello-lib hello-app

writes DEST/project, whose build.conf downloads the upstream tarballs from BASE_URL, and
DEST/served, the tarballs to serve there (`python -m http.server -d DEST/served`).

`--channels RELEASE SNAPSHOT` names the release channel and declares a snapshot channel, in which
hello-snap follows hello-lib's upstream main branch (its release tarball served too);
`--snapshot-commits N` then adds N commits to that branch, listed oldest first in
DEST/commits.json.
"""

import argparse
import json
import shutil
from pathlib import Path

from support import (
    FIXTURE_PROJECT,
    SNAP,
    add_snapshot_package,
    add_upstream_commit,
    make_project,
    use_channels,
    write_tarball,
)


def main() -> None:
    """Write the project and the tarballs the command line asks for."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('dest', type=Path)
    parser.add_argument('base_url')
    parser.add_argument('--releases', nargs='+', required=True, metavar='DISTRIB:RELEASE')
    parser.add_argument('--packages', nargs='+', required=True, metavar='PACKAGE')
    parser.add_argument('--channels', nargs=2, metavar=('RELEASE', 'SNAPSHOT'))
    parser.add_argument('--snapshot-commits', type=int, default=0, metavar='N')
    args = parser.parse_args()
    if args.snapshot_commits and not args.channels:
        parser.error('--snapshot-commits needs --channels')
    versions: dict[str, list[str]] = {}
    for release in args.releases:
        distrib, _, name = release.partition(':')
        versions.setdefault(distrib, []).append(name)
    project = make_project(args.dest / 'project', args.base_url, versions, args.packages)
    served = args.dest / 'served'
    served.mkdir(parents=True)
    for upstream in (FIXTURE_PROJECT / 'upstream').iterdir():
        write_tarball(upstream, served / f'{upstream.name}.tar.gz')
    if args.channels:
        release, snapshot = args.channels
        use_channels(project, release, snapshot)
        add_snapshot_package(project, snapshot, args.base_url, autosetup=True)
        # its release tarball is hello-lib's, under its own name (add_snapshot_package's sources)
        shutil.copyfile(served / 'hello-lib-1.0.tar.gz', served / f'{SNAP}-1.0.tar.gz')
        if args.snapshot_commits:
            bare = project / 'upstream' / 'hello-lib.git'
            commits = [add_upstream_commit(bare, f'c{n}') for n in range(args.snapshot_commits)]
            (args.dest / 'commits.json').write_text(json.dumps(commits) + '\n')


if __name__ == '__main__':
    main()
