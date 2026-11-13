<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Package a first project

This tutorial packages `hello-greeting`, a package that installs one file, for Debian 13 and Fedora 44:
it writes its packaging and `build.conf`, creates a signing key, builds the signed repositories,
and installs from them in clean containers. You need distropack-builder installed
(`pip install distropack-builder`), Docker or Podman, and gpg.

## The package

Make a directory for the project, and in it one for the package:

```text
hello-project/
├── build.conf
└── hello-greeting/
    ├── debian/
    │   ├── changelog
    │   ├── control
    │   ├── rules
    │   └── source/format
    └── hello-greeting.spec
```

The package installs one file, `/usr/share/hello/greeting`, which its build writes. The deb
packaging is a `3.0 (native)` source package, built from the package's directory itself:

`debian/source/format`:

```text
3.0 (native)
```

`debian/changelog` (the date must be in this exact format):

```text
hello-greeting (1.0) unstable; urgency=medium

  * Initial release.

 -- Your Name <you@example.org>  Sat, 07 Nov 2026 12:00:00 +0000
```

`debian/control`:

```text
Source: hello-greeting
Section: misc
Priority: optional
Maintainer: Your Name <you@example.org>
Build-Depends: debhelper-compat (= 13)
Standards-Version: 4.7.0
Rules-Requires-Root: no

Package: hello-greeting
Architecture: all
Depends: ${misc:Depends}
Description: says hello
 Installs a greeting in /usr/share/hello.
```

`debian/rules`, executable, its recipe lines indented with a tab:

```make
#!/usr/bin/make -f

%:
	dh $@

override_dh_auto_install:
	mkdir -p debian/hello-greeting/usr/share/hello
	echo 'Hello, world!' > debian/hello-greeting/usr/share/hello/greeting
```

The rpm packaging is the spec:

`hello-greeting/hello-greeting.spec`:

```text
Name:           hello-greeting
Version:        1.0
Release:        1%{?dist}
Summary:        Says hello
License:        MIT
BuildArch:      noarch

%description
Installs a greeting in /usr/share/hello.

%install
mkdir -p %{buildroot}/usr/share/hello
echo 'Hello, world!' > %{buildroot}/usr/share/hello/greeting

%files
/usr/share/hello/greeting

%changelog
* Sat Nov 07 2026 Your Name <you@example.org> - 1.0-1
- Initial release.
```

A real package has sources: [](../how-to/add-package.md) says where the builder takes them from.

## `build.conf`

```yaml
distribs:
  debian:
    versions: [trixie]
    repository: {path: repositories/debian, url: "https://example.org/debian", name: hello}
  fedora:
    versions: ['44']
    repository: {path: repositories/fedora, url: "https://example.org/fedora", name: hello}
packages:
  hello-greeting: {}
```

`url` is where you will serve the repositories: it goes in the instructions of their pages and in
the rpm `.repo` files. The versions are a codename for Debian, a version for Fedora, quoted so that
YAML reads a string; [](../how-to/releases.md) shows how to follow the supported releases instead.

## The signing key

The builder signs the packages and the repositories with the key in `key.gpg`, beside
`build.conf`. Make one, with a passphrase, and export its secret part:

```sh
gpg --quick-generate-key 'Hello packages <packages@example.org>' ed25519 sign never
gpg --export-secret-keys --armor 'Hello packages' > key.gpg
```

Keep `key.gpg` out of version control, and the passphrase in a file only you can read:

```sh
install -m 600 /dev/null ~/.hello-passphrase   # then write the passphrase in it
```

## Build

From the project's directory:

```sh
distropack-build run --passphrase-file ~/.hello-passphrase
```

The builder pulls the builder images of Debian 13 and Fedora 44, builds `hello-greeting` for each, and
publishes `repositories/debian` and `repositories/fedora`, signed, each with its `index.html`
page and its public key: `hello-archive-keyring.gpg` for Debian, `hello.key` for Fedora. In CI,
set `DISTROPACK_GPG_PASSPHRASE` from a secret instead of using a file.

Each run builds everything again. To keep what a run built, and build again only what changed, give
it a directory to keep it in:

```sh
distropack-build run --passphrase-file ~/.hello-passphrase --state-dir ~/.hello-state
```

A second run then says `reused` for each package; [What a release keeps between
runs](../explanation/state.md) tells when a package is rebuilt, and why a changed package needs a
new version.

## Install

The repositories are plain directories: serve them at their `url` with any web server. To try
them first, mount them into clean containers, with the same checks a user's apt and dnf make.

```sh
docker run --rm -v "$PWD/repositories/debian:/repo:ro" debian:trixie sh -c '
  cat > /etc/apt/sources.list.d/hello.sources <<EOF
Types: deb
URIs: file:/repo
Suites: trixie
Components: main
Signed-By: /repo/hello-archive-keyring.gpg
EOF
  apt-get update && apt-get install -y hello-greeting && cat /usr/share/hello/greeting'
```

```sh
docker run --rm -v "$PWD/repositories/fedora:/repo:ro" fedora:44 sh -c '
  cat > /etc/yum.repos.d/hello.repo <<EOF
[hello]
name=hello
baseurl=file:///repo/44
gpgcheck=1
repo_gpgcheck=1
gpgkey=file:///repo/hello.key
EOF
  dnf install -y hello-greeting && cat /usr/share/hello/greeting'
```

Both refuse a repository whose signatures do not check. Once the repositories are served, the
`index.html` of each gives your users the same instructions, with the real URLs.

## Build in CI

The same build runs in GitHub Actions with the reusable workflow, which builds, checks the
packages install, and publishes the repositories on GitHub Pages. Store the content of `key.gpg`
(armored, as exported above) as the repository secret `HELLO_KEY`, the passphrase as
`HELLO_PASSPHRASE`, let Pages deploy from GitHub Actions (the repository's settings, *Pages*,
*Source*), and add `.github/workflows/packages.yml`:

```yaml
name: Packages
on:
  push: { branches: [main] }
permissions: {}
jobs:
  packages:
    permissions: { contents: read, packages: write }
    uses: jbigot/distropack-builder/.github/workflows/distropack.yml@<commit hash> # v1.0.0
    with:
      channel: stable
      check-install: fresh
    secrets:
      passphrase: ${{ secrets.HELLO_PASSPHRASE }}
      signing-key: ${{ secrets.HELLO_KEY }}
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

Each push to `main` then builds the packages for Debian 13 and Fedora 44, installs each signed
repository in a clean container, and publishes them. Set the repositories' `url` in `build.conf` to
where Pages serves them for the pages' instructions to be right; then `check-install: upgrade`, the
default, checks from the second publication on that what users have upgrades. Without a
`state-registry`, every push rebuilds everything:
[Keep the states in a registry](../how-to/state-registry.md) says how to keep what did not change,
and [Build and publish with the reusable workflow](../how-to/triggers.md) how to trigger the
builds otherwise.
