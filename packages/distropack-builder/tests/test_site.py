# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The site: directory listings, extra trees, its size and the report of what weighs."""

import os
import re
from pathlib import Path

import pytest
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.single_file import SingleFileSnapshotExtension, WriteMode

from distropack_builder import site

WHEN = 1_798_000_000  # 2026-12-23, fixed so that the listings are reproducible


class HTMLSnapshot(SingleFileSnapshotExtension):
    _write_mode = WriteMode.TEXT
    file_extension = 'html'


def tree(root: Path) -> Path:
    """repositories/debian/{dists/trixie/Release, pool/a.deb}, repositories/fedora/44/x.rpm."""
    files = {
        'repositories/debian/dists/trixie/Release': b'Release\n',
        'repositories/debian/pool/main/a.deb': b'x' * 2048,
        'repositories/fedora/44/x.rpm': b'y' * 3000,
        'repositories/fedora/index.html': b'<p>the page</p>\n',
    }
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for path in [root, *root.rglob('*')]:
        os.utime(path, (WHEN, WHEN))
    return root


def test_listings_only_where_missing(tmp_path: Path) -> None:
    root = tree(tmp_path / 'site')
    site.write_listings(root)
    assert (root / 'fedora' / 'index.html').exists() is False
    assert (root / 'repositories' / 'fedora' / 'index.html').read_bytes() == b'<p>the page</p>\n'
    for directory in ('', 'repositories', 'repositories/debian', 'repositories/debian/pool/main'):
        assert (root / directory / 'index.html').is_file(), directory
    # the listing of a directory with a page lists nothing new for it, but its subdirectories do
    assert (root / 'repositories' / 'fedora' / '44' / 'index.html').is_file()


def test_listing_links_are_relative(tmp_path: Path) -> None:
    root = tree(tmp_path / 'site')
    site.write_listings(root)
    html = (root / 'repositories' / 'debian' / 'index.html').read_text()
    links = re.findall(r'href="([^"]*)"', html)
    assert links == ['../', 'dists/', 'pool/']
    pool = (root / 'repositories' / 'debian' / 'pool' / 'main' / 'index.html').read_text()
    assert re.findall(r'href="([^"]*)"', pool) == ['../', 'a.deb']
    assert '2.0 KiB' in pool and '2026-12-23' in pool
    top = (root / 'index.html').read_text()
    assert re.findall(r'href="([^"]*)"', top) == ['repositories/']


def test_listings_deterministic(tmp_path: Path, snapshot: SnapshotAssertion) -> None:
    root = tree(tmp_path / 'site')
    site.write_listings(root)
    html = (root / 'repositories' / 'debian' / 'pool' / 'main' / 'index.html').read_text()
    assert html == snapshot.use_extension(HTMLSnapshot)


def test_tree_size_counts_hard_links_once(tmp_path: Path) -> None:
    root = tree(tmp_path / 'site')
    os.link(root / 'repositories/fedora/44/x.rpm', root / 'repositories/fedora/44/y.rpm')
    assert site.tree_size(root) == 8 + 2048 + 3000 + 16


def test_size_report(tmp_path: Path) -> None:
    root = tree(tmp_path / 'site')
    assert site.size_report(root) == [
        ('repositories', 8 + 2048 + 3000 + 16),
        ('repositories/debian', 8 + 2048),
        ('repositories/fedora', 3000 + 16),
    ]


@pytest.mark.parametrize(
    ('text', 'size'),
    [('900M', 900 * 1024**2), ('1G', 1024**3), ('512K', 512 * 1024), ('100', 100)],
)
def test_parse_size(text: str, size: int) -> None:
    assert site.parse_size(text) == size


@pytest.mark.parametrize('text', ['x', '1T', '-1', '1.5G', ''])
def test_parse_size_refuses(text: str) -> None:
    with pytest.raises(ValueError, match='size'):
        site.parse_size(text)


def test_extra_copied_as_is(tmp_path: Path) -> None:
    extra = tmp_path / 'legacy'
    (extra / 'dists').mkdir(parents=True)
    (extra / 'dists' / 'Release').write_text('old\n')
    (extra / 'run.sh').write_text('#!/bin/sh\n')
    (extra / 'run.sh').chmod(0o755)
    output = tmp_path / 'site'
    output.mkdir()
    site.copy_extras(output, {'pdi-main.debian': extra})
    assert (output / 'pdi-main.debian' / 'dists' / 'Release').read_text() == 'old\n'
    assert (output / 'pdi-main.debian' / 'run.sh').stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize('path', ['../out', '/abs', '.', 'a/../..'])
def test_extra_outside_output_refused(tmp_path: Path, path: str) -> None:
    extra = tmp_path / 'legacy'
    extra.mkdir()
    output = tmp_path / 'site'
    output.mkdir()
    with pytest.raises(ValueError, match='inside the site'):
        site.copy_extras(output, {path: extra})


INDEX_SITE = site.SiteIndex(
    'Example',
    (
        site.SiteRepository('Debian GNU/Linux', 'debian'),
        site.SiteRepository('Fedora Linux, channel weekly', 'weekly.fedora'),
    ),
)


def test_index_page_default(tmp_path: Path) -> None:
    site.write_index(tmp_path, None, INDEX_SITE)
    html = (tmp_path / 'index.html').read_text()
    assert '<a href="debian/">Debian GNU/Linux</a>' in html
    assert '<a href="weekly.fedora/">Fedora Linux, channel weekly</a>' in html


def test_index_page_override(tmp_path: Path) -> None:
    project = tmp_path / 'README.tpl'
    project.mkdir()
    (project / 'index.html.j2').write_text(
        '{% extends "distropack/index.html.j2" %}{% block intro %}<p>Ours</p>{% endblock %}'
    )
    output = tmp_path / 'site'
    output.mkdir()
    site.write_index(output, project, INDEX_SITE)
    html = (output / 'index.html').read_text()
    assert '<p>Ours</p>' in html and '<a href="debian/">' in html
