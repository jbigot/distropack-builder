"""Which releases a configuration names.

`select` reads a distribution's list of release tokens, as `build.conf` gives it; `matches` reads a
`<distribution>[:<release>]` pattern, as a command line restricting a run gives it.
"""

from collections.abc import Sequence
from datetime import date

from distro_releases.errors import SelectorError, UnknownRelease
from distro_releases.model import Distribution, Release

# The suites a distribution has, which may name no release on some days (Ubuntu's devel between the
# release of one version and the opening of the next).
KNOWN_SUITES = {
    'debian': frozenset(
        {'stable', 'oldstable', 'oldoldstable', 'testing', 'unstable', 'experimental'}
    ),
    'fedora': frozenset({'rawhide'}),
    'ubuntu': frozenset({'devel'}),
}


def select(
    distribution: Distribution, tokens: str | Sequence[str], on: date | None = None
) -> frozenset[Release]:
    """The releases of distribution that tokens name, as of on, today by default.

    Each token adds releases to the result: `all` every release, `supported` the supported ones,
    `+` those after the latest the previous token named, `-` those before its earliest, and any
    other token the releases whose codename, suite or version it is.

    Raises:
        SelectorError: a `+` or a `-` with no token before it.
        UnknownRelease: a token that names no release and is not a suite of the distribution.
        SourceError: the distribution's source could not be read.
    """
    on = on or date.today()
    releases = distribution.releases(on)
    result: set[Release] = set()
    previous: set[Release] = set()
    for token in [tokens] if isinstance(tokens, str) else tokens:
        if token in ('+', '-'):
            if not previous:
                raise SelectorError(
                    f"'{token}' must follow a release in {distribution.id} versions"
                )
            if token == '+':
                current = {r for r in releases if r > max(previous)}
            else:
                current = {r for r in releases if r < min(previous)}
        elif token == 'all':
            current = set(releases)
        elif token == 'supported':
            current = {r for r in releases if r.is_supported(on)}
        else:
            current = {r for r in releases if token in (r.codename, r.suite, r.version)}
            if not current and token not in KNOWN_SUITES.get(distribution.id, frozenset()):
                raise UnknownRelease(distribution.id, token)
        result |= current
        previous = current
    return frozenset(result)


def matches(release: Release, pattern: str) -> bool:
    """Whether pattern names release.

    The pattern is the distribution's id or name, alone or followed by `:` and the release's
    version, name, suite or codename: `debian`, `debian:trixie`, `Debian GNU/Linux:13`.
    """
    distribution, colon, name = pattern.partition(':')
    if distribution not in (release.distribution.id, release.distribution.name):
        return False
    names = (release.version, release.name, release.suite, release.codename)
    return not colon or name in {n for n in names if n is not None}
