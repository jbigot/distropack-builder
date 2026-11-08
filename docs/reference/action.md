<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# The GitHub Actions

Both actions run exactly the code of the commit or tag they are pinned to, with the third-party
dependencies its `uv.lock` pins; nothing is built nor installed besides those dependencies.
[Use the GitHub Action](../how-to/github-action.md) shows them in a workflow.

## `jbigot/distropack-builder`

```{include} _generated/action.md
```

## `jbigot/distropack-builder/plan`

```{include} _generated/plan-action.md
```
