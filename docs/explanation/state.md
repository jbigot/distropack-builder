<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# What a release keeps between runs

A run given `--state-dir DIR` keeps, for each release, the packages of its last complete build and
what they were built from: the release's *state*. The next run starts from it. A package whose
inputs did not change is not built again; one whose build dependencies changed is rebuilt with a
rebuild number, and a source change without a new version is refused. The repositories are then
written from the states. Without `--state-dir`, the state lives in the run's temporary directory:
every package is built, as if for the first time.

## The state of a release

`DIR/stable/<release>/` (`stable` is the one channel until snapshot channels come) holds a
`manifest.json` and one directory per package with the files of its build: the source package,
the binary packages, the `.buildinfo`. For each package, the manifest records:

- its **base version**: the version a first build gets, `<changelog version>~bpo<release version>`
  for a deb (the changelog's own on a release without a version, as Debian's sid), the spec's
  `Version`-`Release` as the release's rpm expands it;
- its **rebuild number** `n`, 0 for a first build, and its full version;
- its **static key**: a sha256 over its packaging (`debian/`, the whole source directory of a
  native deb, or the spec file), the files downloaded for it, and the channel; and its
  **environment key**: the sha256 of the list of every package installed in its build container
  once its build dependencies were in.

## What a build does with it

| What the state stores of the package | The build |
| --- | --- |
| nothing, or another base version | from source: a deb's `-sa` build, the spec as written |
| the same base version and static key, the same environment | none: the stored package is **reused** |
| the same base version and static key, another environment | a **rebuild**, number `n + 1` |
| the same base version, another static key | **refused**: a new version is needed |

The environment is known only once the build dependencies are installed: the dependency container
always runs, and when what it installed is what the state records, the build container does not.
A rebuild candidate whose deb source has no `Architecture: any` package is reused without any
container, since a binary-only rebuild never rebuilds `Architecture: all` packages.

A refused package fails with

```text
<package>: sources or packaging changed without a new version (add a debian/changelog entry / bump Release in the spec)
```

before any container starts: publishing other bytes under a version users may have installed would
leave them with packages that differ from what the repository says.

## Rebuilds

A **deb** rebuild is a binNMU, as Debian's archive makes them: the stored source package, unchanged,
is unpacked with an entry `<base>+b<n>` (`binary-only=yes`) on top of its changelog, and only its
`Architecture: any` packages are built, with `dpkg-buildpackage -B`. The release keeps the stored
source package and `Architecture: all` packages beside the new binaries, whose `Source:` field
names the unchanged source version: a binNMU never alters what the repository publishes as source.

An **rpm** has no binary-only build: the whole package is built again with `Release: <spec
release>.<n>`, and a `%changelog` entry `- Rebuilt against updated build dependencies` dated as the
spec's top one, as Fedora's mass rebuilds do. A spec without `%changelog` gets the `Release`
alone: nothing in it would date an entry.

A new base version starts again at 0, without suffix.

## What a failure keeps

Once every build of the run is settled, a release all of whose builds were built or reused gets
them as its new state, written aside and then put in place of the old one; a package no longer in
`build.conf` is not carried over. A release with a failed, skipped or interrupted build keeps its
previous state, untouched, and the run says so (`kept the previous state of <release>`) and exits
with status 1. An interrupted run commits no state.

## Publication

With `--state-dir`, the repositories are written again at each run from the state of every release
`build.conf` selects, whatever `-D` builds: the repository directories are removed first, so that
they hold exactly the releases that have a state, and their pages list those. A release without a
state yet is left out (`no state for <release>`). Hence `repository.path` must be a directory inside
the project, never the project itself.

`--no-publish` builds and keeps the states without writing any repository; `--publish-only` builds
nothing and writes the repositories from the states, so that the releases can be built in separate
jobs and published once.

Without `--state-dir`, only the releases this run built completely are written, and a release that
failed keeps the repository of its last publication, as before states existed.
