# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The repository pages, rendered from the default template or a project's."""

from datetime import date
from pathlib import Path

import pytest
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.single_file import SingleFileSnapshotExtension, WriteMode

from distro_releases import Release, distribution
from distropack_builder import readme, targets
from distropack_builder.config import ExtraRepository
from distropack_builder.readme import Page, PageRelease, TemplateError

DEB = Page(
    distribution_id='debian',
    distribution_name='Debian GNU/Linux',
    family='deb',
    repository_name='hello',
    description='Hello test',
    url='https://repo.example.org/debian',
    key_url='https://repo.example.org/debian/hello-archive-keyring.gpg',
    releases=(
        PageRelease(
            label='Debian GNU/Linux 12 (Bookworm)',
            codename='bookworm',
            suite='oldstable',
            version='12',
            url='https://repo.example.org/debian',
            repo_file=None,
        ),
        PageRelease(
            label='Debian GNU/Linux unstable (Sid)',
            codename='sid',
            suite='unstable',
            version=None,
            url='https://repo.example.org/debian',
            repo_file=None,
        ),
    ),
)
RPM = Page(
    distribution_id='fedora',
    distribution_name='Fedora Linux',
    family='rpm',
    repository_name='hello',
    description='Hello test',
    url='https://repo.example.org/fedora',
    key_url='https://repo.example.org/fedora/hello.key',
    releases=(
        PageRelease(
            label='Fedora Linux 44',
            codename=None,
            suite=None,
            version='44',
            url='https://repo.example.org/fedora/44',
            repo_file='https://repo.example.org/fedora/44/hello.repo',
        ),
    ),
)


class HTMLSnapshot(SingleFileSnapshotExtension):
    """One HTML file per snapshot."""

    _write_mode = WriteMode.TEXT
    file_extension = 'html'


@pytest.mark.parametrize('page', [DEB, RPM], ids=['deb', 'rpm'])
def test_default_page(page: Page, snapshot: SnapshotAssertion) -> None:
    html = readme.render(readme.environment(None), page)
    for release in page.releases:
        assert release.label in html
        assert release.url in html
    if page.family == 'deb':
        assert 'Suites: bookworm' in html and 'Signed-By:' in html
    else:
        assert 'https://repo.example.org/fedora/44/hello.repo' in html
    assert page.key_url in html
    assert 'None' not in html
    assert html == snapshot.use_extension(HTMLSnapshot)


def write_template(tmp_path: Path, text: str) -> Path:
    project = tmp_path / 'README.tpl'
    project.mkdir()
    (project / 'README.html.j2').write_text(text)
    return project


def test_project_override_extends(tmp_path: Path) -> None:
    project = write_template(
        tmp_path,
        '{% extends "distropack/README.html.j2" %}'
        '{% block intro %}<p>Our own words</p>{% endblock %}',
    )
    html = readme.render(readme.environment(project), DEB)
    assert '<p>Our own words</p>' in html
    assert 'Suites: sid' in html


def test_project_override_replaces(tmp_path: Path) -> None:
    project = write_template(
        tmp_path, '{% for r in page.releases %}{{ r.codename }} {% endfor %}{{ page.url }}\n'
    )
    assert readme.render(readme.environment(project), DEB) == (
        'bookworm sid https://repo.example.org/debian\n'
    )


def test_autoescape() -> None:
    page = Page(**{**DEB.__dict__, 'description': '<b>&'})
    html = readme.render(readme.environment(None), page)
    assert '&lt;b&gt;&amp;' in html
    assert '<b>&' not in html


def test_check_default() -> None:
    readme.check(None)


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        pytest.param('{% block intro %}\n{{ page.url }\n', 'line 2', id='syntax'),
        pytest.param('<p>{{ page.nosuch }}</p>\n', 'nosuch', id='undefined'),
        pytest.param('{% extends "distropack/nosuch.j2" %}\n', 'nosuch.j2', id='missing-base'),
    ],
)
def test_check_broken(tmp_path: Path, text: str, expected: str) -> None:
    project = write_template(tmp_path, text)
    with pytest.raises(TemplateError) as caught:
        readme.check(project)
    message = str(caught.value)
    assert 'README.tpl/README.html.j2' in message
    assert expected in message
    assert '\n' not in message


def test_check_project_without_template(tmp_path: Path) -> None:
    """A README.tpl directory without README.html.j2 (another site's files) takes the default."""
    project = tmp_path / 'README.tpl'
    project.mkdir()
    (project / 'index.html').write_text('<p>the site root</p>')
    readme.check(project)
    assert 'Suites: bookworm' in readme.render(readme.environment(project), DEB)


def el_release(major: str) -> Release:
    """A release of Enterprise Linux, made by hand."""
    return Release(
        targets.EL, major, None, major, int(major), None, None, date(2025, 6, 11), None, None
    )


def test_el_prerequisites_per_host_family() -> None:
    page = readme.Page.of('rpm', 'https://x/el', 'ex', 'Ex', [el_release('10'), el_release('8')])
    ten = {r.version: r for r in page.releases}['10']
    hosts = dict(ten.prerequisites)
    assert list(hosts) == ['Rocky Linux and AlmaLinux', 'Red Hat Enterprise Linux', 'Oracle Linux']
    assert 'dnf config-manager --set-enabled crb' in hosts['Rocky Linux and AlmaLinux']
    assert 'epel-release' in hosts['Rocky Linux and AlmaLinux']
    assert 'codeready-builder-for-rhel-10-$(arch)-rpms' in hosts['Red Hat Enterprise Linux']
    assert 'epel-release-latest-10.noarch.rpm' in hosts['Red Hat Enterprise Linux']
    assert 'ol10_codeready_builder' in hosts['Oracle Linux']
    assert 'oracle-epel-release-el10' in hosts['Oracle Linux']
    eight = dict({r.version: r for r in page.releases}['8'].prerequisites)
    assert 'dnf config-manager --set-enabled powertools' in eight['Rocky Linux and AlmaLinux']


def test_el_page_shows_the_prerequisites() -> None:
    page = readme.Page.of('rpm', 'https://x/el', 'ex', 'Ex', [el_release('10')])
    html = readme.render(readme.environment(None), page)
    assert 'On Oracle Linux' in html and 'ol10_codeready_builder' in html


def test_fedora_has_no_prerequisites() -> None:
    fedora = Release(
        distribution('fedora'), '44', None, '44', 44, None, None, date(2026, 4, 1), None, None
    )
    page = readme.Page.of('rpm', 'https://x/fedora', 'ex', 'Ex', [fedora])
    assert page.releases[0].prerequisites == ()


def test_opensuse_page_uses_zypper() -> None:
    leap = Release(
        distribution('opensuse'), '16.0', None, '16.0', 0, None, None, date(2025, 10, 1), None, None
    )
    page = readme.Page.of('rpm', 'https://x/opensuse', 'ex', 'Ex', [leap])
    assert page.package_manager == 'zypper'
    html = readme.render(readme.environment(None), page)
    assert 'sudo zypper addrepo https://x/opensuse/16.0/ex.repo' in html
    assert 'sudo zypper --gpg-auto-import-keys refresh' in html
    assert 'dnf' not in html


def test_extra_repository_listed_first() -> None:
    trixie = Release(
        distribution('debian'), '13', 'trixie', '13 (Trixie)', 13, 'stable', None, None, None, None
    )
    extra = ExtraRepository(
        url='https://repo.pdi.dev/debian/',
        key=Path('pdi.asc'),
        key_url='https://repo.pdi.dev/debian/pdidev-archive-keyring.gpg',
        name='pdi',
    )
    page = readme.Page.of('deb', 'https://x/debian', 'ex', 'Ex', [trixie], [(extra, 'ABCD1234')])
    (release,) = page.releases
    assert [(e.name, e.url, e.suite, e.fingerprint) for e in release.extras] == [
        ('pdi', 'https://repo.pdi.dev/debian/', 'trixie', 'ABCD1234')
    ]
    html = readme.render(readme.environment(None), page)
    first, own = html.index('/etc/apt/sources.list.d/pdi.sources'), html.index('ex.sources')
    assert first < own
    assert 'URIs: https://repo.pdi.dev/debian/' in html and 'ABCD1234' in html


def test_extra_key_saved_under_its_url_name() -> None:
    trixie = Release(
        distribution('debian'), '13', 'trixie', '13 (Trixie)', 13, 'stable', None, None, None, None
    )
    extra = ExtraRepository(
        url='https://repo.pdi.dev/debian/',
        key=Path('pdi.gpg'),
        key_url='https://repo.pdi.dev/debian/pdidev-archive-keyring.gpg',
        name='pdi',
    )
    page = readme.Page.of('deb', 'https://x/debian', 'ex', 'Ex', [trixie], [(extra, 'ABCD')])
    html = readme.render(readme.environment(None), page)
    assert '-o /usr/share/keyrings/pdidev-archive-keyring.gpg https://repo.pdi.dev/' in html
    assert 'Signed-By: /usr/share/keyrings/pdidev-archive-keyring.gpg' in html
