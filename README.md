<!--
SPDX-FileCopyrightText: 2021-2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# distropack-builder

Build deb and rpm packages, and the signed repositories that serve them, for every supported
release of several Linux distributions, in containers.

This repository is a uv workspace of two Python packages, released together:

- [`distropack-builder`](packages/distropack-builder/README.md), the builder, with the Docker
  images of its builders and tools under `packages/distropack-builder/src/distropack_builder/images/`;
- [`distro-releases`](packages/distro-releases/README.md), the releases of each Linux
  distribution and their support status, which the builder relies on.

At its root, `action.yml`, `plan/action.yml` and `notify/action.yml` are the builder's GitHub
Actions, which run the builder from the sources through `action/run-cli`, and
`.github/workflows/distropack.yml` its reusable workflow. A packaging repository builds, checks
and publishes its packages with one job calling it, and one deploying the site:

```yaml
name: Packages
on:
  push: { tags: ['v*'] }
permissions: {}
jobs:
  packages:
    permissions: { contents: read, packages: write }
    uses: jbigot/distropack-builder/.github/workflows/distropack.yml@<commit hash> # v1.0.0
    with:
      channel: stable
      state-registry: ghcr.io/example/pkgs/state
    secrets:
      passphrase: ${{ secrets.PACKAGES_PASSPHRASE }}
  deploy:
    needs: packages
    if: ${{ !cancelled() && needs.packages.outputs.site-artifact != '' }}
    runs-on: ubuntu-24.04
    permissions: { pages: write, id-token: write }
    environment: { name: github-pages, url: "${{ steps.deploy.outputs.page_url }}" }
    steps:
    - id: deploy
      uses: actions/deploy-pages@<commit hash> # v5.0.1
      with: { artifact_name: "${{ needs.packages.outputs.site-artifact }}" }
```

[Build and publish with the reusable workflow](docs/how-to/triggers.md)
shows a workflow for each way of triggering it.

The documentation is at https://jbigot.github.io/distropack-builder/, built from `docs/`;
[CONTRIBUTING.md](CONTRIBUTING.md) says how to work on the repository, test it and release it.

Both packages are under the MIT license, following the [REUSE](https://reuse.software/)
specification: `LICENSES/` holds the texts, each file its SPDX header or an entry of `REUSE.toml`.
