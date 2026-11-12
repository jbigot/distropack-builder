<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Build snapshot channels

A *channel* is a set of repositories built together. `build.conf` has exactly one release channel,
built from each package's own packaging, and may have snapshot channels, which build some packages
from commits of their upstream git repositories: a `nightly` of the development branch, a `weekly`
of a release branch. Without `channels:`, the one release channel is named `stable`.

## Declare the channels

```yaml
channels:
  stable: {kind: release}
  nightly:
    kind: snapshot
    epoch_bump: 1           # deb epoch added to the changelog's (default 0)
    # version_label: main   # default: the ref's name, all but letters and digits made `.`
    # fallback: stable      # default: the release channel
distribs:
  debian:
    repository:
      path: debian
      url: "https://repo.example.org/debian/"
      channels: {nightly: {component: nightly}}   # default: the channel's name
  fedora:
    repository:
      path: fedora
      url: "https://repo.example.org/fedora/"
      channels:
        nightly: {path: nightly.fedora, url: "https://repo.example.org/nightly.fedora/"}
packages:
  example:
    channels:
      nightly:
        git: "https://github.com/example/example.git"
        ref: main
        verify: {keys: upstream-keys.asc}
```

- A channel's name is what a deb component, a directory suffix and an OCI tag accept:
  `[a-z0-9][a-z0-9.-]*`.
- A package builds in the snapshot channels it declares. Its entry names the `git` URL and either a
  `commit`, pinned by the packaging, or a `ref` with a trust level: `verify: none`, or
  `verify: {keys: <file>}`, which builds only a commit signed by one of the file's OpenPGP keys.
  There is no default: a channel says what it trusts.
- A deb channel is a component of the distribution's repository; an rpm channel is a repository of
  its own, whose `path` is required, and its `url` too when the distribution's repository has one.
- A 3.0 (native) deb source has no upstream tarball for a snapshot to replace: make it 3.0 (quilt)
  to build it in a snapshot channel.

## The packaging of a channel

A snapshot channel builds `<name>-<channel>/` beside `<name>/` when that directory exists, and
`<name>/` otherwise: a channel can carry packaging changes the next release will need. Its
packages' versions come from git, `<tag>+<label>.<commits since>.g<sha7>`, the tag being the
highest version tag reachable from the commit: the snapshots sort above the release they follow
and below the next one.

The snapshot's source is the commit's tree, as `git archive` makes it, in a tarball whose top
directory is `<name>-<commit>`, `<name>` being the package's name in `build.conf`. A deb source
takes it as its orig tarball, `debian/copyright`'s `Files-Excluded` applied. An rpm spec is built
with its `Version` set to the snapshot's and `%global commit <sha>` defined, the tarball as its
first `Source`; a spec that builds both kinds of sources unpacks with

```spec
%autosetup -n example-%{?commit}%{!?commit:%{version}}
```

A spec that does not fails at `%setup`, rpm saying the directory does not exist.

Every other file a snapshot downloads (a second `Source`) is checked against the package's
`sources` file, as for the release channel; the snapshot's own tarball is checked by its trust
level instead.

## Build a channel

```sh
distropack-build run --channel stable --state-dir states
distropack-build run --channel nightly --state-dir states
```

A snapshot channel builds only the packages that declare it; their other dependencies come from
its fallback channel's state, which must exist: a release without it fails with `no stable state
for <release>: build the stable channel first`. A run of either channel republishes every channel
from its state, so that the deb repository keeps all its components.

## Resolve once, build in many jobs

`fetch` resolves the channel's refs, checks the commits at their trust level, records them in
`<download dir>/snapshots.json` and downloads every source:

```sh
distropack-build fetch --channel nightly --download-dir downloads --state-dir states
distropack-build run --channel nightly --download-dir downloads --state-dir states -D debian
distropack-build run --channel nightly --download-dir downloads --state-dir states -D fedora
```

Every `run` given that download directory builds the recorded commits, even when the branch moved
meanwhile; only `fetch` records them, so that a channel advances at each `fetch`, and a `run`
without one resolves the refs itself. `--snapshot <package>=<commit>` builds that commit instead of
the ref's head, provided it is on the ref: what a push event names. Give it to `fetch`, which
records it for every job; a `run` given it builds that package's commit and the others' recorded
ones. A recorded commit is not checked again: share a download directory only where you would
share the packages built from it. With `--state-dir`, `fetch` prints `changed=true` when a
run would build anything the states do not hold (a release without a state, a new commit, a
packaging change), `changed=false` otherwise, and writes `changed` as a GitHub Actions output.
