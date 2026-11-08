<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# distropack-builder

distropack-builder builds deb and rpm packages, and the signed repositories that serve them, for
every supported release of several Linux distributions, in containers. distro-releases, the
package it relies on, says which releases each distribution has and which are supported.

It was started for goals no other tool met together ([why](explanation/why.md)):

- a set of packages that depend on each other, built in parallel;
- Debian, Ubuntu, Fedora and Enterprise Linux from one configuration and one command;
- isolated builds, so that the packaging declares everything it needs;
- many releases of each distribution from one packaging;
- no server: the same command locally and in CI, and a static site to publish;
- the distributions' own tools first, so that a package directory stays submittable upstream.

The documentation is in four parts: a [tutorial](tutorial/index.md) to learn, [how-to
guides](how-to/index.md) for given tasks, the [reference](reference/index.md), and
[explanations](explanation/index.md) of the design.

```{toctree}
:hidden:

tutorial/index
how-to/index
reference/index
explanation/index
```
