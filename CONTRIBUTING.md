# Contributing

## Setting up

The repository is a [uv](https://docs.astral.sh/uv/) workspace of two packages,
`packages/distro-releases/` and `packages/distropack-builder/`.

```sh
uv sync                     # the workspace environment, with the dev group
uv run pre-commit install   # ruff, mypy, the lock, actionlint and zizmor at each commit
```

`uv run <command>` runs a command in that environment. ruff and mypy come from `uv.lock`, in the
hooks as in CI: `uv run ruff check`, `uv run ruff format`, `uv run mypy`.

The code moved from the former scripts is typed and linted as later phases rewrite it: the
`ignore_errors` list of `[tool.mypy]` and the per-file ignores of `[tool.ruff.lint]` name it, and
those lists only shrink.

## Tests

| Layer | Run with | Needs |
|---|---|---|
| unit | `uv run pytest` | nothing: no network, recorded release data, a fixed date |
| `packaging` | `uv run pytest -m packaging` | git, uv and PyPI: it builds the packages from synthetic repositories and archives |
| `golden` | `uv run pytest -m golden` | a Docker engine: it compares the repositories built for the fixture project with the recorded golden master |
| `e2e` | `uv run pytest -m e2e` | a Docker engine: it builds the fixture project and installs from its repositories |

A rootless Docker engine works for `golden` and `e2e`: the tests then go through a docker shim.
The golden master itself is recorded on a rootful engine, the one of GitHub's runners.
The `docker` and `live` layers of the design come in later phases (4 and 9).

## Versions

Each package takes its version from its own git tags, `distro-releases-v<X.Y.Z>` and
`distropack-builder-v<X.Y.Z>`, through hatch-vcs; the GitHub Action's `v<X.Y.Z>` tags follow
`distropack-builder`.

From a `git archive`, git expands a single `describe` per archive, so only `distropack-builder`
gets its version from it, and `distro-releases` builds as `0.0`. An archive also reads its version
only when no parent directory holds a `.git`: extracted inside another repository, it takes that
repository's tags.

## Images and publication

`./build` builds the Docker images. It is transitional, and goes when the images move to a bake
file. Nothing is published from this repository (packages, tags, images or documentation) before
the initial release.

## Dependencies

Nothing is pinned: `uv.lock` makes runs reproducible, and the weekly `Upgrade` workflow runs the
checks on the latest release of every dependency and pre-commit hook, without committing. When it
fails, an upgrade broke something: fix the code, or the wrapper around the dependency, rather than
pinning it, and run `uv lock --upgrade` in the same change.

The build requirements of the two packages (hatchling, hatch-vcs, setuptools-scm) are not in
`uv.lock`: a fresh environment builds the packages with their latest releases. A release of one of
them that breaks the build therefore fails `CI` on whatever commit comes next, not `Upgrade`: look
there first when `CI` fails in `uv sync` on a commit that does not touch the packaging.

## Documentation

```sh
uv run --group docs sphinx-build -W --keep-going -n -b html docs <output directory>
```

Choose the output directory outside the tree, or list it in your `.git/info/exclude`.
