<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Guard what gets published

A repository's users install what its URL serves. A publication that offered them a lower version,
or the same version with other bytes, would leave them with packages that differ from what the
repository says, or that they never get rid of. Every publication therefore compares what it
writes with what the repository's `url` publishes.

## The version guard

Once a repository is written, each of its packages is compared with the same package at the
repository's `url` (the deb `Packages` and `Sources` of each release and component, the rpm
`primary` metadata of each release):

- a package not published there yet, or published at a lower version, passes;
- the same version passes only with the same bytes (a package reused as it was);
- a lower version, or the same with other bytes, fails:

  ```text
  hello-lib 1.0-1.fc44 on Fedora 44/stable: 1.0-1.fc44 is already published
  ```

Every offender is listed; the repository just written is removed, every release and component
of it, so that nothing deployable is left, and the run exits 1. A release kept from the last
publication (without `--state-dir`, one whose build failed) is compared too, since it is deployed
with the others. Each repository compared says so (`<directory>: compared with <url>`). When an
index lists several versions of a package, the new one must be above the highest. A repository or a release the URL does not have yet publishes nothing,
and passes; an index that cannot be read (a server error, no network) fails the publication, since
its versions are unknown. Versions are compared as apt and dnf compare them: epochs first,
`~` before anything.

The usual cause is a lost state: the rebuild numbers restart, and the builds reissue versions
already published. Restore the state (from its registry, [Keep the states in a
registry](state-registry.md)) rather than turning the guard off. `--no-version-guard` publishes
without comparing, saying so: for the first publication to a new URL whose repository is not
the one at that URL, or a deliberate rollback.

## Check before publishing

```sh
distropack-build check-versions --state-dir states
distropack-build check-versions --state-dir states --url debian=https://old.example.org/debian/
```

writes every channel's repositories from the states into a temporary directory, compares them as
a publication does, and exits 1 listing the offenders, or when it compared nothing (no release
with a state, or no repository with a URL). `--url DIST=URL` compares a distribution
with another URL than its repository's, such as a repository being replaced; a `DIST` that is
not a distribution of `build.conf` is an error. It needs the signing
key, as a publication does.

## Assemble the site

```sh
distropack-build site --output site --state-dir states --max-size 900M \
    --extra pdi-main.debian=frozen/pdi-main.debian
```

publishes every channel's repositories from the states under `site/`, at their `repository.path`
or channel `path`, the version guard included, then:

- copies each `--extra PATH=DIR` tree as is to `PATH` (a directory inside the site);
- writes the site's index page, linking every repository: the project's
  `README.tpl/index.html.j2` when there is one, which may extend the default as
  `distropack/index.html.j2` and override its blocks `title`, `intro` and `repositories`;
- writes a listing (directories first, sizes, UTC dates, relative links) into every directory
  without an `index.html`, so that the whole tree can be browsed;
- measures the tree, hard links once: above `--max-size` (`<n>`, `<n>K`, `<n>M`, `<n>G`, powers of
  1024, as `du -h` counts), it exits 1, listing the size of every top-level and second-level
  directory. GitHub Pages serves at most 1 GB.

It needs the signing key, as any publication.
