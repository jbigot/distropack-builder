# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The page of a repository: how to install from it, for each of its releases.

The page is `README.html.j2`, a Jinja template: the project's own, from its `README.tpl/`
directory, or else the default one. A project's template may extend the default as
`distropack/README.html.j2`, and override its blocks: `title`, `intro`, `install`, and `release`,
the instructions of one release.
"""

import traceback
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import jinja2

from distro_releases import Release
from distropack_builder import targets
from distropack_builder.config import ExtraRepository, expand

TEMPLATE = 'README.html.j2'


class TemplateError(Exception):
    """A page template that cannot be compiled or rendered; its message names the file."""


_MANAGERS: dict[targets.Family, Literal['apt', 'dnf', 'zypper']] = {
    'deb': 'apt',
    'rpm-dnf': 'dnf',
    'rpm-zypper': 'zypper',
}


@dataclass(frozen=True)
class PageExtra:
    """A repository of other packages the release's packages depend on, to add first."""

    name: str
    """Its short name, in its keyring and configuration file names."""
    url: str
    """Its URL for this release."""
    suite: str
    """Its suite for this release, for deb."""
    components: tuple[str, ...]
    """Its components, for deb."""
    key_url: str | None
    """The URL of its key, if build.conf gives one."""
    key_file: str
    """The file name to save the key as: its URL's, whose extension tells apt how to read it."""
    fingerprint: str
    """The fingerprint of its key, for its users to check."""


@dataclass(frozen=True)
class PageRelease:
    """A release of the repository, as its page lists it."""

    label: str
    """The release's full name (`Debian GNU/Linux 13 (Trixie)`)."""
    codename: str | None
    """Its codename, the suite of its deb repository; None where the distribution has none."""
    suite: str | None
    """Its suite (`stable`), None if it has none."""
    version: str | None
    """Its version, None for a release that has none yet (Debian's sid)."""
    url: str
    """The URL of its repository: the whole repository for deb, its own directory for rpm."""
    repo_file: str | None
    """The URL of its .repo file, for rpm; None for deb."""
    prerequisites: tuple[tuple[str, str], ...] = ()
    """What a host must enable first, per host family: (host family, shell commands) pairs."""
    extras: tuple[PageExtra, ...] = ()
    """The repositories of other packages to add first."""


@dataclass(frozen=True)
class Page:
    """A repository, as its page describes it."""

    distribution_id: str
    """The distribution's id (`debian`)."""
    distribution_name: str
    """Its name (`Debian GNU/Linux`)."""
    family: Literal['deb', 'rpm']
    """The packaging family."""
    repository_name: str
    """The repository's short name, in its keyring and .repo file names."""
    description: str
    """The repository's label."""
    url: str
    """The URL of the repository, without a trailing slash."""
    key_url: str
    """The URL of the public key that signs the repository."""
    releases: tuple[PageRelease, ...]
    """Its releases, sorted by name."""
    package_manager: Literal['apt', 'dnf', 'zypper'] = 'apt'
    """What installs from it on its hosts."""

    @classmethod
    def of(
        cls,
        family: Literal['deb', 'rpm'],
        url: str,
        name: str,
        description: str,
        releases: Iterable[Release],
        extras: Sequence[tuple[ExtraRepository, str]] = (),
    ) -> 'Page':
        """The page of a repository of family at url, for its releases, of one distribution.

        extras are the repositories of other packages its builds use, with their keys'
        fingerprints, which each release's instructions add first.
        """
        url = url.rstrip('/')
        listed = sorted(releases, key=str)
        if family == 'deb':
            key_url = f'{url}/{name}-archive-keyring.gpg'
            rows = [_release(r, url, None, extras) for r in listed]
        else:
            key_url = f'{url}/{name}.key'
            rows = [
                _release(r, f'{url}/{r.version}', f'{url}/{r.version}/{name}.repo', extras)
                for r in listed
            ]
        distribution = listed[0].distribution
        return cls(
            distribution.id,
            distribution.name,
            family,
            name,
            description,
            url,
            key_url,
            tuple(rows),
            _MANAGERS[targets.family(distribution)],
        )


def _release(
    release: Release,
    url: str,
    repo_file: str | None,
    extras: Sequence[tuple[ExtraRepository, str]],
) -> PageRelease:
    """A release of a page."""
    target = targets.TARGETS.get(release.distribution.id)
    prerequisites = () if target is None else target.prerequisites(release.version or '')
    return PageRelease(
        str(release),
        release.codename,
        release.suite,
        release.version,
        url,
        repo_file,
        prerequisites,
        tuple(
            PageExtra(
                extra.name or f'extra-{index}',
                expand(extra.url, release),
                expand(extra.suite, release),
                extra.components,
                extra.key_url,
                extra.key_url.rsplit('/', 1)[-1] if extra.key_url else f'{extra.name}.asc',
                fingerprint,
            )
            for index, (extra, fingerprint) in enumerate(extras)
        ),
    )


# What check renders a template with, to find a variable it misspells.
_SAMPLES = (
    Page(
        'debian',
        'Debian GNU/Linux',
        'deb',
        'example',
        'Example',
        'https://example.org/debian',
        'https://example.org/debian/example-archive-keyring.gpg',
        (
            PageRelease(
                'Debian GNU/Linux 13 (Trixie)',
                'trixie',
                'stable',
                '13',
                'https://example.org/debian',
                None,
            ),
        ),
    ),
    Page(
        'fedora',
        'Fedora Linux',
        'rpm',
        'example',
        'Example',
        'https://example.org/fedora',
        'https://example.org/fedora/example.key',
        (
            PageRelease(
                'Fedora Linux 44',
                None,
                None,
                '44',
                'https://example.org/fedora/44',
                'https://example.org/fedora/44/example.repo',
            ),
        ),
    ),
)


def project_dir(root: Path) -> Path | None:
    """The directory of a project's templates, README.tpl beside build.conf, if it has one."""
    directory = root / 'README.tpl'
    return directory if directory.is_dir() else None


def environment(project_dir: Path | None) -> jinja2.Environment:
    """The templates of the project's directory, if any, then the default ones.

    The default ones are also reachable under `distropack/`, for a project's template to extend.
    """
    default = jinja2.PackageLoader('distropack_builder', 'templates')
    loaders: list[jinja2.BaseLoader] = [default, jinja2.PrefixLoader({'distropack': default})]
    if project_dir is not None:
        loaders.insert(0, jinja2.FileSystemLoader(project_dir))
    return jinja2.Environment(
        loader=jinja2.ChoiceLoader(loaders),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


def render(env: jinja2.Environment, page: Page) -> str:
    """The page, rendered from env's README.html.j2.

    Raises:
        TemplateError: the template cannot be compiled or rendered.
    """
    try:
        return env.get_template(TEMPLATE).render(page=page)
    except jinja2.TemplateSyntaxError as error:
        where = error.filename or error.name or TEMPLATE
        raise TemplateError(f'{where}: line {error.lineno}: {error.message}') from error
    except jinja2.TemplateError as error:
        raise TemplateError(f'{_where(error)}: {error.message}') from error


def _where(error: jinja2.TemplateError) -> str:
    """The template file, and line, the error was raised in, as its traceback says."""
    frames = [
        frame
        for frame in traceback.extract_tb(error.__traceback__)
        if frame.filename.endswith('.j2')
    ]
    if not frames:
        return TEMPLATE
    return f'{frames[-1].filename}: line {frames[-1].lineno}'


def check(project_dir: Path | None) -> None:
    """Render the page of each family with sample data, so that a broken template fails early.

    Raises:
        TemplateError: the template cannot be compiled, or does not render.
    """
    env = environment(project_dir)
    for page in _SAMPLES:
        render(env, page)
