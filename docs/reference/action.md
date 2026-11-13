<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# The GitHub Actions

The reusable workflow and the actions run exactly the code of the commit or tag they are pinned to, with the third-party
dependencies its `uv.lock` pins; nothing is built nor installed besides those dependencies.
[Use the GitHub Action](../how-to/github-action.md) shows them in a workflow.

## `jbigot/distropack-builder/.github/workflows/distropack.yml`

The reusable workflow: plans the releases, builds each one in a job of its own on the states a
registry keeps, then assembles the site, guards its versions, checks that it installs, and uploads
it as a Pages artifact for the caller to deploy. Its jobs run the builder of the workflow's own
commit, whatever the caller checks out.

```{include} _generated/workflow.md
```

## `jbigot/distropack-builder`

```{include} _generated/action.md
```

## `jbigot/distropack-builder/plan`

```{include} _generated/plan-action.md
```

## `jbigot/distropack-builder/notify`

For the upstream repository: asks the packaging repository to build a channel from the commit
being pushed. The reusable workflow reads the request through its `dispatch-payload` input.

```{include} _generated/notify-action.md
```
