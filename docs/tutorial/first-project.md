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

The same build runs in a GitHub Actions workflow with the action. Store the content of `key.gpg`
(armored, as exported above) as the repository secret `HELLO_KEY`, the passphrase as
`HELLO_PASSPHRASE`, and add `.github/workflows/packages.yml`:

```yaml
name: Packages
on:
  push: { branches: [main] }
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-24.04
    steps:
    - uses: actions/checkout@<commit hash> # v7.0.1
      with: { persist-credentials: false }
    - env: { KEY: "${{ secrets.HELLO_KEY }}" }
      run: printf '%s\n' "$KEY" > key.gpg
    - id: build
      uses: jbigot/distropack-builder@<commit hash> # v1.0.0
      with:
        passphrase: ${{ secrets.HELLO_PASSPHRASE }}
    - uses: actions/upload-artifact@<commit hash> # v7.0.2
      with:
        name: repositories
        path: repositories
```

Each push to `main` then builds the packages for Debian 13 and Fedora 44 and keeps the two signed
repositories as the run's `repositories` artifact. [Use the GitHub Action](../how-to/github-action.md)
says how to pin the action, and how to give each distribution or each release a job of its own.
