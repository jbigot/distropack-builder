<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# How a run is organized

`distropack-build run` goes through five phases, each finished before the next starts: selection,
builders, downloads, builds, publication. A failure stays as local as it can: one package that does
not build stops what depends on it, and the release it belongs to, but nothing else.

## Selection

The run first reads `build.conf` and the release data of the distributions it names, then connects
to the container engine. An error in the configuration, a release source that does not answer or an
engine that cannot be reached stops the run here, before any container starts.

## Builders

A build is one package for one release. The run creates one for each package and release that
`build.conf` selects, when the package has packaging for that family: a `debian/` directory for deb
releases, a `<package>.spec` for rpm ones. A package without packaging for a family has no build
there, and so cannot hold its releases back.

Creating a builder reads what its package provides and what it needs to build: `debian/control`
for deb, `rpmspec` queries for rpm, which also give the URLs of the spec's `Source:` lines.

Builds depend on each other within a release only: a build depends on the builds of its release
whose packages provide one of its build dependencies. Its local repository, the one its builder
container installs build dependencies from, holds the outputs of every build it depends on,
directly or not.

## Downloads

Every file the builds need from the network is downloaded before the first build starts:

- each URL is fetched once, however many builds need it, and each deb upstream tarball once per
  watch file and upstream version: the same for all the releases of a package, and for every
  package sharing them;
- a URL that cannot be fetched is known before any build: it fails the builds that need it, which
  then skip their dependents, and spares the others;
- timeouts, transport errors and server errors (5xx) are retried, five attempts in all; a client
  error, such as a 404, fails at once.

The upstream tarball of a deb package of the `3.0 (quilt)` format (`debian/source/format`) is
downloaded by `uscan`, in the Debian tools image, as the package's `debian/watch` says; the
project is mounted read-only. uscan writes the tarball as downloaded and
`<package>_<upstream version>.orig.tar.<compression>` beside it, a symbolic link, or a repacked
tarball when `debian/copyright` excludes files. Another package with the same watch file and
upstream version gets its own name for it from `mk-origtargz`, run in its directory.

## Builds

The builds run in dependency order, `-j` of them at once. When one fails, every build that depends
on it, directly or not, is skipped without running; the builds independent of it go on.

Each build adds an entry to its package's changelog, for deb, whose version sorts the build where
it belongs, and whose date is that of the packaging's own top entry, never the clock's: the same
inputs give the same version and the same `SOURCE_DATE_EPOCH`.

```text
<version>~bpo<release version>[+b<rebuild>]
```

The `~bpo<release version>` part, absent for a release without a version (Debian's sid), sorts the
build before `<version>` itself, and the build for a release before the build for a later one:
upgrading a system to the next release upgrades the package too, as the official backports do. A
rebuild of the same version, its build dependencies having changed, adds a binNMU's `+b<n>`, and
an rpm's release gains `.<n>`; the number comes from what a release keeps of its last build
(Phase 12's state), 0 until then. A snapshot of an upstream branch takes its upstream version from
the commit, `<last tag>+main.<commits since>.g<hash>`, with the epoch raised above the
changelog's ({source}`versions.py <packages/distropack-builder/src/distropack_builder/versions.py>`).

Ctrl-C and SIGTERM (`kill`, `timeout`, a cancelled CI job) take the same path: no build or download
starts any more, the containers of the builds running are killed, nothing is published, the run's
work directory is removed, and the run ends with Python's `KeyboardInterrupt`. A download stalled
on the network ends at its 30-second timeout, without a retry.

## Publication

A release is published only when every one of its builds succeeded; the other releases of the
distribution publish regardless. A release that is not published keeps the repository of its last
publication, so that its users never see a partial set of packages.

Each release is published on its own: when the publication of one fails, the others still publish.
The README of a distribution lists the releases this run published, and those it did not publish
whose previous repository is still in the output directory. A distribution of which no release is
published is left untouched, its README and exported key included.

The run exits with status 1 when a build failed or was skipped, or when a publication failed.

## The report

The run prints one line per build when it ends: `built`, `failed`, `skipped` (with the failed build
it waited for) or `interrupted`, then the package and the release. The output of the build's
containers is kept per build, and printed under the line of a build that failed, so that the output
of parallel builds never interleaves. With `-v`, it is printed as it comes instead, each line
prefixed with its build. On a terminal, a progress bar counts the builds done.

When `GITHUB_ACTIONS` is `true`, each build is printed as one collapsed group of the job's log, its
output included, and a table of every build's outcome is appended to the job's summary
(`GITHUB_STEP_SUMMARY`).

Unless interrupted, the run ends with the list of the builds that did not succeed.
