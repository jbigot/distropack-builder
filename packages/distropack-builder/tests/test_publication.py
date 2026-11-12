# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

from dataclasses import dataclass

from distro_releases import Release
from distropack_builder.publication import published
from distropack_builder.scheduler import Failed, Interrupted, Outcome, Reused, Skipped, Succeeded
from support import FORKY, TRIXIE


@dataclass(frozen=True)
class Build:
    name: str
    dist_release: Release


def test_all_succeeded_publishes() -> None:
    outcomes = {Build('a', TRIXIE): Succeeded(), Build('b', TRIXIE): Succeeded()}
    assert published(outcomes) == {TRIXIE}


def test_one_failure_withholds_its_release_only() -> None:
    outcomes: dict[Build, Outcome] = {
        Build('a', TRIXIE): Succeeded(),
        Build('b', TRIXIE): Failed(RuntimeError()),
        Build('a', FORKY): Succeeded(),
    }
    assert published(outcomes) == {FORKY}


def test_skipped_withholds() -> None:
    outcomes: dict[Build, Outcome] = {
        Build('a', TRIXIE): Skipped('b'),
        Build('a', FORKY): Interrupted(),
    }
    assert published(outcomes) == set()


def test_reused_release_is_published() -> None:
    outcomes: dict[Build, Outcome] = {Build('a', TRIXIE): Reused(), Build('b', TRIXIE): Succeeded()}
    assert published(outcomes) == {TRIXIE}
