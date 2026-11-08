# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

from dataclasses import dataclass, field

from distro_releases import Release
from distropack_builder.graph import closure, dependencies
from support import FORKY, TRIXIE


@dataclass(frozen=True)
class Pkg:
    name: str
    dist_release: Release
    bin_provides: frozenset[str] = field(default=frozenset())
    bin_depends: frozenset[str] = field(default=frozenset())


def lib(release: Release) -> Pkg:
    return Pkg('lib', release, frozenset({'liblib', 'liblib-dev'}))


def app(release: Release) -> Pkg:
    return Pkg('app', release, frozenset({'app'}), frozenset({'liblib-dev', 'make'}))


def test_dependency_within_the_release() -> None:
    graph = dependencies([lib(TRIXIE), app(TRIXIE)])
    assert graph == {lib(TRIXIE): frozenset(), app(TRIXIE): frozenset({lib(TRIXIE)})}


def test_no_dependency_across_releases() -> None:
    graph = dependencies([lib(TRIXIE), app(FORKY)])
    assert graph == {lib(TRIXIE): frozenset(), app(FORKY): frozenset()}


def test_unprovided_dependency_ignored() -> None:
    graph = dependencies([app(TRIXIE)])
    assert graph == {app(TRIXIE): frozenset()}


def test_closure_is_transitive() -> None:
    a, b, c = (Pkg(n, TRIXIE) for n in 'abc')
    graph = {a: frozenset[Pkg](), b: frozenset({a}), c: frozenset({b})}
    assert closure(graph, c) == {a, b}


def test_closure_excludes_itself() -> None:
    a, b = (Pkg(n, TRIXIE) for n in 'ab')
    graph = {a: frozenset[Pkg](), b: frozenset({a})}
    assert b not in closure(graph, b)
    assert closure(graph, a) == frozenset()
