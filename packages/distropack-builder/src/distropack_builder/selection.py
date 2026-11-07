"""The releases a build configuration selects, and those its packages leave out."""

from collections.abc import Callable, Collection, Iterable, Mapping
from datetime import date

from distro_releases import Release, SelectorError, matches, select
from distropack_builder import targets
from distropack_builder.config import AVAILABLE, BuildConfig

__all__ = ['AVAILABLE', 'configured_releases', 'disabled_releases', 'matching']


def matching(releases: Iterable[Release], patterns: Collection[str]) -> frozenset[Release]:
    """The releases one of the -D patterns names."""
    return frozenset(r for r in releases if any(matches(r, pattern) for pattern in patterns))


def configured_releases(
    config: BuildConfig,
    patterns: Collection[str],
    on: date,
    is_available: Callable[[Release], bool],
) -> dict[str, frozenset[Release]]:
    """The releases build.conf names for each of its distributions, as of on.

    With -D patterns, a distribution none of them names gets no release, and its source is not
    read: a source being down only stops the distributions it serves. A pattern naming a release
    must name one that build.conf selects, available aside. A distribution whose
    versions hold `available` keeps the releases is_available accepts, and only those of its
    releases that the patterns kept are asked about.

    Raises:
        SelectorError: a pattern names no distribution of build.conf, or no release it selects,
            or the tokens cannot be read.
        SourceError: a distribution's source could not be read.
    """
    distributions = {dist_id: targets.target(dist_id) for dist_id in config.distribs}
    for pattern in patterns:
        prefix = pattern.partition(':')[0]
        if not any(prefix in (d.id, d.name) for d in distributions.values()):
            raise SelectorError(f'-D {pattern} names no distribution of build.conf')
    named = {pattern.partition(':')[0] for pattern in patterns}
    # the patterns naming a release, which must name one build.conf selects
    unmatched = {pattern for pattern in patterns if ':' in pattern}
    result = {}
    for dist_id, distrib in config.distribs.items():
        distribution = distributions[dist_id]
        if patterns and not named & {distribution.id, distribution.name}:
            result[dist_id] = frozenset[Release]()
            continue
        releases = select(distribution, [t for t in distrib.versions if t != AVAILABLE], on)
        if patterns:
            unmatched -= {p for p in unmatched if any(matches(r, p) for r in releases)}
            releases = matching(releases, patterns)
        if AVAILABLE in distrib.versions:
            releases = frozenset(r for r in sorted(releases) if is_available(r))
        result[dist_id] = releases
    if unmatched:
        raise SelectorError(f'-D {min(unmatched)} names no release build.conf selects')
    return result


def disabled_releases(
    config: BuildConfig, selected: Mapping[str, frozenset[Release]], on: date
) -> dict[str, frozenset[Release]]:
    """For each package, the selected releases its disable list names, as of on.

    Only the distributions that have selected releases are read.
    """
    return {
        name: frozenset(
            release
            for dist_id, tokens in package.disable.items()
            if selected.get(dist_id)
            for release in select(targets.target(dist_id), tokens, on)
        )
        for name, package in config.packages.items()
    }
