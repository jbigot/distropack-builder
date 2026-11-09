<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Reproducible builds

A build is reproducible when building the same sources again gives the same bytes, so that anyone
can check that a package is what its sources say. That is the package's doing, as far as its
distribution supports it: distropack-builder sets nothing of its own that the distribution's build
tools do not set, and only keeps out of their way.

## What the builder keeps out of the way

- **Versions and dates from the inputs alone.** The changelog entry a deb build adds is versioned
  from the packaging (`~bpo<release>`, `+b<n>` for a rebuild) and dated as the packaging's own
  top entry, never from the clock: `dpkg-buildpackage` derives `SOURCE_DATE_EPOCH` from it, and
  Fedora's rpm from the spec's `%changelog`.
- **A fixed build environment.** The build path, the user (`builder`) and the host name are the
  same in every run, and no variable of the machine running the builder reaches the build.
- **The record of the environment.** A deb package's `.buildinfo`, which lists what its build
  installed, is published beside its `.dsc`, so that anyone can rebuild in that environment.

## What a package still decides

- **Debian and Ubuntu**: debhelper and dpkg make most packages reproducible by default; what is
  left is the package's own build (a timestamp it embeds, an unsorted file list).
- **Fedora, Enterprise Linux, openSUSE**: rpm records the build time and the files' times unless
  the spec asks otherwise, which a package wanting to be reproducible does:

  ```spec
  %global clamp_mtime_to_source_epoch 1
  %global use_source_date_epoch_as_buildtime 1
  ```

  rpm also records the build host and, in the source rpm, the spec expanded with the build path:
  the builder keeps both fixed, as the archives' builders do.

## Checking it

`distropack-build run --check-reproducible` builds every package twice from the same installed
dependencies, the second time varying what must not matter: the umask, the time zone, the locale,
the parallelism, the time, and for deb the build path and the host name. Their `.deb`, `.rpm` and
`.dsc` files are compared, not the `.changes` nor the `.buildinfo`, which record the build. A
difference fails the package as `unreproducible`, with diffoscope's report on each differing file
in its output, and skips the packages that depend on it.
