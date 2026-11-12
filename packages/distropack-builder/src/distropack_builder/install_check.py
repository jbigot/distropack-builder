# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The install check: every repository of a site, installed in a clean container of its release.

For each release and channel, the site is served over HTTP by a sidecar on a network of the check's
own, and a container of the distribution's base image (not the builder's),
on that network, which also routes to the distribution's own mirrors, adds the repository
as the page tells its users, signatures checked, and installs the packages. With a previous URL,
it installs from the published site first, then switches to the new one and upgrades: no package
may be held back nor removed. The containers and the network are removed however the check ends.
"""

import re
import shlex
from collections.abc import Callable, Mapping, Sequence, Set
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from distro_releases import Release
from distropack_builder import published, targets
from distropack_builder.builders import Public
from distropack_builder.config import BuildConfig, Repository
from distropack_builder.context import BuildContext
from distropack_builder.docker import Engine, Mount
from distropack_builder.docker.runner import private_network, serve

SIDECAR = 'docker.io/library/busybox:stable'
PORT = 8080
_MANAGERS: dict[targets.Family, Literal['apt', 'dnf', 'zypper']] = {
    'deb': 'apt',
    'rpm-dnf': 'dnf',
    'rpm-zypper': 'zypper',
}


@dataclass(frozen=True)
class Target:
    """A repository to install from, as one container sees it.

    Attributes:
        manager: what installs.
        name: the repository's short name, in its keyring and configuration file names.
        url: the repository's URL as the container reaches the site.
        release: the release installed.
        components: the deb components to add, the release channel's first; one for rpm.
        previous: the URL of the published repository to install from first, or None.
    """

    manager: Literal['apt', 'dnf', 'zypper']
    name: str
    url: str
    release: Release
    components: tuple[str, ...]
    previous: str | None


@dataclass(frozen=True)
class CheckResult:
    """How the install check of a release and channel ended: whether it passed, its output."""

    passed: bool
    output: str


def _deb_sources(target: Target, url: str) -> str:
    lines = []
    for index, component in enumerate(target.components):
        file = f'{target.name}.sources' if index == 0 else f'{target.name}-{component}.sources'
        body = (
            f'Types: deb\\nURIs: {url}\\nSuites: {target.release.codename}\\n'
            f'Components: {component}\\n'
            f'Signed-By: /usr/share/keyrings/{target.name}-archive-keyring.gpg\\n'
        )
        lines.append(f"printf '{body}' > /etc/apt/sources.list.d/{file}")
    # a source that cannot be fetched is an error, not a warning: the site must be read
    return '\n'.join(lines) + '\napt-get -o APT::Update::Error-Mode=any update\n'


def _repo_file(target: Target, url: str, path: str) -> str:
    body = (
        f'[{target.name}]\\nname={target.name}\\nbaseurl={url}/{target.release.version}\\n'
        f'enabled=1\\ngpgcheck=1\\nrepo_gpgcheck=1\\ngpgkey={url}/{target.name}.key\\n'
    )
    return f"printf '{body}' > {path}\n"


def script(
    target: Target,
    packages: Sequence[str],
    prerequisites: str = '',
    expected: Mapping[str, str] | None = None,
) -> str:
    """The shell script installing packages from target, then upgrading from the site to it.

    Signatures are always checked; an upgrade that would remove a package, or keep one back, fails;
    each package of expected must end installed at its version there (the site's).
    """
    names = ' '.join(shlex.quote(p) for p in packages)
    # the page's commands, for a user; the container runs them as root, without sudo
    commands = re.sub(r'^sudo ', '', prerequisites, flags=re.MULTILINE)
    if 'dnf config-manager' in commands:  # a host has it; a minimal image does not
        commands = "dnf install -y 'dnf-command(config-manager)'\n" + commands
    lines = ['set -e', commands] if commands else ['set -e']
    first = target.previous or target.url
    if target.manager == 'apt':
        lines.append('export DEBIAN_FRONTEND=noninteractive')
        lines.append(f'cp /keyring/{target.name}-archive-keyring.gpg /usr/share/keyrings/')
        lines.append(_deb_sources(target, first))
        lines.append(f'apt-get install -y {names}')
        if target.previous is not None:
            lines.append(_deb_sources(target, target.url))
            lines.append('apt-get -y --no-remove dist-upgrade')
            lines.append('test -z "$(apt list --upgradable 2>/dev/null | grep -v ^Listing)"')
            lines.append(f'dpkg -s {names} > /dev/null')
    elif target.manager == 'dnf':
        path = f'/etc/yum.repos.d/{target.name}.repo'
        lines.append(_repo_file(target, first, path))
        lines.append(f'dnf install -y {names}')
        if target.previous is not None:
            lines.append(_repo_file(target, target.url, path))
            lines.append('dnf upgrade -y --refresh')
            lines.append('dnf check-update')
            lines.append(f'rpm -q {names}')
    else:
        path = f'/tmp/{target.name}.repo'
        lines.append(_repo_file(target, first, path))
        lines.append(f'zypper --non-interactive addrepo {path}')
        lines.append('zypper --non-interactive --gpg-auto-import-keys refresh')
        lines.append(f'zypper --non-interactive install {names}')
        if target.previous is not None:
            lines.append(f'zypper --non-interactive removerepo {target.name}')
            lines.append(_repo_file(target, target.url, path))
            lines.append(f'zypper --non-interactive addrepo {path}')
            lines.append('zypper --non-interactive --gpg-auto-import-keys refresh')
            lines.append('zypper --non-interactive update')
            lines.append('test -z "$(zypper --quiet list-updates | grep \'^v \')"')
            lines.append(f'rpm -q {names}')
    for name, version in sorted((expected or {}).items()):
        if target.manager == 'apt':
            installed = f"dpkg-query -W -f '${{Version}}' {shlex.quote(name)}"
        else:
            evr = '%|EPOCH?{%{EPOCH}:}:{}|%{VERSION}-%{RELEASE}'
            installed = f"rpm -q --qf '{evr}' {shlex.quote(name)}"
        lines.append(f'test "$({installed})" = {shlex.quote(version)}')
    return '\n'.join(lines) + '\n'


def binary_packages(
    repository: Path, family: Literal['deb', 'rpm'], release: Release, component: str
) -> list[str]:
    """The binary packages a repository directory publishes for release (and component)."""
    with httpx.Client() as client:
        found = published.versions(repository, family, release, component, client)
    return sorted({name for name, arch in found if arch not in ('source', 'src', 'nosrc')})


def _held(
    directory: Path, family: Literal['deb', 'rpm'], release: Release, component: str
) -> dict[str, str]:
    """The binary packages a repository directory publishes, at their highest version."""
    with httpx.Client() as client:
        found = published.versions(directory, family, release, component, client)
    held: dict[str, str] = {}
    for (name, arch), versions in found.items():
        if arch in ('source', 'src', 'nosrc'):
            continue
        for version in versions:
            current = held.get(name)
            if current is None or published.compare(family, version.version, current) > 0:
                held[name] = version.version
    return held


def _release_tag(release: Release) -> str:
    if targets.is_deb(release.distribution):
        return release.codename or release.uid
    return release.version or release.codename or release.uid


@dataclass(frozen=True)
class _Check:
    channel: str
    repository: Repository
    components: tuple[str, ...]
    previous: str | None


def _checks(config: BuildConfig, dist_id: str, channels: Set[str] | None) -> list[_Check]:
    """What to check for a distribution: one per channel published, in its repository."""
    deb = targets.is_deb(targets.target(dist_id))
    wanted = [config.release_channel, *config.snapshot_channels()]
    wanted = [c for c in wanted if channels is None or c in channels]
    found = []
    main = config.component(dist_id, config.release_channel)
    for channel in wanted:
        if channel != config.release_channel and not config.channel_packages(channel):
            continue
        if deb:
            components = (
                (main,)
                if channel == config.release_channel
                else (
                    main,
                    config.component(dist_id, channel),
                )
            )
            found.append(_Check(channel, config.distribs[dist_id].repository, components, None))
        else:
            repository = config.channel_repository(dist_id, channel)
            found.append(_Check(channel, repository, ('main',), None))
    return found


def check(
    context: BuildContext,
    config: BuildConfig,
    site: Path,
    selected: Mapping[str, Set[Release]],
    previous: Mapping[str, str],
    channels: Set[str] | None = None,
    *,
    network: Callable[[Engine], AbstractContextManager[str]] | None = None,
    sidecar: Callable[..., AbstractContextManager[None]] | None = None,
) -> dict[tuple[Release, str], CheckResult]:
    """Install every repository of site, release by release and channel by channel.

    previous gives, by distribution id, the published URL to upgrade from; it applies to the
    distribution's own repository (every deb channel, the rpm release channel).
    """
    network = network or private_network
    sidecar = sidecar or serve
    results: dict[tuple[Release, str], CheckResult] = {}
    for dist_id in config.distribs:
        target = targets.TARGETS[dist_id]
        family: Literal['deb', 'rpm'] = 'deb' if target.family == 'deb' else 'rpm'
        for release in sorted(selected.get(dist_id, ()), key=lambda r: r.uid):
            for item in _checks(config, dist_id, channels):
                directory = site / item.repository.path
                component = item.components[-1]
                # what the site holds decides what is checked; check_install, what is installed
                held = _held(directory, family, release, component)
                if not held:
                    continue
                packages = list(config.check_install or ()) or sorted(held)
                if item.repository.url is None:
                    results[(release, item.channel)] = CheckResult(
                        False, f'{directory} has no url: its users have no keyring to install with'
                    )
                    continue
                public = Public.of(
                    item.repository.url, item.repository.name, item.repository.description
                )
                is_main = item.repository is config.distribs[dist_id].repository
                url = f'http://site:{PORT}/{item.repository.path.as_posix()}'
                checked = Target(
                    _MANAGERS[target.family],
                    public.name,
                    url,
                    release,
                    item.components,
                    previous.get(dist_id) if is_main else None,
                )
                prerequisites = target.prerequisites(release.version or '')
                commands = prerequisites[0][1] if prerequisites else ''
                expected = {name: held[name] for name in packages if name in held}
                try:
                    with (
                        network(context.engine) as net,
                        sidecar(
                            context.engine,
                            SIDECAR,
                            ['httpd', '-f', '-p', str(PORT), '-h', '/site'],
                            mounts=[Mount('/site', site, read_only=True)],
                            network=net,
                            alias='site',
                        ),
                    ):
                        result = context.run(
                            context.engine,
                            target.base_image(_release_tag(release)),
                            ['sh', '-c', script(checked, packages, commands, expected)],
                            mounts=[Mount('/keyring', directory, read_only=True)],
                            as_root=True,
                            check=False,
                            cancellation=context.cancellation,
                            network=net,
                        )
                except Exception as error:  # noqa: BLE001 -- the engine's failure fails this row only
                    reason = ' '.join(str(error).split()) or type(error).__name__
                    results[(release, item.channel)] = CheckResult(False, reason)
                    continue
                results[(release, item.channel)] = CheckResult(result.status == 0, result.output)
    return results
