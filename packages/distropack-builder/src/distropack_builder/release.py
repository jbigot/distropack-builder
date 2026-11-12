# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The release tool: packaging changes pending in a channel, and the release cut from them.

`pending` copies a package's packaging to `<name>-<channel>/`, which the snapshot channel builds
from, with an entry saying its changes wait for the next release. `release` makes those directories
the packaging again, and writes the release's entries. Both edit the packaging repository's working
tree, clean when they start, and commit nothing: the maintainer reviews and commits.

The tool writes its entries itself, as `dch` and `rpmdev-bumpspec` would, so that it needs neither
on the host; a tag names its version with `-` made `~`, so that a release candidate sorts before
its release.
"""

import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path

from debian.changelog import Changelog

from distropack_builder.config import BuildConfig
from distropack_builder.inputs import source_format, spec_file
from distropack_builder.versions import tag_version

PENDING = 'Pending changes for the next release'
_VERSION = re.compile(r'^(?P<tag>Version\s*:\s*)(?P<value>\S.*?)[ \t]*$', re.I | re.M)
_RELEASE = re.compile(r'^(?P<tag>Release\s*:\s*)(?P<value>\S.*?)[ \t]*$', re.I | re.M)
_SOURCE = re.compile(r'^(?P<tag>Source\d*\s*:\s*)(?P<value>\S.*?)[ \t]*$', re.I | re.M)
_CHANGELOG = re.compile(r'^%changelog[ \t]*\n', re.M)


class ReleaseError(Exception):
    """What the release tool refuses to do, before changing anything."""


class UnportedCommits(Exception):
    """Commits changed a package's packaging after its changes were set pending in a channel.

    Attributes:
        commits: the `git log --oneline` lines of each package's directory.
    """

    def __init__(self, channel: str, commits: Mapping[str, Sequence[str]]) -> None:
        """The commits to port, by package, into their channel's directory."""
        self.commits = dict(commits)
        lines = []
        for name, logged in commits.items():
            lines += [f'{name}/ changed since {name}-{channel}/ was made:', *logged]
            lines.append(f'port these into {name}-{channel}/ and rerun with --ported')
        super().__init__('\n'.join(lines))


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ['git', '-C', str(root), *args], capture_output=True, text=True, check=True
    ).stdout


def _check_clean(root: Path) -> None:
    try:
        changes = _git(root, 'status', '--porcelain')
    except subprocess.CalledProcessError as error:
        reason = error.stderr.strip().splitlines()[-1] if error.stderr.strip() else str(error)
        raise ReleaseError(f'{root} is not a git working tree: {reason}') from error
    if changes.strip():
        raise ReleaseError(
            f'{root} has uncommitted changes: commit or stash them first, so that what this'
            ' writes can be reviewed alone'
        )


def _author(config: BuildConfig) -> str:
    if config.release.author is None:
        raise ReleaseError('release.author is not set in build.conf')
    return config.release.author


def _rpm_date(when: datetime) -> str:
    return when.strftime('%a %b %d %Y')


def pending(config: BuildConfig, channel: str, names: Sequence[str], today: datetime) -> None:
    """Copy the packaging of each package named to `<name>-<channel>/`, marked pending.

    Raises:
        ReleaseError: the tree is not clean, channel is not a snapshot channel, a package does not
            build in it, or already has its directory there.
    """
    root = config.root
    _check_clean(root)
    if channel not in config.channels:
        raise ReleaseError(f'unknown channel {channel}')
    if channel == config.release_channel:
        raise ReleaseError(
            f'{channel} is the release channel: changes wait for a release in a snapshot channel'
        )
    author = _author(config)
    for name in names:
        package = config.packages.get(name)
        if package is None or channel not in package.channels:
            raise ReleaseError(f'{name}: it does not build in channel {channel}')
        if (root / f'{name}-{channel}').exists():
            raise ReleaseError(f'{name}-{channel}/ exists already: its changes are pending')
    for name in names:
        target = root / f'{name}-{channel}'
        shutil.copytree(root / name, target, symlinks=True)
        debian = target / 'debian'
        if debian.is_dir():
            _pending_deb(debian / 'changelog', author, today)
        spec = spec_file(target, name)
        if spec.is_file():
            _pending_rpm(spec, author, today)


def _pending_deb(path: Path, author: str, today: datetime) -> None:
    changelog = Changelog(path.read_text())
    changelog.new_block(
        package=changelog.package,
        version=changelog.version,
        distributions='UNRELEASED',
        urgency='medium',
        changes=['', f'  * {PENDING}', ''],
        author=author,
        date=format_datetime(today),
    )
    path.write_text(str(changelog))


def _autospec(spec: str) -> bool:
    """Whether rpmautospec writes the spec's Release and %changelog."""
    return '%autorelease' in spec or '%autochangelog' in spec


def _pending_rpm(path: Path, author: str, today: datetime) -> None:
    spec = path.read_text()
    version, release, changelog = (
        _VERSION.search(spec),
        _RELEASE.search(spec),
        _CHANGELOG.search(spec),
    )
    if _autospec(spec) or version is None or release is None or changelog is None:
        return
    entry = (
        f'* {_rpm_date(today)} {author} - {version["value"]}-{release["value"]}\n- {PENDING}\n\n'
    )
    path.write_text(spec[: changelog.end()] + entry + spec[changelog.end() :])


def _pending_channels(config: BuildConfig) -> dict[str, list[str]]:
    """The snapshot channels with pending directories, and their packages."""
    found: dict[str, list[str]] = {}
    for channel in config.snapshot_channels():
        names = [name for name in config.packages if (config.root / f'{name}-{channel}').is_dir()]
        if names:
            found[channel] = names
    return found


def release(
    config: BuildConfig, version: str, *, from_channel: str | None, ported: bool, now: datetime
) -> None:
    """Make the pending packaging the packaging, and write the release's entries.

    The pending directories of from_channel (by default the only channel that has some) replace
    their package's directory; every package release.packages matches gets an entry for version.

    Raises:
        ReleaseError: the tree is not clean, several channels have pending changes and from_channel
            does not choose, or a pending directory's package is not one released.
        UnportedCommits: a package's directory changed after its pending directory was made, and
            ported does not say these changes are in the pending one.
    """
    root = config.root
    _check_clean(root)
    author = _author(config)
    found = _pending_channels(config)
    if from_channel is None:
        if len(found) > 1:
            raise ReleaseError(
                f'channels {", ".join(found)} have pending changes: choose one with --from'
            )
        from_channel = next(iter(found), None)
    elif from_channel not in config.snapshot_channels():
        raise ReleaseError(f'{from_channel} is not a snapshot channel')
    moved = found.get(from_channel, []) if from_channel is not None else []
    released = config.released_packages()
    for name in moved:
        if name not in released:
            raise ReleaseError(
                f'{name}-{from_channel}/: {name} is not one of release.packages, and would keep'
                ' its UNRELEASED entry'
            )
    if moved and not ported:
        assert from_channel is not None
        unported = {}
        for name in moved:
            added = _git(
                root, 'log', '--diff-filter=A', '--format=%H', '--', f'{name}-{from_channel}/'
            )
            if not added.split():
                continue  # not committed yet: _check_clean refused it
            since = added.split()[-1]
            logged = _git(root, 'log', '--oneline', f'{since}..HEAD', '--', f'{name}/').splitlines()
            if logged:
                unported[name] = logged
        if unported:
            raise UnportedCommits(from_channel, unported)
    for name in moved:
        shutil.rmtree(root / name)
        (root / f'{name}-{from_channel}').rename(root / name)
    upstream = tag_version(version)
    for name in released:
        directory = root / name
        if (directory / 'debian').is_dir():
            _release_deb(directory / 'debian' / 'changelog', version, upstream, config, author, now)
        spec = spec_file(directory, name)
        if spec.is_file():
            _release_rpm(spec, version, upstream, author, now)


def _release_deb(
    path: Path, version: str, upstream: str, config: BuildConfig, author: str, now: datetime
) -> None:
    text = path.read_text()
    changelog = Changelog(text)
    top = changelog[0]
    pending_lines: list[str] = []
    if top.distributions == 'UNRELEASED':
        pending_lines = [line for line in top.changes() if line.strip()]
        # the entry is the block's text: drop it, the release's takes its place
        text = text[len(str(top)) :].lstrip('\n')
    epoch = changelog.epoch
    # a 3.0 (native) source's version has no Debian revision
    revision = '' if source_format(path.parent) == '3.0 (native)' else '-1'
    entry = Changelog()
    entry.new_block(
        package=changelog.package,
        version=f'{epoch}:{upstream}{revision}' if epoch else f'{upstream}{revision}',
        distributions=config.release.distribution,
        urgency='medium',
        changes=['', f'  * Upstream release {version}', *pending_lines, ''],
        author=author,
        date=format_datetime(now),
    )
    path.write_text(str(entry) + text)


def _release_rpm(path: Path, version: str, upstream: str, author: str, now: datetime) -> None:
    spec = path.read_text()
    old = _VERSION.search(spec)
    if old is None:
        return
    url_version = version.removeprefix('v')
    old_forms = {old['value'], old['value'].replace('~', '-')} - {''}
    old_forms = {form for form in old_forms if '%' not in form}

    def source(line: re.Match[str]) -> str:
        url = line['value']
        if url_version != upstream:  # %{version} would name the ~ form, which no URL holds
            url = url.replace('%{version}', url_version)
            for form in old_forms:
                url = url.replace(form, url_version)
        else:
            for form in old_forms:
                url = url.replace(form, '%{version}')
        return line['tag'] + url

    spec = _SOURCE.sub(source, spec)
    spec = _VERSION.sub(lambda line: line['tag'] + upstream, spec)
    if _autospec(spec):
        path.write_text(spec)
        return
    release = _RELEASE.search(spec)
    value = '1%{?dist}' if release is not None and '%{?dist}' in release['value'] else '1'
    spec = _RELEASE.sub(lambda line: line['tag'] + value, spec)
    header = f'* {_rpm_date(now)} {author} - {upstream}-{value}\n- Upstream release {version}\n'
    changelog = _CHANGELOG.search(spec)
    if changelog is None:
        path.write_text(spec)
        return
    rest = spec[changelog.end() :]
    first = re.match(r'\*[^\n]*\n(?P<items>(?:-[^\n]*\n?)*)', rest)
    if first is not None and f'- {PENDING}' in first['items']:
        rest = header + first['items'] + rest[first.end() :]
    else:
        rest = header + '\n' + rest
    path.write_text(spec[: changelog.end()] + rest)
