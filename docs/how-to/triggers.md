<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Build and publish with the reusable workflow

The reusable workflow `jbigot/distropack-builder/.github/workflows/distropack.yml` does what a
packaging repository's CI needs. It plans the releases, builds each one in a job of its own on the
states a registry keeps (see [Keep the states in a registry](state-registry.md)), then:

- assembles the site with every channel's repositories;
- checks the versions against those already published (see
  [Guard what gets published](publication-guards.md));
- installs the site in clean containers;
- uploads it as a Pages artifact.

The caller decides when it runs, and deploys the artifact. Its inputs, secrets and outputs are in
[the reference](../reference/action.md).

Every example below is a complete workflow of the packaging repository. Pin the workflow by a full
commit hash, as for the actions (see [Use the GitHub Action](github-action.md)).

The registry repository must be lowercase (`ghcr.io/example/pkgs/state`). The passphrase of the
signing key is the secret `PACKAGES_PASSPHRASE`. When the key itself is not committed (it is
protected by that passphrase), the secret `PACKAGES_KEY` holds it, armored, and goes in as
`signing-key`. GitHub Pages must deploy from GitHub Actions (the repository's settings, *Pages*,
*Source*).

## The release channel: on a tag, and every month

A tag rebuilds the release channel; a monthly run rebuilds it too, so that the packages follow the
distributions' updates. A pull request builds without publishing.

```yaml
name: Packages
on:
  push: { tags: ['v*'] }
  pull_request:
  schedule: [{ cron: '27 1 3 * *' }]
permissions: {}
jobs:
  packages:
    permissions: { contents: read, packages: write }
    uses: jbigot/distropack-builder/.github/workflows/distropack.yml@<commit hash> # v1.0.0
    with:
      channel: stable
      state-registry: ghcr.io/example/pkgs/state
      max-site-size: 900M
      publish: ${{ github.event_name != 'pull_request' }}
    secrets:
      passphrase: ${{ secrets.PACKAGES_PASSPHRASE }}
      signing-key: ${{ secrets.PACKAGES_KEY }}
  deploy:
    needs: packages
    # a failed release fails the call, the others are published all the same
    if: ${{ !cancelled() && needs.packages.outputs.site-artifact != '' }}
    runs-on: ubuntu-24.04
    permissions: { pages: write, id-token: write }
    environment: { name: github-pages, url: "${{ steps.deploy.outputs.page_url }}" }
    steps:
    - id: deploy
      uses: actions/deploy-pages@<commit hash> # v5.0.1
      with: { artifact_name: "${{ needs.packages.outputs.site-artifact }}" }
```

Name the channel in every caller, the release channel too: the runs of a channel wait for each
other, in the order they started, keyed on the `channel` input as written. One run of a channel
runs at a time; of those queued, GitHub keeps the latest only. The publications of all channels
wait for each other too, since each publishes every channel.

The `deploy` job is the same in every example below. Two runs that publish within minutes of each
other can deploy in the reverse order, the older site last; the next run deploys the right one.

## A snapshot channel, when upstream pushes

The upstream repository's workflow sends a `repository_dispatch` on each push to its branch (see
[The upstream side](#upstream-side), below); the packaging repository builds the commit it names,
even if the branch moved since. Bursts coalesce: only the latest of the pending runs is kept.

```yaml
name: Nightly packages
on:
  repository_dispatch: { types: [distropack-build] }
permissions: {}
jobs:
  packages:
    permissions: { contents: read, packages: write }
    uses: jbigot/distropack-builder/.github/workflows/distropack.yml@<commit hash> # v1.0.0
    with:
      channel: nightly
      dispatch-payload: ${{ toJSON(github.event.client_payload) }}
      state-registry: ghcr.io/example/pkgs/state
      max-site-size: 900M
    secrets:
      passphrase: ${{ secrets.PACKAGES_PASSPHRASE }}
      signing-key: ${{ secrets.PACKAGES_KEY }}
  deploy:
    needs: packages
    # a failed release fails the call, the others are published all the same
    if: ${{ !cancelled() && needs.packages.outputs.site-artifact != '' }}
    runs-on: ubuntu-24.04
    permissions: { pages: write, id-token: write }
    environment: { name: github-pages, url: "${{ steps.deploy.outputs.page_url }}" }
    steps:
    - id: deploy
      uses: actions/deploy-pages@<commit hash> # v5.0.1
      with: { artifact_name: "${{ needs.packages.outputs.site-artifact }}" }
```

A payload for another channel, or naming a package the channel does not build, fails the run
before anything is built.

## A snapshot channel, every day if something changed

With `build-if: changed`, a run whose packages' commits and packaging are those the states hold
builds nothing (it still publishes, which changes nothing). It also skips the rebuilds a new build
environment would call for, so pair it with a weekly run that always builds, below.

```yaml
name: Daily packages
on:
  schedule: [{ cron: '41 2 * * *' }]
permissions: {}
jobs:
  packages:
    permissions: { contents: read, packages: write }
    uses: jbigot/distropack-builder/.github/workflows/distropack.yml@<commit hash> # v1.0.0
    with:
      channel: nightly
      build-if: changed
      state-registry: ghcr.io/example/pkgs/state
      max-site-size: 900M
    secrets:
      passphrase: ${{ secrets.PACKAGES_PASSPHRASE }}
      signing-key: ${{ secrets.PACKAGES_KEY }}
  deploy:
    needs: packages
    # a failed release fails the call, the others are published all the same
    if: ${{ !cancelled() && needs.packages.outputs.site-artifact != '' }}
    runs-on: ubuntu-24.04
    permissions: { pages: write, id-token: write }
    environment: { name: github-pages, url: "${{ steps.deploy.outputs.page_url }}" }
    steps:
    - id: deploy
      uses: actions/deploy-pages@<commit hash> # v5.0.1
      with: { artifact_name: "${{ needs.packages.outputs.site-artifact }}" }
```

## A snapshot channel, every week

```yaml
name: Weekly packages
on:
  schedule: [{ cron: '17 3 * * 0' }]
permissions: {}
jobs:
  packages:
    permissions: { contents: read, packages: write }
    uses: jbigot/distropack-builder/.github/workflows/distropack.yml@<commit hash> # v1.0.0
    with:
      channel: nightly
      state-registry: ghcr.io/example/pkgs/state
      max-site-size: 900M
    secrets:
      passphrase: ${{ secrets.PACKAGES_PASSPHRASE }}
      signing-key: ${{ secrets.PACKAGES_KEY }}
  deploy:
    needs: packages
    # a failed release fails the call, the others are published all the same
    if: ${{ !cancelled() && needs.packages.outputs.site-artifact != '' }}
    runs-on: ubuntu-24.04
    permissions: { pages: write, id-token: write }
    environment: { name: github-pages, url: "${{ steps.deploy.outputs.page_url }}" }
    steps:
    - id: deploy
      uses: actions/deploy-pages@<commit hash> # v5.0.1
      with: { artifact_name: "${{ needs.packages.outputs.site-artifact }}" }
```

## Any channel, by hand

```yaml
name: Packages on demand
on:
  workflow_dispatch:
    inputs:
      channel: { description: The channel to build, required: true, default: nightly }
permissions: {}
jobs:
  packages:
    permissions: { contents: read, packages: write }
    uses: jbigot/distropack-builder/.github/workflows/distropack.yml@<commit hash> # v1.0.0
    with:
      channel: ${{ inputs.channel }}
      state-registry: ghcr.io/example/pkgs/state
      max-site-size: 900M
    secrets:
      passphrase: ${{ secrets.PACKAGES_PASSPHRASE }}
      signing-key: ${{ secrets.PACKAGES_KEY }}
  deploy:
    needs: packages
    # a failed release fails the call, the others are published all the same
    if: ${{ !cancelled() && needs.packages.outputs.site-artifact != '' }}
    runs-on: ubuntu-24.04
    permissions: { pages: write, id-token: write }
    environment: { name: github-pages, url: "${{ steps.deploy.outputs.page_url }}" }
    steps:
    - id: deploy
      uses: actions/deploy-pages@<commit hash> # v5.0.1
      with: { artifact_name: "${{ needs.packages.outputs.site-artifact }}" }
```

These triggers combine: one workflow may list several of them, as long as each run names the
channel it builds.

(upstream-side)=

## The upstream side

In the upstream repository, the action `jbigot/distropack-builder/notify` sends the dispatch the
snapshot channel's workflow above receives, for the commit being pushed:

```yaml
name: Notify the packages
on:
  push: { branches: [main] }
permissions: {}
jobs:
  notify:
    runs-on: ubuntu-24.04
    steps:
    - uses: jbigot/distropack-builder/notify@<commit hash> # v1.0.0
      with:
        repository: example/pkgs
        channel: nightly
        token: ${{ secrets.PKGS_DISPATCH_TOKEN }}
```

The token must be allowed to dispatch on the packaging repository, which `GITHUB_TOKEN` never is.
Store one of these as the secret `PKGS_DISPATCH_TOKEN` of the upstream repository:

- a fine-grained personal access token with access to the packaging repository only, with the
  permission *Contents: read and write*;
- an installation token of a GitHub App installed on the packaging repository with that
  permission, made by `actions/create-github-app-token` in a step before `notify`.

`packages` names the packages this push concerns, when the channel builds several of them from
different upstreams; by default, every package of the channel is built from the pushed commit.
`dry-run: true` prints the request instead of sending it.
