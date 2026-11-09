# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The build targets, and what each one's family decides."""

import re
from pathlib import Path

from distro_releases import distribution
from distropack_builder import targets

SOURCES = Path(targets.__file__).parent


def test_every_target_has_a_family() -> None:
    assert {t.id: t.family for t in targets.TARGETS.values()} == {
        'almalinux': 'rpm-dnf',
        'debian': 'deb',
        'fedora': 'rpm-dnf',
        'rocky': 'rpm-dnf',
        'ubuntu': 'deb',
    }


def test_family_of_a_distribution() -> None:
    assert targets.family(distribution('ubuntu')) == 'deb'
    assert targets.is_deb(distribution('ubuntu'))
    assert targets.is_rpm(distribution('fedora'))
    assert not targets.is_rpm(distribution('debian'))


def test_base_images() -> None:
    debian, fedora = targets.TARGETS['debian'], targets.TARGETS['fedora']
    assert debian.base_image('trixie') == 'debian:trixie-slim'
    assert debian.base_image('rc-buggy') == 'debian:rc-buggy'
    assert fedora.base_image('44') == 'fedora:44'
    assert targets.TARGETS['rocky'].base_image('10') == 'rockylinux/rockylinux:10'


def test_no_name_test_outside_targets() -> None:
    """What a distribution is decides nothing outside targets.py: its family does."""
    names = '|'.join(re.escape(id) for id in targets.TARGETS)
    test = re.compile(rf"""(==|!=|\bin)\s*\(?\s*['"]({names})['"]|\.id\s*(==|!=)\s*['"]""")
    found = [
        f'{path.name}:{number}: {line.strip()}'
        for path in SOURCES.rglob('*.py')
        if path.name != 'targets.py'
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if test.search(line)
    ]
    assert found == []
