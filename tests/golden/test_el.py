# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The el repository of the fixture project, compared with master's Rocky Linux 10 repository."""

import json
from pathlib import Path
from typing import cast

import pytest

from normalize import normalize_repository

MASTER = Path(__file__).parent / 'data' / 'rocky-master.json'


def flat(value: object, prefix: str = '') -> dict[str, object]:
    """The leaves of a nested structure, by path."""
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            out |= flat(item, f'{prefix}/{key}')
        return out
    return {prefix: value}


@pytest.mark.e2e
def test_el_repository_is_masters_rocky_one(fixture_build: Path) -> None:
    """The same packages, metadata, signatures and keys; these differences alone:

    - the repository's path and name: `el` and Enterprise Linux, not `rocky` and Rocky Linux;
    - the page, index.html, written from the Jinja template (Phase 7).
    """
    master = flat(json.loads(MASTER.read_text()))
    el = flat(json.loads(json.dumps(normalize_repository(fixture_build / 'repositories' / 'el'))))
    # release 10 alone, which master's data holds: the nightly run builds 9 as well
    el = {k: v for k, v in el.items() if '/9/' not in k and not k.startswith(('/text/9/',))}
    el['/tree'] = [f for f in cast(list[str], el['/tree']) if not f.startswith('9/')]
    rocky_repo = str(master.pop('/text/10/hello.repo'))
    assert el.pop('/text/10/hello.repo') == rocky_repo.replace('/rocky/', '/el/').replace(
        'Rocky Linux 10', 'Enterprise Linux 10'
    )
    master.pop('/text/index.html')
    el.pop('/text/index.html')
    differences = sorted(k for k in master.keys() | el.keys() if master.get(k) != el.get(k))
    assert differences == []
