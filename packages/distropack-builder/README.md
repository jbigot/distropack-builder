<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# distropack-builder

distropack-builder builds deb and rpm packages, and the signed apt and dnf repositories that
serve them, for every supported release of Debian, Ubuntu, Fedora and Enterprise Linux, in
containers, with one command.

- **A dependent set, built in parallel:** independent packages build at the same time, and each
  build sees the packages it depends on, freshly built for the same release.
- **Several families from one configuration:** Debian, Ubuntu, Fedora and Enterprise Linux,
  published as signed repositories.
- **Isolated builds:** each build starts from a clean image of its release, installs only the
  declared build dependencies and runs unprivileged, as in the archives' own builders; rootful or
  rootless Docker, or rootless Podman, with no host root.
- **Many releases with minimal duplication:** one `debian/` directory and one spec per package
  serve every release, and the release list is computed (`supported`, `+`, `-`).
- **No server:** the same command locally and in CI, and a static site any web host can serve.
- **The distributions' own tools first:** the packaging is built with `apt-get build-dep`,
  `dpkg-buildpackage` and `rpmbuild`, and nothing is asked of it that an archive would not ask, so
  a package directory stays submittable to Debian or Fedora.

## Install

```sh
pip install distropack-builder
```

It needs Docker or Podman to run the builds.

## Use

Beside a `hello/` directory holding `debian/` and `hello.spec`, a `build.conf`:

```yaml
distribs:
  debian: {versions: [supported]}
  fedora: {versions: [supported]}
packages:
  hello: {}
```

and the signing key in `key.gpg`, then:

```sh
distropack-build run --passphrase-file ~/.hello-passphrase
```

builds `hello` for every supported Debian and Fedora release, and publishes the signed
repositories under `repositories/`.

In a GitHub Actions workflow, the action does the same:

```yaml
- uses: jbigot/distropack-builder@<commit hash>
  with:
    passphrase: ${{ secrets.HELLO_PASSPHRASE }}
```

The documentation, a tutorial included, is at https://jbigot.github.io/distropack-builder/.
