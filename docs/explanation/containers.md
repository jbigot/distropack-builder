<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Containers and engines

distropack-builder runs every tool in a container, so that a build sees only what its packaging
declares and the host needs nothing but a container engine.

## Where each tool runs

| Container | Image | Runs |
|---|---|---|
| the builders | `<distribution>_builder:<release>` | the target release's own `dpkg-buildpackage` or `rpmbuild`, after installing the build dependencies; for rpm, also the `rpmspec` queries of every spec of the release, in one container |
| aptly | `aptly` | `aptly`, which publishes deb repositories unsigned: the builder signs them itself |
| rpm signing | `rpm_sign` | `rpmsign`, through gpg, with the key and its passphrase mounted for that one call |

Everything else runs in the builder's own process: the signatures of deb sources and of every
repository (pysequoia), and the metadata of rpm repositories (the createrepo_c bindings). Only
what has no equivalent on PyPI keeps an image.

## Engines

The builder drives the engine through the Docker Engine API, which Docker and Podman both serve,
rootful or rootless. It talks to the first of these that applies:

1. the `--docker-host` option, or `DISTROPACK_DOCKER_HOST`;
2. `DOCKER_HOST`;
3. the current Docker context, `DOCKER_CONTEXT`'s or else the one `docker context use` chose, when
   it is not `default` (rootless Docker is usually reached this way);
4. Docker's socket, `/var/run/docker.sock`, if it exists;
5. Podman's socket, `$XDG_RUNTIME_DIR/podman/podman.sock`, if it exists.

The endpoint must be a local `unix://` socket: the containers bind-mount directories of the host
running the builder, which a remote engine could not see.

## The container user

The builder asks the engine whether it runs rootless (`name=rootless` among its security options,
under Docker and Podman alike), and chooses the user of each container from that alone:

- under a **rootless** engine, every container runs as container root, which the engine maps to
  the invoking user: what a container writes to its mounts belongs to that user. Any other uid
  would map to a subordinate one, unable to write the mounts.
- under **rootful** Docker, the tool containers run as the invoking user's uid and gid, for the
  same result.
- the **builders** start as container root under every engine, since they install the build
  dependencies; they then build as an unprivileged `builder` user (`dpkg-buildpackage` with
  fakeroot, `rpmbuild`), as the archives' own builders do, so that packaging that writes outside
  its build tree fails; and they hand their outputs back to the owner of their source mount.

## Engine quirks handled once

Every container is created with SELinux labelling disabled (`label=disable`), so that bind mounts
work on SELinux-enforcing hosts without relabelling the user's directories. It applies to every
container, the builders included: SELinux does not confine the build scripts either, and the
isolation of a build rests on the container itself. Every container also gets an explicit, unlimited
process limit, since Podman's default throttles parallel builds, and a fully qualified image
reference, since Podman may ask which registry a short name means.

Containers are labelled `distropack-builder=1`. Interrupting a run, with Ctrl-C or SIGTERM, kills
its containers, and every container is removed once it ends, whether it succeeded, failed or was
killed.
