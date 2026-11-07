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

- each URL is fetched once, however many builds need it: the `orig:` tarball of a deb package is
  the same for all its releases;
- a URL that cannot be fetched is known before any build: it fails the builds that need it, which
  then skip their dependents, and spares the others;
- timeouts, transport errors and server errors (5xx) are retried, five attempts in all; a client
  error, such as a 404, fails at once.

The `orig:` URL of a deb package is used only for the `3.0 (quilt)` format, which
`debian/source/format` gives. The downloaded tarball is named
`<package>_<upstream version>.orig.tar.<compression>`, the compression read from its content.

## Builds

The builds run in dependency order, `-j` of them at once. When one fails, every build that depends
on it, directly or not, is skipped without running; the builds independent of it go on.

Each build adds an entry to its package's changelog, for deb, whose version sorts the rebuild where
it belongs:

```text
<version>~bpo<release version>.pdidev.<seconds>
```

The `~bpo<release version>` part, absent for a release without a version (Debian's sid), sorts the
rebuild before `<version>` itself, and the rebuild for a release before the rebuild for a later
one: upgrading a system to the next release upgrades the package too. The last part counts the
seconds from the changelog entry's date to the build, so that a later rebuild of the same entry
sorts after an earlier one.

Ctrl-C and SIGTERM (`kill`, `timeout`, a cancelled CI job) take the same path: no build starts any
more, the containers of those running are killed, nothing is published, and the run's work
directory is removed.

## Publication

A release is published only when every one of its builds succeeded; the other releases of the
distribution publish regardless. A release that is not published keeps the repository of its last
publication, so that its users never see a partial set of packages.

Each release is published on its own: when the publication of one fails, the others still publish.
The README of a distribution lists the releases this run published, and those it did not publish
whose previous repository is still in the output directory. A distribution of which no release is
published is left untouched, its README and exported key included.

The run exits with status 1 when a build failed, was skipped or was interrupted, or when a
publication failed.

## The report

The run prints one line per build when it ends: `built`, `failed`, `skipped` (with the failed build
it waited for) or `interrupted`, then the package and the release. The output of the build's
containers is kept per build, and printed under the line of a build that failed, so that the output
of parallel builds never interleaves. With `-v`, it is printed as it comes instead, each line
prefixed with its build. On a terminal, a progress bar counts the builds done.

When `GITHUB_ACTIONS` is `true`, each build is printed as one collapsed group of the job's log, its
output included, and a table of every build's outcome is appended to the job's summary
(`GITHUB_STEP_SUMMARY`).

The run ends with the list of the builds that did not succeed.
