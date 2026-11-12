<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Record pending changes, and cut a release

Between two upstream releases, a snapshot channel builds the upstream branch, whose next release
may need packaging changes the current release must not get: a new build dependency, a new
plugin. They wait in the channel's own directory until the release; `distropack-build pending` and
`release` manage that, in the packaging repository, which they leave for you to review and commit.
Both refuse to start on a working tree with uncommitted changes, and need `release.author`:

```yaml
release:
  author: "Jane Packager <jane@example.org>"
  packages: ["example", "example-plugin-*"]   # default: every package with a snapshot channel
  # distribution: unstable                    # of the deb entries
```

## Set changes pending

```sh
distropack-build pending build.conf nightly example
```

copies `example/` to `example-nightly/`, which the `nightly` channel now builds from, with a
`UNRELEASED` entry `Pending changes for the next release` on top of its `debian/changelog` and in
its spec's `%changelog`. Edit `example-nightly/` and commit: the release channel keeps building
`example/` as it was. A fix that the release needs too goes to `example/`, and to
`example-nightly/` by hand.

## Cut the release

Once upstream tagged `1.3.0`:

```sh
distropack-build release build.conf 1.3.0
```

1. checks that no commit changed `example/` since `example-nightly/` was made: such a commit is
   listed, with `port these into example-nightly/ and rerun with --ported`, and nothing is
   touched; once the changes are in the pending directory, `--ported` goes on;
2. makes each pending directory the package's directory;
3. gives every package `release.packages` selects an entry for the release: the deb changelog's
   `UNRELEASED` entry becomes `example (1.3.0-1) unstable`, `Upstream release 1.3.0` first, or
   a new entry is written; the spec gets `Version: 1.3.0`, `Release: 1` (`%{?dist}` kept) and its
   `%changelog` entry likewise, but a spec whose `Release` and `%changelog` rpmautospec writes
   (`%autorelease`, `%autochangelog`) gets its `Version` alone.

A tag with a `-` is a pre-release: `1.3.0-rc1` is version `1.3.0~rc1`, which sorts before
`1.3.0`, and the spec's `Source` URLs get the tag itself where they held `%{version}`. With
pending changes in several channels, `--from <channel>` says which the release takes.

`build.conf` is never edited. Review the result with `git diff`, update each package's `sources`
file (`distropack-build sources`), and commit.
