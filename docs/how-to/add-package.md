<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Add a package

1. Make a directory named after the package beside `build.conf`, with its packaging: `debian/`
   for the Debian family, `<name>.spec` for the Fedora family, or both. A package with neither is
   an error. `debian/changelog` gives the deb version; the builder adds an entry of its own for
   each release it builds for, which never reaches your directory.
2. Name it in `build.conf`'s `packages`, with no setting (`example: {}`, or `example:`), or with
   the ones it needs:

   ```yaml
   packages:
     example:
       orig: "https://example.org/releases/example-{upstream_version}.tar.gz"
       disable: {fedora: [rawhide], ubuntu: all}
   ```

## Its sources

- A deb package of format `3.0 (native)` (`debian/source/format`) is built from its own
  directory, which holds the sources beside `debian/`.
- A deb package of format `3.0 (quilt)` holds `debian/` alone, and its upstream tarball is
  downloaded from `orig`, where `{upstream_version}` and `{package}` are replaced from
  `debian/changelog`. The builder names the file `<package>_<version>.orig.tar.<compression>`,
  the compression read from the file itself. `orig` needs `debian/`: it is a deb setting, and an
  rpm build of the same package ignores it.
- An rpm package downloads what its spec's `Source:` lines name, as `rpmspec` expands them in each
  release's own builder image.

Every source is downloaded once, before the first build: a download that fails fails the builds
that need it, and spares the others.

## Its dependencies

A package that build-depends on another of `build.conf` (`Build-Depends` in `debian/control`,
`BuildRequires` in the spec, matched against what the other one's binary packages provide) is
built after it, with its packages available to the build. A failed build skips the builds that
depend on it.

## Where it is not built

`disable` maps distribution ids to the releases the package is not built for, as
[release tokens](releases.md) name them: `all`, `supported`, a codename, a version, `+` or `-`
after one. The other packages of a release are still built and published.
