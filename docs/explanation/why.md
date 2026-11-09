<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Why distropack-builder

distropack-builder was started for goals that no existing tool met together. Each goal below
names the tests that show it holds; the comparison with the other tools follows.

## 1. Parallel builds of a set of packages that depend on each other

The packages of a project form a graph through their build dependencies: independent ones build
in parallel, and each build sees the packages it depends on, freshly built for the same release,
through a local repository.

- The scheduler builds a package after its dependencies
  ({source}`test_dependencies_first <packages/distropack-builder/tests/test_scheduler.py>`), runs
  at most as many builds as asked ({source}`test_jobs_bound
  <packages/distropack-builder/tests/test_scheduler.py>`), and skips only the descendants of a
  failed build ({source}`test_failure_skips_only_descendants
  <packages/distropack-builder/tests/test_scheduler.py>`).
- The end-to-end fixture builds `hello-app` against the `hello-lib` built in the same run, and
  installing `hello-app` prints `hello-lib`'s greeting
  ({source}`test_install_from_repository <tests/e2e/test_install.py>`).

## 2. Debian, Ubuntu, Fedora and Enterprise Linux from one configuration and one command

One `build.conf` and one `distropack-build run` publish signed apt and dnf repositories.

- The fixture is built for Debian, Ubuntu, Fedora and Enterprise Linux in one run, and each repository
  installs in a clean container of its release with apt or dnf checking the signatures
  ({source}`test_install_from_repository <tests/e2e/test_install.py>`); a tampered repository is
  refused ({source}`test_tampered_repository_is_refused <tests/e2e/test_install.py>`).

## 3. Isolation, so that packaging declares everything it needs

Each build starts from a clean image of the target release and installs only the declared build
dependencies; anything taken from the developer's machine, or present by accident, makes the build
fail. The build itself runs as an unprivileged user, as in the archives' own builders. Containers
through the Docker API are the means, not the point: rootful or rootless Docker, or rootless
Podman, so no host root is needed.

- A package using a tool it does not declare fails, in every family
  ({source}`test_undeclared_tool_fails <tests/e2e/test_isolation.py>`), and so does one that needs
  root to build ({source}`test_needs_root_fails <tests/e2e/test_isolation.py>`).
- The container layer runs under rootful Docker, rootless Docker and rootless Podman in CI
  ({source}`packages/distropack-builder/tests/test_runner.py`), files written into a mount belong
  to the invoking user ({source}`test_mount_written_belongs_to_invoker
  <packages/distropack-builder/tests/test_runner.py>`).

## 4. Many releases of each distribution with minimal duplication

One `debian/` directory and one spec per package serve every release. The release list is
computed (`supported`, `+`, `-`) by distro-releases instead of maintained by hand, and the
per-release versions (`~bpo<N>`) are derived. Differences between releases are expressed inside
the packaging with means the archives accept (alternative or versioned build dependencies, tests
on build-dependency versions, rpm `%if 0%{?fedora} >= N`), so the same directory stays submittable
to Debian and Fedora, or by disabling a package for some releases in `build.conf`.

- The release tokens select the supported releases, a codename, a version or a suite, and the
  releases after or before them ({source}`test_supported
  <packages/distro-releases/tests/test_selectors.py>`, {source}`test_after_and_before
  <packages/distro-releases/tests/test_selectors.py>`).
- Each release gets its own version of the same changelog entry ({source}`test_versioned_release
  <packages/distropack-builder/tests/test_versions.py>`).
- A package is left out of the releases `build.conf` disables for it ({source}`test_disabled
  <tests/orchestration/test_selection.py>`).

## 5. No server to run

The same command runs locally and in CI, and its output is a static site that any web host, GitHub
Pages included, can serve.

## 6. The distributions' own tools and conventions first

distropack-builder builds the packaging with the distributions' own tools (`apt-get build-dep`,
`dpkg-buildpackage`, `dnf builddep`, `rpmbuild`), as an unprivileged user and with no network as
their builders do,
downloads a deb package's upstream tarball with `uscan`, as its `debian/watch` says, accepts a
download only if it matches its line in the package's `sources` file (Fedora's) or its upstream
signature (Debian's), and asks nothing
of the packaging that an archive would not, so that a package directory stays submittable to
Debian or Fedora. Where it departs from them, it is to support what an archive's packages do not
need (several distributions and releases from one packaging, a dependent set built in parallel),
and the departure is listed below with what it buys and what it costs.

| Departure | What it supports | What it costs |
|---|---|---|
| Builds in its own images (`apt-get build-dep`, then `dpkg-buildpackage` or `rpmbuild`), not in sbuild or mock | a dependent set built in parallel against a local repository of fresh builds, every container engine through one API, no host root | the build environment could differ from the archives'; the images install nothing more than sbuild's or mock's build roots would, and a difference is a bug of the images |
| deb builds get the build profiles `<distribution>,<codename>` (`-P` of `dpkg-buildpackage` and `apt-get build-dep`) | varying a build per release | Debian reserves the build profile namespace and its archive never sets these, so a `Build-Profiles: <trixie>` restriction would not survive a submission; dropping them is proposed |
| rpm sources (`Source:` URLs) are downloaded by distropack-builder, not by `spectool -g` | one download per URL across packages and releases, retried on transient errors | none for the packaging: the URLs are the spec's own |
| Release versions such as `1.0-1~bpo13.pdidev.<n>` | the same changelog entry built for several releases, and rebuilds | the scheme follows the official backports one, with a suffix of its own |

## How it compares

Checked on 2026-11-05.

| Tool | Families | Builds a dependent set in parallel | Isolated, clean builds | Many releases from one packaging | Repository publishing | Runs where |
|---|---|---|---|---|---|---|
| **distropack-builder** | Debian, Ubuntu, Fedora, EL, openSUSE | yes, graph across packages | yes, container per build, no network, no host root needed | yes, computed release list | yes, signed, static | locally or any CI |
| sbuild, pbuilder/cowbuilder, debspawn | Debian, Ubuntu | no, one package per run | yes (chroot, unshare, nspawn) | one run per release | no | locally |
| git-buildpackage | Debian, Ubuntu | no | delegates to the above | no | no | locally |
| mock (`--chain`) | Fedora, EL, openSUSE | in order, not in parallel | yes | one run per target | no | locally |
| Copr, Packit | RPM family | build batches | yes | yes (chroots) | yes, hosted | hosted service |
| Launchpad PPA | Ubuntu | dependency waits | yes | yes | yes, hosted | hosted service |
| Open Build Service | Debian, Ubuntu, Fedora, openSUSE, EL | yes | yes (VMs) | yes | yes, hosted; apt repositories are flat (one per project and release, no `dists/`, suites nor components) | OBS server (public or self-hosted) |
| packpack, build-deb-action | several / Debian | no, one package | yes (Docker) | one run per release | no | locally, CI |
| fpm, nFPM | deb, rpm, … | no | no build at all | n/a | no | anywhere |

What this means in practice:

- The Open Build Service is the closest in scope, and the one to choose when a hosted build farm is
  wanted. Its apt repositories are flat, though: one directory per project and release, scanned
  with `dpkg-scanpackages`, under a `Release` file without components. A channel there is a
  separate project with its own URL, where distropack-builder publishes one archive-style
  repository with codename and suite aliases.
- The per-family tools, sbuild and mock, are what distropack-builder complements rather than
  replaces.
- fpm and nFPM solve another problem: they wrap files already built, with no source package, build
  dependencies nor distribution policy.

Its limits: the native packaging is still written per family (`debian/` and a spec), only `amd64`
is built, and the scale of the hosted services (many architectures, build farms) is out of scope.
