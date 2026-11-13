<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Use the GitHub Action

The action `jbigot/distropack-builder` runs `distropack-build run` in a workflow job; the action
`jbigot/distropack-builder/plan` lists the releases a `build.conf` selects, as a job matrix. Their
inputs and outputs are in [the reference](../reference/action.md).

To build, keep the states, check and publish a site, call the reusable workflow instead
([Build and publish with the reusable workflow](triggers.md)): it uses these actions in the jobs it
runs. The actions are for pipelines of your own.

## Pin a version

An action runs exactly the code of what `uses:` names, with the third-party dependencies its
`uv.lock` pins: nothing else is resolved, built or installed at run time. Pin a full commit hash,
and keep the version in a comment for Dependabot to update:

```yaml
- uses: jbigot/distropack-builder@<commit hash> # v1.0.0
```

Version tags, `v1.0.0` and a moving `v1`, come with the first release. The runner needs a
container engine: GitHub's Ubuntu runners have Docker; a self-hosted runner needs Docker or Podman
(see [Run with rootless Docker or Podman](rootless.md)).

## Build and publish each distribution

One job per distribution builds every release `build.conf` selects for it, and publishes its
repository. The passphrase of the signing key comes from a secret, here `PACKAGES_PASSPHRASE`:

```yaml
name: Packages
on:
  push: { branches: [main] }
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-24.04
    strategy:
      fail-fast: false
      matrix:
        distrib: [debian, ubuntu, fedora]
    steps:
    - uses: actions/checkout@<commit hash> # v7.0.1
      with: { persist-credentials: false }
    - id: build
      uses: jbigot/distropack-builder@<commit hash> # v1.0.0
      with:
        distributions: ${{ matrix.distrib }}
        passphrase: ${{ secrets.PACKAGES_PASSPHRASE }}
    - uses: actions/upload-artifact@<commit hash> # v7.0.2
      with:
        name: ${{ matrix.distrib }}
        path: ${{ fromJSON(steps.build.outputs.output-dirs)[matrix.distrib] }}
```

`output-dirs` gives the absolute path of each repository published, by distribution id, and
`<distribution id>.<channel>` for an rpm snapshot channel's own repository. A failed
build fails the step, once the releases all of whose builds succeeded are published; to go on
anyway, set `continue-on-error: true` on the step and read its `failed` output.

The step's log has one group per build, in the order they end: a failed or interrupted build's
group holds its whole output, a successful one's only its status line. Set `verbose: true` to get
every build's output.

## One job per release

The `plan` action lists the releases, so that each gets a job of its own: to spread a large set
of packages over more runners, or to test each release's packages on its own runner.

```yaml
jobs:
  plan:
    runs-on: ubuntu-24.04
    outputs:
      matrix: ${{ steps.plan.outputs.matrix }}
    steps:
    - uses: actions/checkout@<commit hash> # v7.0.1
      with: { persist-credentials: false }
    - id: plan
      uses: jbigot/distropack-builder/plan@<commit hash> # v1.0.0
  build:
    needs: plan
    runs-on: ubuntu-24.04
    strategy:
      fail-fast: false
      matrix:
        include: ${{ fromJSON(needs.plan.outputs.matrix) }}
    steps:
    - uses: actions/checkout@<commit hash> # v7.0.1
      with: { persist-credentials: false }
    - uses: jbigot/distropack-builder@<commit hash> # v1.0.0
      with:
        distributions: ${{ matrix.distrib }}:${{ matrix.release }}
        passphrase: ${{ secrets.PACKAGES_PASSPHRASE }}
```

Each job then publishes a repository holding its release alone: the action does not merge the
repositories of several jobs.

## Build a snapshot channel when upstream moved

A scheduled workflow builds a snapshot channel only when something changed: `mode: fetch`
resolves the channel's refs, downloads every source into `download-dir`, and sets `changed`
against the states; the build steps then run from what it fetched, all on the same commits. The
states and downloads last from run to run in `actions/cache`:

```yaml
name: Nightly
on:
  schedule: [{ cron: '17 2 * * *' }]
permissions:
  contents: read
jobs:
  nightly:
    runs-on: ubuntu-24.04
    steps:
    - uses: actions/checkout@<commit hash> # v7.0.1
      with: { persist-credentials: false }
    - uses: actions/cache@<commit hash> # v5.0.1
      with:
        path: |
          states
          downloads
        key: nightly-${{ github.run_id }}
        restore-keys: nightly-
    - id: fetch
      uses: jbigot/distropack-builder@<commit hash> # v1.0.0
      with: { mode: fetch, channel: nightly, state-dir: states, download-dir: downloads }
    - if: steps.fetch.outputs.changed == 'true'
      uses: jbigot/distropack-builder@<commit hash> # v1.0.0
      with:
        channel: nightly
        state-dir: states
        download-dir: downloads
        passphrase: ${{ secrets.PACKAGES_PASSPHRASE }}
```

A run triggered by an upstream push builds the pushed commit rather than the branch's head, which
may have moved since: pass it to the `mode: fetch` step as `snapshots: <package>=<commit>`. Several jobs can share one fetch:
build with `mode: no-publish` per distribution, each restoring the same download directory, then
publish once with `mode: publish-only`.
