<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Keep the states in a registry

A release's state ([What a release keeps between runs](../explanation/state.md)) must outlive the
CI job that built it. `distropack-build state` keeps each one in an OCI registry, such as GitHub's
`ghcr.io`, as one artifact per channel and release, tagged `<channel>.<release uid>`
(`stable.debian-13-trixie`):

```sh
distropack-build state pull --registry ghcr.io/example/pkgs/state --channel stable \
    --release debian-13-trixie --release fedora-44 --state-dir states
distropack-build run --channel stable --state-dir states
distropack-build state push --registry ghcr.io/example/pkgs/state --channel stable \
    --release debian-13-trixie --state-dir states
```

- `pull` prints `no state for <tag>` for a tag the registry does not have, and the release starts
  from no state. Any other failure, an unreachable registry or a refused token, exits 1, and the
  release being pulled keeps its local state (the releases pulled before it have the registry's):
  a job must never build from an empty state it took for an absent one, which would restart the
  rebuild numbers. A layer is checked against its digest before it replaces a state.
- `push` refuses a state that is not there, or not whole (`no state for <tag>`, exit 1), so that a
  failed or mistyped job never replaces a tag's state with an empty one. It uploads the layer,
  then moves the tag: an interrupted push leaves the previous state under
  the tag. Under GitHub Actions it annotates the artifact with the repository
  (`org.opencontainers.image.source`, which links the package to it and grants the workflow's token
  access), the commit and the run id; `--annotation KEY=VALUE` adds others.
- The credentials are Docker's (`docker login`, `docker/login-action`), or `--token` (or
  `$DISTROPACK_REGISTRY_TOKEN`); `--insecure` talks plain HTTP to a local registry.

## Roll a tag back

Each push leaves the previous artifact as an untagged version. To restore one, find its digest (on
the package's page of GitHub, or with `oras manifest fetch`) and tag it:

```sh
distropack-build state tag --registry ghcr.io/example/pkgs/state sha256:<digest> stable.debian-13-trixie
```

## Keep a whole tree

A site can also carry trees no build makes, copied as they are (`distropack-build site --extra`):
repositories frozen when they moved, say. The registry keeps them too, one artifact per tree, under
a tag of your choice:

```sh
distropack-build state push-tree --registry ghcr.io/example/pkgs/state legacy-main frozen/
distropack-build state pull-tree --registry ghcr.io/example/pkgs/state legacy-main frozen/
distropack-build site --state-dir states --output site \
    --extra main.debian=frozen/main.debian --extra main.ubuntu=frozen/main.ubuntu build.conf
```

`pull-tree` replaces the directory whole, and fails when the registry has no such tag: unlike a
state, a tree is pulled because something needs it. Neither command accepts the other kind of
artifact, so a tree can never be taken for a state. The tree loses its group and other write bits,
as a served site does not need them.

## Prune the untagged versions

```sh
distropack-build state prune --registry ghcr.io/example/pkgs/state --keep-untagged 50
```

deletes the untagged versions beyond the 50 most recent, never a tagged one. It uses GitHub's
Packages API, as the registry protocol cannot list untagged versions: it works on `ghcr.io` only,
with a token allowed to delete the package's versions (`--token`, `$DISTROPACK_REGISTRY_TOKEN`, or
`$GITHUB_TOKEN`).
