<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Build for openSUSE

openSUSE's releases are Leap's, from 16.0 on, and Tumbleweed, the rolling release, which is always
supported:

```yaml
distribs:
  opensuse:
    versions: [supported]          # Leap's supported releases, and Tumbleweed
    repository: {path: opensuse, url: "https://example.org/opensuse/"}
```

- The spec is the same as for Fedora and Enterprise Linux; its build dependencies are installed with
  zypper, from what `rpmspec -q --buildrequires` lists.
- openSUSE defines no `%{?dist}`: the builder sets one per release, `.lp160` for Leap 16.0, `.tw` for
  Tumbleweed, so that a spec's `Release: 1%{?dist}` gives each release its own version, as on
  Fedora.
- The repository is a dnf-style one, which zypper reads: its page tells hosts to add the release's
  `.repo` file with `zypper addrepo`, and to trust the key at the first `zypper refresh`.
