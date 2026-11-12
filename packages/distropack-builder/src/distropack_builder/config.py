# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""build.conf: the distributions to build for, their repositories, and the packages to build.

Every path in it is relative to its own directory. The top-level keys that start with a dot are
ignored: they hold the YAML anchors that the distributions merge (``<<: *distribution``).
"""

import fnmatch
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    WithJsonSchema,
    model_validator,
)
from pydantic_core import PydanticCustomError

from distro_releases import Release, UnknownDistribution
from distropack_builder import signing, sources, targets
from distropack_builder.versions import version_label

# The release token that keeps, among the releases selected, those whose builder image exists.
AVAILABLE = 'available'


class ConfigError(Exception):
    """A build.conf that cannot be read, or that does not describe a build.

    Its message is one line: the file, the dotted path of the setting at fault, and the reason.
    """


def _tokens(value: object) -> object:
    """A list of release tokens, from a list or a single token; numbers become strings."""
    return [_token(item) for item in (value if isinstance(value, list | tuple) else [value])]


def _token(item: object) -> object:
    """A release token: a string, an integer written as a string, anything else as it is."""
    if isinstance(item, float):
        raise PydanticCustomError(
            'version_number',
            'a version written as a number loses its trailing zeros: write it quoted',
        )
    if isinstance(item, int) and not isinstance(item, bool):
        return str(item)
    return item


_TOKEN_SCHEMA = {'anyOf': [{'type': 'string'}, {'type': 'integer'}]}

Tokens = Annotated[
    tuple[str, ...],
    BeforeValidator(_tokens),
    WithJsonSchema({'anyOf': [*_TOKEN_SCHEMA['anyOf'], {'type': 'array', 'items': _TOKEN_SCHEMA}]}),
]


def _not_available(tokens: tuple[str, ...]) -> tuple[str, ...]:
    """The tokens, if none of them is the available filter."""
    if AVAILABLE in tokens:
        raise PydanticCustomError(
            'available_outside_versions', f'{AVAILABLE} is a filter of distribs.*.versions only'
        )
    return tokens


def _target(id: str) -> str:
    """The id, if it names a distribution the builder builds for."""
    try:
        targets.target(id)
    except (targets.NotATarget, UnknownDistribution) as error:
        raise PydanticCustomError('not_a_target', str(error)) from error
    return id


TargetId = Annotated[str, AfterValidator(_target)]


def _not_available_alone(tokens: tuple[str, ...]) -> tuple[str, ...]:
    """The tokens, unless available is the only one: it filters, and would have nothing to."""
    if tokens == (AVAILABLE,):
        raise PydanticCustomError(
            'available_alone',
            f'{AVAILABLE} keeps some of the releases the other tokens select: it needs one',
        )
    return tokens


class _Model(BaseModel):
    """The settings every model shares: frozen, strict about keys, documented by attributes."""

    model_config = ConfigDict(extra='forbid', frozen=True, use_attribute_docstrings=True)


ChannelName = Annotated[str, Field(pattern=r'^[a-z0-9][a-z0-9.-]*$')]
"""A channel's name: what a deb component, a directory suffix and an OCI tag all accept."""


class Channel(_Model):
    """A channel: the release one, built from the packaging, or a snapshot of upstream branches."""

    kind: Literal['release', 'snapshot']
    """``release`` for the one channel built from each package's own packaging, ``snapshot`` for
    one built from upstream git commits."""

    epoch_bump: Annotated[int, Field(ge=0)] = 0
    """Snapshot only: what is added to the changelog's epoch in its deb versions, so that they
    supersede packages installed from elsewhere."""

    version_label: str | None = None
    """Snapshot only: the label of its upstream versions, ``<tag>+<label>.<n>.g<sha>`` (default:
    the ref's name, every character outside ``[A-Za-z0-9]`` turned into ``.``; the channel's name
    for a package pinned by ``commit``)."""

    fallback: str | None = None
    """Snapshot only: the channel whose state provides the dependencies this channel does not
    build (default: the release channel)."""

    @model_validator(mode='after')
    def _snapshot_settings(self) -> Self:
        if self.kind == 'release':
            given = [
                name
                for name in ('epoch_bump', 'version_label', 'fallback')
                if name in self.model_fields_set
            ]
            if given:
                raise PydanticCustomError(
                    'snapshot_only', '{names}: snapshot channels only', {'names': ', '.join(given)}
                )
        return self


class VerifyKeys(_Model):
    """Check that the commit built is signed by one of the keys of a file."""

    keys: Path
    """The file of the OpenPGP public keys allowed to sign it, relative to build.conf's
    directory."""


Verify = Literal['none'] | VerifyKeys


class PackageChannel(_Model):
    """Where a snapshot channel takes a package's upstream source from."""

    git: str
    """The URL of the upstream git repository, used as written."""

    commit: Annotated[str, Field(pattern=r'^[0-9a-f]{40}$')] | None = None
    """The full hash of the commit to build, pinned by the packaging (trusted as it is)."""

    ref: str | None = None
    """The branch whose head to build, or whose commit given to ``fetch --snapshot``."""

    verify: Verify | None = None
    """With ``ref``: ``none``, or ``{keys: <file>}`` for a commit signed by one of its keys."""

    @model_validator(mode='after')
    def _commit_or_ref(self) -> Self:
        if (self.commit is None) == (self.ref is None):
            raise PydanticCustomError('commit_or_ref', 'give either commit or ref')
        if self.ref is not None and self.verify is None:
            raise PydanticCustomError(
                'verify_needed', 'a ref needs verify: none, or verify: {keys: <file>}'
            )
        if self.commit is not None and self.verify is not None:
            raise PydanticCustomError(
                'verify_with_commit', 'a commit is pinned by the packaging: it takes no verify'
            )
        return self


class RepositoryChannel(_Model):
    """Where a channel is published, for one distribution."""

    component: str | None = None
    """Debian family: the component holding the channel (default: ``main`` for the release
    channel, the channel's name for a snapshot one)."""

    path: Path | None = None
    """Fedora family, snapshot channels: the directory of the channel's own repository, relative
    to build.conf's directory (required)."""

    url: str | None = None
    """Fedora family, snapshot channels: the URL that repository is served from (required when
    the distribution's repository has one)."""

    name: str | None = None
    """Fedora family: the repository's short name, as the distribution's ``name``."""

    description: str | None = None
    """Fedora family: the repository's label, as the distribution's ``description``."""


class ReleaseTool(_Model):
    """The settings of the release tool (``distropack-build pending`` and ``release``)."""

    author: str | None = None
    """The author of the changelog entries it writes, ``Name <email>`` (default: from git's
    ``user.name`` and ``user.email``)."""

    packages: tuple[str, ...] | None = None
    """Globs of the packages a release updates (default: every package with a snapshot
    channel)."""

    distribution: str = 'unstable'
    """The distribution of the deb changelog entries it writes."""


class Repository(_Model):
    """The repository the packages of a distribution are published in."""

    path: Path = Path('repositories')
    """Where the repository is written, relative to build.conf's directory."""

    url: str | None = None
    """The URL the repository is served from; without it, no installation page is written."""

    name: str | None = None
    """The repository's short name, in the keyring and .repo file names (default: the URL's last
    path component)."""

    description: str | None = None
    """The repository's label, in its Release file and its .repo file (default: its name)."""

    channels: Mapping[ChannelName, RepositoryChannel] = Field(default_factory=dict)
    """Where each channel is published, when not as the defaults say."""


class Gpg(_Model):
    """The OpenPGP key that signs a distribution's packages and repository."""

    file: Path = Path('key.gpg')
    """The file holding the secret key, relative to build.conf's directory."""

    id: str | None = None
    """The fingerprint or key ID of the key to use, when the file holds several."""

    uid: str | None = None
    """The user ID of the key to use, in the changelog entries (default: the key's first one)."""


class ExtraRepository(_Model):
    """A signed repository of other packages the builds depend on (another project's).

    The builds install from it with a lower precedence than the project's own packages, and the
    repository's page tells its users to add it first.
    """

    url: str
    """Its URL: of the whole repository for the Debian family, of a release's directory for the
    rpm families; ``{codename}``, ``{version}`` and ``{suite}`` are replaced from the release. A
    ``file://`` URL names a directory of the build's host, mounted into the build."""

    key: Path
    """The file of the public key that signs it, relative to build.conf's directory."""

    key_url: str | None = None
    """The URL its users download that key from, for the page."""

    name: Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]+$')] | None = None
    """Its short name, in the file names of its configuration (default: extra-<its index>):
    letters, digits, `_` and `-`, which apt and dnf accept in their file names."""

    suite: str = '{codename}'
    """The suite of a Debian family repository, a template as url."""

    components: tuple[str, ...] = ('main',)
    """The components of a Debian family repository."""


class Distrib(_Model):
    """A distribution to build for, keyed by its id (``debian``, ``fedora``)."""

    versions: Annotated[Tokens, AfterValidator(_not_available_alone)] = ('supported',)
    """The releases to build for: codenames, versions, ``supported``, ``testing``, ``sid``,
    ``all``, and ``available``, which keeps only those whose builder image is published."""

    repository: Repository = Repository()
    """The repository its packages are published in."""

    gpg: Gpg = Gpg()
    """The key that signs its packages and repository."""

    extra_repositories: tuple[ExtraRepository, ...] = ()
    """The repositories of other packages its builds depend on, signature-checked."""


class Package(_Model):
    """A package to build, keyed by the name of its directory.

    The directory holds ``debian/`` for the Debian family, ``<name>.spec`` for the Fedora family,
    or both. A 3.0 (quilt) deb source downloads its upstream tarball as ``debian/watch`` says; an
    rpm, as its spec's ``Source:`` lines say.
    """

    disable: Mapping[TargetId, Annotated[Tokens, AfterValidator(_not_available)]] = Field(
        default_factory=dict
    )
    """The releases, per distribution id, not to build this package for (release tokens)."""

    channels: Mapping[ChannelName, PackageChannel] = Field(default_factory=dict)
    """The snapshot channels that build this package, and its upstream source in each."""


class ConfigFile(_Model):
    """build.conf."""

    model_config = ConfigDict(title='build.conf')

    channels: Mapping[ChannelName, Channel] = Field(
        default_factory=lambda: {'stable': Channel(kind='release')}
    )
    """The channels, keyed by name: exactly one ``kind: release``, any number of ``kind:
    snapshot`` (default: one release channel, ``stable``)."""

    release: ReleaseTool = ReleaseTool()
    """The settings of the release tool."""

    distribs: Mapping[TargetId, Distrib]
    """The distributions to build for, keyed by id."""

    packages: Mapping[str, Annotated[Package, BeforeValidator(lambda v: {} if v is None else v)]]
    """The packages to build, keyed by the name of their directory, beside build.conf."""


class BuildConfig(ConfigFile):
    """build.conf, and the directory it was read from."""

    root: Path
    """The directory of build.conf, which every relative path is read from."""

    @property
    def release_channel(self) -> str:
        """The name of the release channel."""
        return next(name for name, channel in self.channels.items() if channel.kind == 'release')

    def snapshot_channels(self) -> list[str]:
        """The names of the snapshot channels, as declared."""
        return [name for name, channel in self.channels.items() if channel.kind == 'snapshot']

    def fallback(self, channel: str) -> str:
        """The channel whose state provides what a snapshot channel does not build."""
        return self.channels[channel].fallback or self.release_channel

    def label(self, channel: str, package: str) -> str:
        """The label of a package's snapshot versions in a channel."""
        label = self.channels[channel].version_label
        if label is not None:
            return label
        ref = self.packages[package].channels[channel].ref
        return channel if ref is None else version_label(ref)

    def channel_packages(self, channel: str) -> list[str]:
        """The packages a channel builds: all for the release one, those declaring a snapshot."""
        if channel == self.release_channel:
            return list(self.packages)
        return [name for name, package in self.packages.items() if channel in package.channels]

    def packaging_dir(self, name: str, channel: str) -> Path:
        """The packaging directory of a package in a channel.

        ``<name>-<channel>/`` for a snapshot channel when it exists, else ``<name>/``.
        """
        if channel != self.release_channel:
            own = self.root / f'{name}-{channel}'
            if own.is_dir():
                return own
        return self.root / name

    def component(self, dist_id: str, channel: str) -> str:
        """The deb component a channel is published in."""
        setting = self.distribs[dist_id].repository.channels.get(channel)
        if setting is not None and setting.component is not None:
            return setting.component
        return 'main' if channel == self.release_channel else channel

    def channel_repository(self, dist_id: str, channel: str) -> Repository:
        """The repository a channel is published in.

        On the rpm families, a snapshot channel's own; the distribution's otherwise.
        """
        repository = self.distribs[dist_id].repository
        setting = repository.channels.get(channel)
        if channel == self.release_channel or setting is None:
            return repository
        return repository.model_copy(
            update={
                'path': setting.path if setting.path is not None else repository.path,
                'url': setting.url,
                'name': setting.name,
                'description': setting.description,
            }
        )

    def released_packages(self) -> list[str]:
        """The packages the release tool updates.

        Those release.packages matches, else every package with a snapshot channel.
        """
        globs = self.release.packages
        if globs is None:
            return [name for name, package in self.packages.items() if package.channels]
        return [name for name in self.packages if any(fnmatch.fnmatchcase(name, g) for g in globs)]


def schema() -> dict[str, Any]:
    """The JSON Schema of build.conf."""
    return ConfigFile.model_json_schema()


def load(path: Path) -> BuildConfig:
    """The build configuration in path.

    Raises:
        ConfigError: the file cannot be read, is not YAML, does not match the models, or names a
            package whose directory has no packaging for what it asks.
    """
    try:
        text = path.read_text()
    except OSError as error:
        raise ConfigError(f'{path}: {error.strerror}') from error
    try:
        data = yaml.safe_load(text)
    except yaml.MarkedYAMLError as error:
        where = '' if error.problem_mark is None else f'line {error.problem_mark.line + 1}: '
        raise ConfigError(f'{path}: {where}{error.problem}') from error
    except yaml.YAMLError as error:
        raise ConfigError(f'{path}: {" ".join(str(error).split())}') from error
    if not isinstance(data, dict):
        raise ConfigError(
            f'{path}: expected a mapping with distribs and packages, found {_kind(data)}'
        )
    data = {key: value for key, value in data.items() if not str(key).startswith('.')}
    try:
        parsed = ConfigFile.model_validate(data)
    except ValidationError as error:
        raise ConfigError(f'{path}: {_describe(error)}') from error
    root = path.parent.resolve()
    for dist_id, distrib in parsed.distribs.items():
        _check_repository_path(
            f'{path}: distribs.{dist_id}.repository.path', root, distrib.repository.path
        )
        for index, extra in enumerate(distrib.extra_repositories):
            where = f'{path}: distribs.{dist_id}.extra_repositories.{index}'
            _check_extra(where, root, targets.is_deb(targets.target(dist_id)), extra)
            if distrib.repository.url is not None and extra.key_url is None:
                raise ConfigError(
                    f'{where}: key_url: the repository page tells its users to download this key'
                    ' from it; give it, or no repository.url'
                )
    _check_channels(path, root, parsed)
    for name in parsed.packages:
        _check_packaging(path, root, name)
        for channel in parsed.packages[name].channels:
            if (root / f'{name}-{channel}').is_dir():
                _check_packaging(path, root, name, f'{name}-{channel}')
    return BuildConfig(
        channels=parsed.channels,
        release=parsed.release,
        distribs=parsed.distribs,
        packages=parsed.packages,
        root=root,
    )


def _check_channels(path: Path, root: Path, parsed: ConfigFile) -> None:
    """Raise ConfigError unless the channels are one release channel and snapshot ones.

    The packages and repositories must name declared channels, consistently with their family.
    """
    releases = [name for name, channel in parsed.channels.items() if channel.kind == 'release']
    if len(releases) != 1:
        raise ConfigError(f'{path}: channels: exactly one channel has kind: release')
    (release,) = releases
    for name, declared in parsed.channels.items():
        if declared.fallback is not None and declared.fallback not in parsed.channels:
            raise ConfigError(
                f'{path}: channels.{name}.fallback: unknown channel {declared.fallback}'
            )
    used: set[str] = set()
    for name, package in parsed.packages.items():
        for channel, entry in package.channels.items():
            where = f'{path}: packages.{name}.channels.{channel}'
            if channel not in parsed.channels:
                raise ConfigError(f'{where}: unknown channel {channel}')
            if channel == release:
                raise ConfigError(
                    f'{where}: the release channel builds every package from its own packaging'
                )
            used.add(channel)
            for directory in (root / f'{name}-{channel}', root / name):
                debian = directory / 'debian'
                if directory.is_dir():
                    if debian.is_dir() and _native(debian):
                        raise ConfigError(
                            f'{where}: a 3.0 (native) deb source has no upstream tarball for a'
                            ' snapshot to replace: make it 3.0 (quilt)'
                        )
                    break
            if isinstance(entry.verify, VerifyKeys):
                _check_key(f'{where}.verify.keys', root, entry.verify.keys)
    for dist_id, distrib in parsed.distribs.items():
        deb = targets.is_deb(targets.target(dist_id))
        repository = distrib.repository
        for channel, setting in repository.channels.items():
            where = f'{path}: distribs.{dist_id}.repository.channels.{channel}'
            if channel not in parsed.channels:
                raise ConfigError(f'{where}: unknown channel {channel}')
            rpm_only = ('path', 'url', 'name', 'description')
            if deb and any(getattr(setting, key) is not None for key in rpm_only):
                raise ConfigError(
                    f'{where}: path, url, name and description are for the rpm families; a deb'
                    " channel is a component of the distribution's repository"
                )
            if not deb and setting.component is not None:
                raise ConfigError(
                    f'{where}: component is for the Debian family; an rpm channel is a'
                    ' repository of its own'
                )
            if not deb and setting.path is not None:
                _check_repository_path(f'{where}.path', root, setting.path)
        if deb:
            continue
        for channel in sorted(used):
            used_setting = repository.channels.get(channel)
            where = f'{path}: distribs.{dist_id}.repository.channels.{channel}'
            if used_setting is None or used_setting.path is None:
                raise ConfigError(
                    f'{where}.path: an rpm snapshot channel is a repository of its own: say where'
                )
            if repository.url is not None and used_setting.url is None:
                raise ConfigError(
                    f"{where}.url: the repository page links to the channel's repository: give"
                    ' its URL, or no repository.url'
                )


def _check_repository_path(where: str, root: Path, directory: Path) -> None:
    """Raise ConfigError unless directory is inside the project, and not the project itself."""
    # publication removes the repository before writing it again: never the project's
    # directory, nor anything outside it
    output = (root / directory).resolve()
    if output == root or not output.is_relative_to(root):
        raise ConfigError(f'{where}: {directory} must be a directory inside the project')


def _native(debian: Path) -> bool:
    """Whether the deb source of debian is in 3.0 (native) format."""
    try:
        return (debian / 'source' / 'format').read_text().strip() == '3.0 (native)'
    except FileNotFoundError:
        return False


_FIELDS = {'codename': 'trixie', 'version': '13', 'suite': 'stable'}


def expand(template: str, release: Release) -> str:
    """template, its {codename}, {version} and {suite} replaced from release (empty when absent)."""
    values = {
        'codename': release.codename or '',
        'version': release.version or '',
        'suite': release.suite or '',
    }
    return template.format(**values)


def _check_extra(where: str, root: Path, deb: bool, extra: ExtraRepository) -> None:
    """Raise ConfigError if extra cannot be used: its key, its templates, its family's fields."""
    for template in (extra.url, extra.suite):
        try:
            template.format(**_FIELDS)
        except KeyError as error:
            raise ConfigError(f'{where}: unknown field {{{error.args[0]}}} in {template}') from None
    if not deb and (extra.suite, extra.components) != ('{codename}', ('main',)):
        raise ConfigError(f'{where}: suite and components are for the Debian family')
    _check_key(f'{where}.key', root, extra.key)


def _check_key(where: str, root: Path, file: Path) -> None:
    """Raise ConfigError unless file holds an OpenPGP public key, and no secret one."""
    key = root / file
    try:
        data = key.read_bytes()
    except OSError as error:
        raise ConfigError(f'{where}: cannot read {key}: {error.strerror}') from error
    try:
        signing.check_public_key(data)
    except ValueError as error:
        if 'secret key' in str(error):
            raise ConfigError(f'{where}: {key} holds a secret key: {error}') from error
        raise ConfigError(f'{where}: {key} is not an OpenPGP public key: {error}') from error


def _kind(data: object) -> str:
    """What a YAML document holds, in words."""
    if data is None:
        return 'an empty document'
    if isinstance(data, list):
        return 'a list'
    return f'a {type(data).__name__}'


def _describe(error: ValidationError) -> str:
    """The first error of a validation, on one line: its dotted path and its message."""
    first = error.errors()[0]
    where = '.'.join(str(part) for part in first['loc'] if part != '[key]')
    more = '' if error.error_count() == 1 else f' (and {error.error_count() - 1} more)'
    return f'{where}: {first["msg"]}{more}' if where else f'{first["msg"]}{more}'


def _check_packaging(path: Path, root: Path, name: str, directory_name: str | None = None) -> None:
    """Raise ConfigError if the directory of a package has no packaging for what it asks.

    The directory is directory_name (a channel's), by default the package's name.
    """
    directory = root / (directory_name or name)
    deb = (directory / 'debian').is_dir()
    if not deb and not (directory / f'{name}.spec').is_file():
        raise ConfigError(
            f'{path}: packages.{name}: {directory} has neither debian/ nor {name}.spec'
        )
    try:
        sources.read(directory)
    except sources.SourcesError as error:
        raise ConfigError(f'{path}: packages.{name}: {error}') from error
    debian = directory / 'debian'
    if deb and _quilt(debian) and not (debian / 'watch').is_file():
        raise ConfigError(
            f'{path}: packages.{name}: a 3.0 (quilt) source needs a debian/watch, to download its'
            f' upstream tarball with, and {debian} has none'
        )


def _quilt(debian: Path) -> bool:
    """Whether the deb source of debian is in 3.0 (quilt) format."""
    try:
        return (debian / 'source' / 'format').read_text().strip() == '3.0 (quilt)'
    except FileNotFoundError:
        return False
