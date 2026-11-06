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
| `golden` | `uv run pytest -m golden` | a Docker engine and the network, to pull the builder images: it compares the repositories built for the fixture project with the recorded golden master |
| `e2e` | `uv run pytest -m e2e` | a Docker engine and the network, to pull the builder images: it builds the fixture project and installs from its repositories |
| `docker` | `uv run pytest -m docker` | a container engine (Docker or Podman, rootful or rootless) and `docker.io/library/alpine`: it runs containers through the runner; CI runs it on rootful Docker, rootless Docker and rootless Podman |
| `live` | `uv run pytest -m live` | the network: it reads the real release sources of distro-releases (the nightly `Live canary` workflow, which opens an issue labelled `live-canary` when it fails) |

`.python-version` makes 3.13 the default interpreter, the one the golden master is recorded with;
`uv run --python 3.12 pytest` runs the unit tests on another one, as the CI matrix does.

Rootless Docker and rootless Podman work for `golden` and `e2e` as rootful Docker does.
The golden master itself is recorded on a rootful engine, the one of GitHub's runners.
The orchestration tests (`tests/orchestration/`) replace the runner with `tests/fake_runner.py`'s
`FakeRunner`, so that they check what the builders ask of the engine without running anything.

The tests of `distro-releases` live in `packages/distro-releases/tests/`, with the release data they
and the root `tests/` read, recorded under `data/` by `record.py` (run by hand, with the network). That
directory is not a package: mypy checks it in a run of its own (the `mypy (distro-releases tests)`
hook), where its `conftest.py` does not clash with `tests/conftest.py`; `packages/distropack-builder/tests/`
gets the same, with the `mypy (distropack-builder tests)` hook. CI's unit job on 3.13 also
enforces a branch-coverage floor on `distro_releases`, which only rises.

## Versions

Each package takes its version from its own git tags, `distro-releases-v<X.Y.Z>` and
`distropack-builder-v<X.Y.Z>`, through hatch-vcs; the GitHub Action's `v<X.Y.Z>` tags follow
`distropack-builder`.

From a `git archive`, git expands a single `describe` per archive, so only `distropack-builder`
gets its version from it, and `distro-releases` builds as `0.0`. An archive also reads its version
only when no parent directory holds a `.git`: extracted inside another repository, it takes that
repository's tags.

## Images and publication

`distropack-build images bake-file` writes the Docker bake definition of the images, which the
`Docker images` workflow builds, one job per image. Only its runs on `main` push them, to
`ghcr.io/jbigot/distropack-builder/`; nothing else is published from this repository (packages,
tags or documentation) before the initial release.

## Dependencies

Nothing is pinned: `uv.lock` makes runs reproducible, and the weekly `Upgrade` workflow runs the
checks on the latest release of every dependency and pre-commit hook, without committing. When it
fails, an upgrade broke something: fix the code, or the wrapper around the dependency, rather than
pinning it, and run `uv lock --upgrade` in the same change.

The build requirements of the two packages (hatchling, hatch-vcs, setuptools-scm) are not in
`uv.lock`: a fresh environment builds the packages with their latest releases. A release of one of
them that breaks the build therefore fails `CI` on whatever commit comes next, not `Upgrade`: look
there first when `CI` fails in its first `uv run` on a commit that does not touch the packaging.

## Documentation

```sh
uv run --group docs sphinx-build -W --keep-going -n -b html docs <output directory>
```

Choose the output directory outside the tree, or list it in your `.git/info/exclude`.
