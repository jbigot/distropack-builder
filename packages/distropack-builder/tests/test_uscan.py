# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""What the uscan module reads of a package's debian/ without running uscan."""

from pathlib import Path

import pytest

from distropack_builder import uscan

SIGNED = (
    'version=4\nopts="pgpsigurlmangle=s%$%.asc%" https://example.org/ example-(.+)\\.tar\\.gz\n'
)


@pytest.fixture
def package(tmp_path: Path) -> Path:
    (tmp_path / 'debian' / 'upstream').mkdir(parents=True)
    (tmp_path / 'debian' / 'upstream' / 'signing-key.asc').write_text('key\n')
    return tmp_path


@pytest.mark.parametrize(
    ('watch', 'expected'),
    [
        pytest.param(SIGNED, True, id='pgpsigurlmangle'),
        pytest.param(
            SIGNED.replace('pgpsigurlmangle=s%$%.asc%', 'pgpmode=gittag'), True, id='gittag'
        ),
        pytest.param(SIGNED.replace('pgpsigurlmangle=s%$%.asc%', 'pgpmode=none'), False, id='none'),
        pytest.param(SIGNED.replace('opts=', '# opts='), False, id='commented-out'),
    ],
)
def test_verified(package: Path, watch: str, expected: bool) -> None:
    (package / 'debian' / 'watch').write_text(watch)
    assert uscan.verified(package) is expected


def test_not_verified_without_the_key(package: Path) -> None:
    (package / 'debian' / 'watch').write_text(SIGNED)
    (package / 'debian' / 'upstream' / 'signing-key.asc').unlink()
    assert not uscan.verified(package)
