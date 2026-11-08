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

At its root, `action.yml` and `plan/action.yml` are the builder's GitHub Actions, which run the
builder from the sources through `action/run-cli`.

The documentation is at https://jbigot.github.io/distropack-builder/, built from `docs/`;
[CONTRIBUTING.md](CONTRIBUTING.md) says how to work on the repository, test it and release it.

Both packages are under the MIT license, following the [REUSE](https://reuse.software/)
specification: `LICENSES/` holds the texts, each file its SPDX header or an entry of `REUSE.toml`.
