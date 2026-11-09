<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Build for Enterprise Linux

Enterprise Linux is one target, `el`, for Red Hat Enterprise Linux and the distributions that share
its ABI: AlmaLinux, Rocky Linux and Oracle Linux. One repository per major serves them all, as
EPEL's does:

```yaml
distribs:
  el:
    versions: [supported]
    repository: {path: el, url: "https://example.org/el/"}
```

- Its releases are RHEL's majors (8, 9, 10). A new major counts as released once Rocky Linux has
  released it too: the builds run in Rocky Linux's image of that major (`rockylinux/rockylinux`, its
  latest minor), which rebuilds RHEL's own sources.
- The builder image enables CRB (PowerTools on 8) and EPEL, so a spec may build-depend on them.
- Minor releases are not targets: a host on an older minor updates first, as with any third-party
  Enterprise Linux repository. AlmaLinux 10's `x86_64_v2` variant is not served: the builds follow
  RHEL 10's x86-64-v3 baseline.

The repository's page tells each host what to enable before installing, since the packages may
depend on CRB and EPEL: `dnf config-manager --set-enabled crb` and `epel-release` on AlmaLinux and
Rocky Linux, `subscription-manager` and EPEL's own package on RHEL, `ol<N>_codeready_builder` and
`oracle-epel-release-el<N>` on Oracle Linux. The tests install the repository on Rocky Linux,
AlmaLinux and Red Hat's freely redistributable UBI.
