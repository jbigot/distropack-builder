# Use distro-releases from Python

`distro_releases` answers which releases a distribution has and which are supported, on a given
day. Its source is read the first time a release of that distribution is asked for, then kept for
the rest of the process.

## List the supported releases

```python
from distro_releases import distribution

for release in distribution('ubuntu').releases():
    if release.is_supported():
        print(release.version, release.codename, release.suite)
```

`distributions()` gives every distribution the library knows; `distribution(id)` raises
`UnknownDistribution` for an id it does not know.

## As of another day

Every question takes a date, `on`, today when it is left out. The releases built for a day only
include those created by then, with that day's suites:

```python
from datetime import date

day = date(2025, 8, 9)  # Debian 13's release day
trixie = next(r for r in distribution('debian').releases(day) if r.codename == 'trixie')
assert trixie.suite == 'stable' and trixie.is_supported(day)
```

Take the date once, and pass it everywhere, when a long-running program must see a single list.

## Select releases with tokens

`select` reads the tokens of a `build.conf` `versions` list: `all`, `supported`, a codename, a suite
or a version, and `+` or `-` for the releases after or before the previous token's:

```python
from distro_releases import select

select(distribution('debian'), ['bookworm', '+'])
```

A token that names no release raises `UnknownRelease`, unless it is a suite of the distribution
that has no release on that day (Ubuntu's `devel` between two development cycles), which selects
nothing. `matches(release, 'debian:trixie')` tells whether a `<distribution>[:<release>]` pattern
names a release.

## Handle an unreachable source

A source that cannot be read raises `SourceError`, whose `url` says which; server errors and
network failures are retried first. Only the distributions of that source are affected: Debian's
releases still load while Bodhi, Fedora's source, is down.
