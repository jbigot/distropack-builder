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
  from no state. Any other failure, an unreachable registry or a refused token, exits 1 without
  writing anything: a job must never build from an empty state it took for an absent one, which
  would restart the rebuild numbers.
- `push` uploads the layer, then moves the tag: an interrupted push leaves the previous state under
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

## Prune the untagged versions

```sh
distropack-build state prune --registry ghcr.io/example/pkgs/state --keep-untagged 50
```

deletes the untagged versions beyond the 50 most recent, never a tagged one. It uses GitHub's
Packages API, as the registry protocol cannot list untagged versions: it works on `ghcr.io` only,
with a token allowed to delete the package's versions (`--token`, `$DISTROPACK_REGISTRY_TOKEN`, or
`$GITHUB_TOKEN`).
