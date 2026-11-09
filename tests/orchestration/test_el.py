# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Enterprise Linux, the builder's single target for RHEL and its clones, from recorded data."""

from datetime import date

import pytest

from distropack_builder import targets


@pytest.mark.usefixtures('recorded_http')
def test_el_releases_follow_rocky() -> None:
    """RHEL's majors that Rocky Linux has, released when both are, until RHEL's end of life."""
    el = targets.target('el')
    assert (el.id, el.name, el.os_release_ids) == (
        'el',
        'Enterprise Linux',
        ('rhel', 'almalinux', 'rocky', 'ol'),
    )
    releases = {str(r.version): r for r in el.releases(date(2026, 11, 5))}
    assert sorted(releases, key=int) == ['8', '9', '10']  # no RHEL 7: Rocky has none
    ten = releases['10']
    assert (ten.release_date, ten.eol_date) == (date(2025, 6, 11), date(2035, 5, 31))
    assert str(ten) == 'Enterprise Linux 10'
    assert not ten.is_released(date(2025, 6, 1))  # RHEL 10 out, Rocky 10 not yet
