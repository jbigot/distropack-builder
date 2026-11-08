<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# How packages and repositories are signed

A run signs with one key per distribution, the `gpg` section of `build.conf` names it: the key
file (`key.gpg` by default), and optionally which key of that file (`id`, the end of its
fingerprint) and which of its user ids (`uid`). The passphrase comes from the file
`--passphrase-file` names, or from the `DISTROPACK_GPG_PASSPHRASE` environment variable, never from
the command line.

## In the builder's own process

The builder reads the key file with [pysequoia](https://pypi.org/project/pysequoia/), and unlocks
the key once, in memory, before any container starts: a wrong passphrase, or an `id` or `uid` the
file does not hold, stops the run there. The unlocked key is never written anywhere. With it the
builder makes:

- the clear signature of each deb source package's `.dsc`, right after its build;
- for each published deb release, under its codename and its suite, `Release.gpg` (detached) and
  `InRelease` (clear-signed) beside `Release`;
- for each published rpm release, `repodata/repomd.xml.asc`, a detached signature of
  `repomd.xml`;
- the public key files of the repositories: `<name>-archive-keyring.gpg` (binary) for deb, and
  `<name>.key` (armored) for rpm.

aptly publishes the deb repositories unsigned, and nothing reads the `.changes` files: aptly adds
each build's `.deb` and signed `.dsc` as they are.

A clear signature drops the spaces and tabs that end a line of its text (RFC 9580), so the builder
removes them from `Release` before signing it: `Release`, `Release.gpg` and `InRelease` say the
same thing.

The local repositories a build installs its build dependencies from are not signed: they live in
the run's work directory, and the builder reads them as trusted.

## rpm packages

An rpm package carries its signature in its own header, which only `rpmsign` writes, and
`rpmsign` signs through gpg, which reads its key from disk. So for each build's packages, the
builder writes the key file as it was given, still locked, and its passphrase into a directory only
the invoking user can read, under `/dev/shm` (a tmpfs, never written to a disk) when it can, in the
run's work directory otherwise. It mounts that directory read-only into the one `rpm_sign`
container that signs, which imports the key into a keyring of its own and gives gpg the passphrase
file. The directory is removed as soon as that container ends, whether it signed or failed. The
passphrase never appears on a command line nor in a container's environment.

## What users check

apt checks `InRelease` (or `Release` with `Release.gpg`) against the key its sources file names
(`Signed-By`), then every index and package through the checksums `Release` lists. dnf checks
`repomd.xml.asc` (`repo_gpgcheck=1`) and each package's own signature (`gpgcheck=1`) against the
key of the `.repo` file.
