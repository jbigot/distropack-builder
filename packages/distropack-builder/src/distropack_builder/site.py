# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The site: every channel's repositories in one tree, browsable, within a size limit.

`distropack-build site` publishes the repositories from the states under an output directory, as
`run --publish-only` would, copies extra trees beside them, writes the site's index page, a
directory listing into every directory without an `index.html`, and measures the tree: a hosting
service limits its size (GitHub Pages, 1 GB).
"""

import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import jinja2

from distropack_builder import readme, targets
from distropack_builder.config import BuildConfig

INDEX = 'index.html'
_UNITS = {'': 1, 'K': 1024, 'M': 1024**2, 'G': 1024**3}


def parse_size(text: str) -> int:
    """A size in bytes, from `<n>[K|M|G]`, powers of 1024 as `du -h` counts them.

    Raises:
        ValueError: text is no such size.
    """
    found = re.fullmatch(r'([0-9]+)([KMG]?)', text.strip())
    if found is None:
        raise ValueError(f'{text!r} is no size: write <n>, <n>K, <n>M or <n>G')
    return int(found[1]) * _UNITS[found[2]]


def human(size: int) -> str:
    """size, in the largest unit it reaches, one decimal: `2.0 KiB`, `900.0 MiB`."""
    value = float(size)
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if value < 1024 or unit == 'GiB':
            return f'{size} B' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024
    raise AssertionError  # pragma: no cover


def tree_size(root: Path) -> int:
    """The size of root as a web host serves it: every file whole, its links followed.

    A hard link or a symbolic link is a copy once deployed: each counts its target's size.
    """
    return sum(path.stat().st_size for path in root.rglob('*') if path.is_file())


def size_report(root: Path) -> list[tuple[str, int]]:
    """The size of each top-level and second-level directory of root, by relative path."""
    directories = sorted(
        path
        for depth in ('*', '*/*')
        for path in root.glob(depth)
        if path.is_dir() and not path.is_symlink()
    )
    return [(path.relative_to(root).as_posix(), tree_size(path)) for path in directories]


def check_extra_paths(
    output: Path, extras: Mapping[str, Path], repositories: Sequence[str] = ()
) -> None:
    """Raise ValueError unless every path of extras names a directory inside output.

    It may neither be, nor hold, nor be inside one of the repositories' paths: an extra tree never
    replaces a file of a signed and guarded repository.
    """
    root = output.resolve()
    for path in extras:
        target = (output / path).resolve()
        if Path(path).is_absolute() or target == root or not target.is_relative_to(root):
            raise ValueError(f'--extra {path}: it must name a directory inside the site')
        for repository in repositories:
            published = (output / repository).resolve()
            if target.is_relative_to(published) or published.is_relative_to(target):
                raise ValueError(
                    f'--extra {path}: it overlaps the repository {repository}, which the site'
                    ' publishes itself'
                )


def copy_extras(output: Path, extras: Mapping[str, Path]) -> None:
    """Copy each extra tree as is (modes, times, links) to its path under output.

    Raises:
        ValueError: a path is not a directory inside the site.
    """
    check_extra_paths(output, extras)
    for path, source in extras.items():
        shutil.copytree(source, output / path, symlinks=True, dirs_exist_ok=True)


@dataclass(frozen=True)
class Entry:
    """A line of a listing: a directory (its href ends with `/`) or a file."""

    href: str
    size: str
    date: str


@dataclass(frozen=True)
class Listing:
    """A directory's listing: its path from the site's root, whether it has a parent, entries."""

    path: str
    parent: bool
    entries: tuple[Entry, ...]


def _date(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime('%Y-%m-%d %H:%M')


def write_listings(root: Path) -> None:
    """Write a listing as index.html into every directory of root that has none.

    Directories first, then files, each sorted by name; sizes and UTC dates; relative links.
    """
    template = jinja2.Environment(
        loader=jinja2.PackageLoader('distropack_builder', 'templates'),
        autoescape=True,
        keep_trailing_newline=True,
    ).get_template('listing.html.j2')
    directories = [root, *sorted(p for p in root.rglob('*') if p.is_dir() and not p.is_symlink())]
    missing = [d for d in directories if not (d / INDEX).exists()]
    for directory in missing:
        children = sorted(directory.iterdir(), key=lambda p: p.name)
        # percent-encoded and after `./`, so that no name reads as a fragment or a scheme
        entries = [
            Entry(f'./{quote(child.name)}/', '', _date(child))
            for child in children
            if child.is_dir()
        ] + [
            Entry(f'./{quote(child.name)}', human(child.stat().st_size), _date(child))
            for child in children
            if child.is_file() and child.name != INDEX
        ]
        relative = directory.relative_to(root).as_posix()
        listing = Listing(
            '/' if relative == '.' else f'/{relative}/', directory != root, tuple(entries)
        )
        (directory / INDEX).write_text(template.render(listing=listing))


@dataclass(frozen=True)
class SiteRepository:
    """A repository of the site, as its index page links it."""

    label: str
    """The distribution's name, and the channel of an rpm snapshot channel's repository."""
    path: str
    """Its directory, relative to the site's root."""


@dataclass(frozen=True)
class SiteIndex:
    """The site's index page: its title and its repositories."""

    title: str
    repositories: tuple[SiteRepository, ...]


def write_index(output: Path, project_dir: Path | None, index: SiteIndex) -> None:
    """Write output/index.html from `index.html.j2`, the project's (its README.tpl/) or the default.

    A project's template may extend the default as `distropack/index.html.j2`, and override its
    blocks `title`, `intro` and `repositories`.
    """
    page = readme.environment(project_dir).get_template('index.html.j2').render(site=index)
    (output / INDEX).write_text(page)


def index_of(config: BuildConfig, output: Path) -> SiteIndex:
    """The index of the site at output: the repositories of config it holds."""
    repositories = []
    for dist_id, distrib in config.distribs.items():
        name = targets.target(dist_id).name
        found = [(name, distrib.repository.path)]
        if not targets.is_deb(targets.target(dist_id)):
            found += [
                (f'{name}, channel {channel}', config.channel_repository(dist_id, channel).path)
                for channel in config.snapshot_channels()
                if config.channel_packages(channel)
            ]
        repositories += [
            SiteRepository(label, path.as_posix())
            for label, path in found
            if (output / path).is_dir()
        ]
    return SiteIndex(config.root.name, tuple(repositories))
