<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Explanation

This section explains the design: why distropack-builder exists next to the other tools, how
packages are built in isolation, how repositories are signed and published, and how the release
data are fetched.

- [Why distropack-builder](why.md)
- [How a run is organized](architecture.md)
- [Containers and engines](containers.md)
- [How packages and repositories are signed](signing.md)
- [Reproducible builds](reproducibility.md)
- [What a release keeps between runs](state.md)

```{toctree}
:hidden:

why
architecture
containers
signing
reproducibility
state
```
