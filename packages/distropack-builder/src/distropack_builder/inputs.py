# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The download phase of a channel: everything its builds need, before any build starts.

`fetch` gathers it for `distropack-build fetch`, which a workflow runs once for all its jobs, and
for `run`, which finds there what an earlier `fetch` left: a snapshot channel's commits, resolved
and checked once and recorded in ``snapshots.json``; the deb upstream tarballs (uscan's for the
release channel, a snapshot's tarball named by mk-origtargz otherwise); the specs, rewritten for a
snapshot, as each release's rpm reads them; and every file their ``Source`` lines download.
"""

import re
from collections.abc import Mapping, Set
from dataclasses import dataclass
from pathlib import Path

from debian.changelog import Changelog

from distro_releases import Release
from distropack_builder import rpmquery, snapshots, targets
from distropack_builder.bake import PROTOCOL, REGISTRY
from distropack_builder.config import BuildConfig
from distropack_builder.context import BuildContext
from distropack_builder.docker import ContainerFailed
from distropack_builder.downloads import DownloadFailed, fetch_all
from distropack_builder.rpmquery import SpecFailed, SpecInfo
from distropack_builder.snapshots import Record, Records, SnapshotFailed
from distropack_builder.uscan import OrigFiles, fetch_origs, snapshot_orig

# The files a build needs downloaded: each URL's file, or why it could not be.
Files = Mapping[str, Path | DownloadFailed]

SNAPSHOTS_FILE = 'snapshots.json'

# A spec's Release and Version lines, its %changelog line, and the date and author of a changelog
# entry, the date in the short (`Thu Nov 05 2026`) or the long form (`Thu Nov 05 12:00:00 UTC
# 2026`).
RELEASE_LINE = re.compile(r'^(?P<tag>Release\s*:\s*)(?P<value>\S.*?)[ \t]*$', re.I | re.M)
VERSION_LINE = re.compile(r'^(?P<tag>Version\s*:\s*)\S.*?[ \t]*$', re.I | re.M)
CHANGELOG_LINE = re.compile(r'^%changelog[ \t]*\n', re.MULTILINE)
ENTRY_DATE = re.compile(
    r'\*\s+(?P<date>\w{3}\s+\w{3}\s+\d{1,2}(?:\s+\d\d:\d\d:\d\d(?:\s+\S+)?)?\s+\d{4})\s'
)
_ENTRY_AUTHOR = re.compile(r'(?P<author>[^\n]*?)(?:\s+-\s+\S+)?[ \t]*$', re.MULTILINE)


def builder_image(dist_release: Release) -> str:
    """The builder image of a release, relative to REGISTRY, as the bake definition tags it.

    The tag is the release's codename in the Debian family, its version elsewhere.
    """
    if targets.is_deb(dist_release.distribution):
        return f'{dist_release.distribution.id}_builder:{dist_release.codename}-p{PROTOCOL}'
    return f'{dist_release.distribution.id}_builder:{dist_release.version}-p{PROTOCOL}'


def source_format(debian_dir: Path) -> str:
    """The format of a deb source package, as debian/source/format says, 1.0 without it."""
    try:
        return (debian_dir / 'source' / 'format').read_text().strip()
    except FileNotFoundError:
        return '1.0'


def spec_file(packaging: Path, name: str) -> Path:
    """The spec of package name in its packaging directory."""
    return packaging / f'{name}.spec'


@dataclass(frozen=True)
class Inputs:
    """What the builds of a channel need, gathered before any starts.

    Attributes:
        snapshots: the commit of each package of a snapshot channel, or why it has none; empty for
            the release channel.
        origs: the upstream tarball of each 3.0 (quilt) deb source built, by package.
        specs: what each release's rpm reads in each spec built, by package and release uid.
        built_specs: the spec each rpm builds, by package: its packaging's, or for a snapshot the
            one rewritten for its commit.
        files: the files of the specs' Source lines, by URL, but a snapshot's own tarball.
    """

    snapshots: Records
    origs: dict[str, OrigFiles | DownloadFailed]
    specs: dict[tuple[str, str], SpecInfo | SpecFailed]
    built_specs: dict[str, Path]
    files: Files


def snapshot_spec(spec: str, version: str, record: Record) -> str:
    """A spec rewritten to build a snapshot: its Version, its commit, and why, in %changelog.

    The commit is the macro `%{commit}`, defined first, so that the spec can unpack the snapshot
    tarball's `<name>-<commit>` directory (`%autosetup -n <name>-%{?commit}%{!?commit:%{version}}`).
    The changelog entry takes its author and date from the spec's top entry, so that the same
    inputs give the same spec; a spec without %changelog, or whose first entry has no date, gets
    none.
    """
    spec = (
        '%global commit '
        + record.commit
        + '\n'
        + VERSION_LINE.sub(lambda line: line['tag'] + version, spec)
    )
    changelog = CHANGELOG_LINE.search(spec)
    release = RELEASE_LINE.search(spec)
    if changelog is None or release is None:
        return spec
    rest = spec[changelog.end() :].lstrip()
    top = ENTRY_DATE.match(rest)
    if top is None:
        return spec
    author = _ENTRY_AUTHOR.match(rest, top.end())
    assert author is not None  # it matches the empty string
    entry = (
        f'* {top["date"]} {author["author"].strip()} - {version}-{release["value"]}\n'
        f'- {snapshot_entry(record)}\n\n'
    )
    return spec[: changelog.end()] + entry + spec[changelog.end() :]


def snapshot_entry(record: Record) -> str:
    """What a changelog entry says of the snapshot record names."""
    of = f'of {record.ref} ' if record.ref is not None else ''
    return f'Snapshot {of}at {record.commit}, {record.level}'


def _snapshots(
    config: BuildConfig, channel: str, download_dir: Path, given: Mapping[str, str]
) -> Records:
    """The records of a snapshot channel: those download_dir holds, else resolved and written."""
    path = download_dir / SNAPSHOTS_FILE
    wanted = set(config.channel_packages(channel))
    if not given:
        recorded = snapshots.read(path, channel)
        if recorded is not None and wanted <= set(recorded):
            return {name: recorded[name] for name in config.channel_packages(channel)}
    records = snapshots.resolve(config, channel, download_dir / 'git', given)
    snapshots.write(path, channel, records)
    return records


def fetch(
    context: BuildContext,
    config: BuildConfig,
    channel: str,
    selected: Mapping[str, Set[Release]],
    disabled: Mapping[str, Set[Release]],
    download_dir: Path,
    given: Mapping[str, str],
) -> Inputs:
    """Gather the inputs of the builds of channel for the releases selected, in download_dir.

    A snapshot channel's commits are those download_dir's snapshots.json records for it, unless
    commits are given; they are resolved, checked and recorded otherwise. A package whose snapshot
    failed fails its tarball and specs with the same reason.
    """
    root = config.root
    release_channel = channel == config.release_channel
    records = {} if release_channel else _snapshots(config, channel, download_dir, given)
    packages = config.channel_packages(channel)
    packaging = {name: config.packaging_dir(name, channel) for name in packages}

    def releases_of(name: str, deb: bool) -> list[Release]:
        return sorted(
            (
                release
                for dist_id, releases in selected.items()
                if targets.is_deb(targets.target(dist_id)) == deb
                for release in releases
                if release not in disabled[name]
            ),
            key=lambda release: release.uid,
        )

    quilt = [
        name
        for name in packages
        if (packaging[name] / 'debian').is_dir()
        and source_format(packaging[name] / 'debian') == '3.0 (quilt)'
        and releases_of(name, deb=True)
    ]
    origs: dict[str, OrigFiles | DownloadFailed] = {}
    if release_channel:
        origs = fetch_origs(context, root, sorted(quilt), download_dir / 'origs')
    else:
        for name in quilt:
            origs[name] = _snapshot_orig(
                context, config, channel, name, packaging[name], records[name], download_dir
            )

    built_specs: dict[str, Path] = {}
    specs: dict[tuple[str, str], SpecInfo | SpecFailed] = {}
    snapshot_sources: set[str] = set()
    rpm_names = [
        name
        for name in packages
        if spec_file(packaging[name], name).is_file() and releases_of(name, deb=False)
    ]
    for name in rpm_names:
        spec = spec_file(packaging[name], name)
        if release_channel:
            built_specs[name] = spec
            continue
        record = records[name]
        if isinstance(record, SnapshotFailed):
            continue
        rewritten = context.workdir / 'specs' / spec.name
        rewritten.parent.mkdir(parents=True, exist_ok=True)
        upstream = record.snapshot().upstream(config.label(channel, name))
        rewritten.write_text(snapshot_spec(spec.read_text(), upstream, record))
        built_specs[name] = rewritten
    by_release: dict[str, tuple[Release, list[str]]] = {}
    for name in rpm_names:
        for release in releases_of(name, deb=False):
            by_release.setdefault(release.uid, (release, []))[1].append(name)
    for uid, (release, names) in sorted(by_release.items()):
        failed = {
            name: failure
            for name in names
            if isinstance(failure := records.get(name), SnapshotFailed)
        }
        for name, failure in failed.items():
            specs[(name, uid)] = SpecFailed(str(failure))
        to_query = {name: built_specs[name] for name in names if name not in failed}
        # every spec of the release, read by the release's own rpm, in one container
        answers = rpmquery.query(context, REGISTRY + builder_image(release), to_query)
        for name, answer in answers.items():
            specs[(name, uid)] = answer
            if isinstance(answer, SpecInfo) and not release_channel and answer.sources:
                snapshot_sources.add(answer.sources[0])

    downloads = download_dir / 'downloads'
    downloads.mkdir(parents=True, exist_ok=True)
    urls = [
        url
        for answer in specs.values()
        if isinstance(answer, SpecInfo)
        for url in answer.sources
        if url not in snapshot_sources
    ]
    files = fetch_all(urls, downloads, jobs=context.jobs, cancellation=context.cancellation)
    return Inputs(records, origs, specs, built_specs, files)


def _snapshot_orig(
    context: BuildContext,
    config: BuildConfig,
    channel: str,
    name: str,
    packaging: Path,
    record: Record | SnapshotFailed,
    download_dir: Path,
) -> OrigFiles | DownloadFailed:
    """The orig tarball of a snapshot of package name, named after its upstream version."""
    if isinstance(record, SnapshotFailed):
        return DownloadFailed(f'the snapshot of {name}', record.reason)
    with (packaging / 'debian' / 'changelog').open() as changelog:
        source = Changelog(changelog, max_blocks=1).package
    upstream = record.snapshot().upstream(config.label(channel, name))
    try:
        tarball = snapshots.tarball(download_dir, name, record)
    except Exception as error:  # noqa: BLE001 -- git's or the archive's failure, as a download's
        return DownloadFailed(f'the snapshot of {name}', str(error))
    try:
        return snapshot_orig(
            context,
            config.root,
            packaging.relative_to(config.root).as_posix(),
            tarball,
            source,
            upstream,
            download_dir / 'origs' / 'snapshots',
        )
    except ContainerFailed as error:
        return DownloadFailed(f'the snapshot of {name}', f'mk-origtargz failed: {error}')
