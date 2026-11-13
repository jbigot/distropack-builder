<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

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
| `e2e` | `uv run pytest -m e2e` | a Docker engine and the images the checkout bakes (below): it builds the fixture project and installs from its repositories |
| `docker` | `uv run pytest -m docker` | a container engine (Docker or Podman, rootful or rootless) and `docker.io/library/alpine`: it runs containers through the runner; CI runs it on rootful Docker, rootless Docker and rootless Podman |
| `live` | `uv run pytest -m live` | the network: it reads the real release sources of distro-releases (the nightly `Live canary` workflow, which opens an issue labelled `live-canary` when it fails) |
| nightly `e2e` | the `Nightly e2e` workflow, every night (or `DISTROPACK_NIGHTLY=1 uv run pytest -m e2e`) | as `e2e`: the fixture built for two releases of every family, one packaging for several releases of each |
| the actions | the `Action` workflow, on every push | GitHub Actions: it runs `uses: ./plan` and `uses: ./` on the fixture project, as another repository's workflow would, and checks their outputs, that a failed build fails the step, and that the passphrase shows nowhere in the logs |

`.python-version` makes 3.13 the default interpreter, the one CI's `e2e` and `packaging` jobs use;
`uv run --python 3.14 pytest` runs the unit tests on another one, as the CI matrix does.

`e2e` runs the images of the checkout, not those the registry publishes from `main`:
bake them into the local engine first, with `uv run python tests/bake_fixture_images.py` (the
fixture's builders and the tools; Docker buildx needed). The runner asks for each image's protocol
tag, which the local image answers without a pull.

Rootless Docker and rootless Podman work for `e2e` as rootful Docker does.

`tests/golden/` holds what remains of the golden master that compared the fixture project's
repositories with those master's code built, retired once the builder was rewritten: the
normalization of a repository, and master's Rocky Linux 10 repository so normalized, the reference
of the el repository to come.

The orchestration tests (`tests/orchestration/`) replace the runner with `tests/fake_runner.py`'s
`FakeRunner`, so that they check what the builders ask of the engine without running anything. They
build a builder on a `BuildContext` holding the fake, or run the whole command line,
`cli.make_app(runner=..., connect=...)` under typer's `CliRunner`, on a copy of the fixture project,
the upstream tarball served by respx; `tools_respond` answers the tool containers such a run asks
for.

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

## Releasing

A version tag releases one package: `distro-releases-v<X.Y.Z>` releases `distro-releases`,
`distropack-builder-v<X.Y.Z>` releases `distropack-builder`. The tag carries the version as the
build writes it (PEP 440, normalized: `1.2.0rc1`, not `1.2.0-rc1`). Pushing it runs the `Release`
workflow:

1. `build` builds both packages, each sdist then its wheel from that sdist, checks them with
   `twine check --strict`, and checks that the tagged package's distributions carry exactly the
   tag's version (`.github/scripts/check-tag-version`); a tag whose commit builds another version
   stops there;
2. `test` installs both packages from their wheels, then from their sdists, in fresh virtual
   environments on Python 3.13 and 3.14, and runs the unit suite against them
   (`.github/scripts/test-installed`, which first checks that the modules come from the
   installation, not from the checkout);
3. `publish` uploads the tagged package to PyPI through trusted publishing, from the `pypi`
   environment, the only job with `id-token: write`.

Running the workflow by hand, on any branch, is the dry run: `build` and `test` run, `publish`
never does. Locally:

```sh
uv build --all-packages --out-dir /tmp/dist
.github/scripts/test-installed /tmp/dist wheel   # or sdist
```

Nothing is tagged before the initial release (at the end of Phase 15 of the modernization plan),
which also configures the PyPI side: the `pypi` environment of this repository and a trusted
publisher per package naming this repository, `release.yml` and that environment.

## Licensing

Both packages are under the MIT license and the repository follows the
[REUSE specification](https://reuse.software/spec/), which the `reuse` pre-commit hook checks: a
new file starts with the two SPDX lines (`reuse annotate --copyright "Julien Bigot
<julien@julien-bigot.fr>" --license MIT --year 2026 <file>` writes them), and a file that takes
no comment, or whose bytes a test compares, gets an entry in `REUSE.toml` instead. Third-party
data keep their own notice there.

## Dependencies

What CI and the actions run is pinned, and every upgrade comes as a pull request, which CI checks
before it is merged:

- the Python dependencies, in `uv.lock`, the workflow actions, by commit hash, and the pre-commit
  hooks, by revision: Dependabot proposes their upgrades every week;
- uv itself, in `.tool-versions`, which every `astral-sh/setup-uv` step reads: Dependabot cannot
  bump it, so the `uv` job of the weekly `Upgrade` workflow opens that pull request, on a branch
  `upgrade/uv-<version>`, and starts `CI` and `Action` on it. It needs the repository setting
  *Allow GitHub Actions to create and approve pull requests*. Locally, any recent uv works: uv
  itself does not read `.tool-versions`.

The packages' own dependencies stay unbounded in their `pyproject.toml`: the pins are the lock's,
not their users'. The `Upgrade` workflow also runs the checks on the latest release of every
dependency and hook, without committing: when it fails, an upgrade broke something, which the
code, or the wrapper around the dependency, is fixed for.

The build requirements of the two packages (hatchling, hatch-vcs, setuptools-scm) are not in
`uv.lock`: a fresh environment builds the packages with their latest releases. A release of one of
them that breaks the build therefore fails `CI` on whatever commit comes next, not `Upgrade`: look
there first when `CI` fails in its first `uv run` on a commit that does not touch the packaging.

## Documentation

```sh
uv run --group docs sphinx-build -W --keep-going -n -b html docs <output directory>
```

Choose the output directory outside the tree, or list it in your `.git/info/exclude`.

That builds the working tree. The published site holds one version per release of
`distropack-builder` (the `distropack-builder-v<X.Y.Z>` tags, published as `X.Y.Z/`) and `main`,
each built from its own checkout in a virtual environment synced from its own `uv.lock`, with a
version switcher in the sidebar and a root page opening the highest release (`main` before the
first one):

```sh
uv run --group docs sphinx-polyversion docs/poly.py <output directory>
```

It reads the local `main`, or `origin`'s when there is no local one, and fails when any version
fails to build. The `Docs` workflow builds it on every push to `main` and leaves it as the run's
`site` artifact; nothing deploys it before the initial release. In a page, ``{source}`<path>` ``
links a file of the repository as of that version: its tag, or `main`'s commit.
