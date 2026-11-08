<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Build the images locally

The images are defined by a Docker bake file that distropack-builder writes; the `Docker images`
workflow builds them from the same file. To build one locally, from the root of a checkout:

```sh
uv run distropack-build images bake-file -o docker-bake.json
docker buildx bake -f docker-bake.json --list targets
docker buildx bake -f docker-bake.json debian_builder-trixie
```

The first command lists on its standard error, as `UNAVAILABLE <base image> for <target>`, every
release whose base image its registry does not publish (yet): that release has no target, by
design, and the file holds all the others. Naming no target builds them all.

The build contexts are absolute paths into the installed `distropack_builder` package, the
checkout itself under `uv run`. Bake only reads files below its working directory without asking:
from elsewhere, grant it that directory, for example
`docker buildx bake --allow fs.read=<checkout> -f docker-bake.json <target>`.

The images are tagged as the registry has them, `ghcr.io/jbigot/distropack-builder/...`. The
runner starts its containers with `docker run`, which pulls only a missing image: a local build
therefore stands in for the published one until it is removed.
