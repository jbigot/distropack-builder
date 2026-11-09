<!--
SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>

SPDX-License-Identifier: MIT
-->

# Add or drop a distribution release

A distribution's `versions` in `build.conf` lists release tokens; each adds releases to what the
distribution builds for, as of the day of the run:

| Token | Adds |
|---|---|
| `supported` | the releases the distribution supports today (the default) |
| `all` | every release distro-releases knows |
| a codename, a version, a suite | that release (`trixie`, `13`, `stable`, `testing`, `sid`, `rawhide`) |
| `+` | the releases after the latest one the previous token named |
| `-` | the releases before the earliest one the previous token named |

`available` adds nothing: it keeps, among the releases the other tokens selected, those whose
builder image the registry publishes. A release can open before its image exists, its
distribution's registry being late or its packages not installable yet; with `available`, it is
built for as soon as its image appears, and skipped until then:

```yaml
distribs:
  debian:
    versions: [bookworm, +, available]   # bookworm, and every later release with an image
```

`available` asks the registry once per release, anonymously. A registry that does not answer stops
the run: an error is never read as a missing image, which would silently drop the release.

Without `available`, `run` asks the registry the same question before any build, and a release
whose image is missing fails, once, with the reason: `no builder image for Debian GNU/Linux 12
(Bookworm): not published yet`. Its packages are not built, every other release builds and
publishes, and the run fails.

## Keep a release in extended support

A release past its end of life stays buildable while its distribution's archive still supports it
for free: Debian's LTS (bookworm until 2028-06-30), Ubuntu's server support. `supported` drops it
at its end of life; name it to keep it:

```yaml
distribs:
  debian:
    versions: [bookworm, supported]   # bookworm, in Debian's LTS, and the supported releases
```

`distro-releases list` shows such a release with the status `extended` and the date its extended
support ends, the `EXTENDED` column; its builder image is published until that day. Debian's ELTS
and Ubuntu's ESM do not count: they need a subscription the builder images do not have.

## Check what a run would build

```sh
distropack-build releases list            # one distribution:release per line
distropack-build releases list -D debian  # Debian's alone
distropack-build releases list --json     # [{"distrib": "debian", "release": "trixie"}, …]
```

The release is the codename in the Debian family and the version elsewhere, as the image tags name
it; each line can be passed to `run -D`.

## Build some releases only

`distropack-build run -D debian:trixie -D fedora` builds for the releases these patterns name,
among those of `build.conf`: a distribution's id or name, alone or followed by `:` and a codename,
a version or a suite. A pattern that names no distribution of `build.conf`, or a release its
`versions` do not select, is an error. A run that selects no release at all, every one of them
left out by `available` say, says so and does nothing.

## Drop a release

Remove its token, or let `supported` drop it at its end of life. Its repository is left as it was:
the next run neither rebuilds it nor lists it on the repository's page, but its files stay where
they were published until you remove them.
