<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Add a package

1. Make a directory named after the package beside `build.conf`, with its packaging: `debian/`
   for the Debian family, `<name>.spec` for the Fedora family, or both. A package with neither is
   an error. `debian/changelog` gives the deb version; the builder adds an entry of its own for
   each release it builds for, which never reaches your directory.
2. Name it in `build.conf`'s `packages`, with no setting (`example: {}`, or `example:`), or with
   the ones it needs:

   ```yaml
   packages:
     example:
       disable: {fedora: [rawhide], ubuntu: all}
   ```

## Its sources

- A deb package of format `3.0 (native)` (`debian/source/format`) is built from its own
  directory, which holds the sources beside `debian/`.
- A deb package of format `3.0 (quilt)` holds `debian/` alone, and its upstream tarball is
  downloaded as its `debian/watch` says, by Debian's own `uscan`, at the upstream version of
  `debian/changelog`: with its version mangling, its upstream signature check, and its
  `Files-Excluded` repacking (from a machine-readable `debian/copyright`). A quilt package without
  `debian/watch` is an error. uscan can download only a version the watch page lists. Packages
  whose watch file and upstream version are the same share one download, which `mk-origtargz`
  names (and repacks, after its own `Files-Excluded`) for each of them. A project on GitHub:

  ```text
  version=4
  opts="searchmode=plain, \
    downloadurlmangle=s%api.github.com/repos/([^/]+)/([^/]+)/tarball/refs/tags/%github.com/$1/$2/archive/refs/tags/%;s%$%.tar.gz%, \
    filenamemangle=s%.*/v?@ANY_VERSION@%@PACKAGE@-$1.tar.gz%" \
    https://api.github.com/repos/<owner>/<repository>/tags?per_page=100 \
    https://api.github.com/repos/<owner>/<repository>/tarball/refs/tags/v?@ANY_VERSION@
  ```

  It lists the tags through GitHub's API (its HTML tags page is slow to the point of timing out),
  and downloads each one from `github.com/<owner>/<repository>/archive/`, the tarball GitHub
  serves for a tag. GitHub's API answers 60 anonymous requests an hour per address.
- An rpm package downloads what its spec's `Source:` lines name, as `rpmspec` expands them in each
  release's own builder image.

Every source is downloaded once, before the first build: a download that fails fails the builds
that need it, and spares the others. A transient failure (a server error, a timeout) is retried,
uscan's included.

## Secure its sources

Every downloaded file must be what the package expects, or the package's builds fail before they
start, and the builds that depend on them are skipped:

- either its line in the package's `sources` file matches it, `SHA512 (<file>) = <digest>`,
  Fedora's format, one line per file, the file named as downloaded;
- or, for a deb upstream tarball, uscan checked its upstream signature: `debian/watch` asks for it
  (`pgpsigurlmangle`, or `pgpmode` `next`, `previous`, `self` or `gittag`) and
  `debian/upstream/signing-key.asc` holds the upstream's key, as Debian checks it. Such a tarball
  needs no line.

`distropack-build sources build.conf <package>…` downloads what each package's builds need and
writes its `sources` file. Run it when an upstream version changes, check the new lines (the
release announcement may give the digest), and commit them with the new version. A line for a file
no build downloads any more is ignored.

## Its dependencies

A package that build-depends on another of `build.conf` (`Build-Depends` in `debian/control`,
`BuildRequires` in the spec, matched against what the other one's binary packages provide) is
built after it, with its packages available to the build. A failed build skips the builds that
depend on it.

## Where it is not built

`disable` maps distribution ids to the releases the package is not built for, as
[release tokens](releases.md) name them: `all`, `supported`, a codename, a version, `+` or `-`
after one. The other packages of a release are still built and published.
