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

Every offender is listed; the repository just written is removed, so that nothing deployable is
left, and the run exits 1. A repository or a release the URL does not have yet publishes nothing,
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
a publication does, and exits 1 listing the offenders. `--url DIST=URL` compares a distribution
with another URL than its repository's, such as a repository being replaced. It needs the signing
key, as a publication does.
