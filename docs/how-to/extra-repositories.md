<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Depend on another project's packages

A package may build-depend on packages another project publishes: a set of packages built on PDI
can take PDI from its repository rather than build it again. Name that repository in the
distribution's `extra_repositories`, with the public key that signs it:

```yaml
distribs:
  debian:
    versions: [supported]
    extra_repositories:
    - url: "https://repo.pdi.dev/debian/"
      key: pdidev.asc            # beside build.conf
      key_url: "https://repo.pdi.dev/debian/pdidev-archive-keyring.gpg"
      name: pdi
  fedora:
    versions: [supported]
    extra_repositories:
    - url: "https://repo.pdi.dev/fedora/{version}"
      key: pdidev.asc
      key_url: "https://repo.pdi.dev/fedora/pdidev.key"
      name: pdi
```

- `url` is the whole repository for the Debian family, a release's directory for the rpm families;
  `{codename}`, `{version}` and `{suite}` are replaced from each release. `suite` (default
  `{codename}`) and `components` (default `[main]`) are the Debian family's.
- `key` is mandatory, a public key, armored or binary: every extra repository is
  signature-checked, by apt (`Signed-By`), dnf and zypper (`gpgcheck`, `repo_gpgcheck`), with that
  key and no other. The key is read from the project, never downloaded during a build.
- `key_url` is mandatory when the distribution's repository has a `url`, since its page tells its
  users to download the key from it. `name`, in file names, takes letters, digits, `_` and `-`.
- The builds install from it with the network they have while installing build dependencies, never
  while building. The project's own packages win over an extra repository's: a package the project
  builds itself is the one its other packages are built against.
- A `file://` URL names a directory of the machine running the builder, mounted read-only into the
  builds.
- A release the repository has no suite or directory for fails, the other releases going on.

The repository's page tells its users to add each extra repository first, with its key's
fingerprint, and its key from `key_url` when given: installing the packages needs them.
