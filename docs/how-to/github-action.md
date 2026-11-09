<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Use the GitHub Action

The action `jbigot/distropack-builder` runs `distropack-build run` in a workflow job; the action
`jbigot/distropack-builder/plan` lists the releases a `build.conf` selects, as a job matrix. Their
inputs and outputs are in [the reference](../reference/action.md).

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

`output-dirs` gives the absolute path of each repository published, by distribution id. A failed
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
