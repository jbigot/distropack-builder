# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""build.conf: the distributions to build for, their repositories, and the packages to build.

Every path in it is relative to its own directory. The top-level keys that start with a dot are
ignored: they hold the YAML anchors that the distributions merge (``<<: *distribution``).
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    WithJsonSchema,
)
from pydantic_core import PydanticCustomError

from distro_releases import UnknownDistribution
from distropack_builder import targets

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


class Gpg(_Model):
    """The OpenPGP key that signs a distribution's packages and repository."""

    file: Path = Path('key.gpg')
    """The file holding the secret key, relative to build.conf's directory."""

    id: str | None = None
    """The fingerprint or key ID of the key to use, when the file holds several."""

    uid: str | None = None
    """The user ID of the key to use, in the changelog entries (default: the key's first one)."""


class Distrib(_Model):
    """A distribution to build for, keyed by its id (``debian``, ``fedora``)."""

    versions: Annotated[Tokens, AfterValidator(_not_available_alone)] = ('supported',)
    """The releases to build for: codenames, versions, ``supported``, ``testing``, ``sid``,
    ``all``, and ``available``, which keeps only those whose builder image is published."""

    repository: Repository = Repository()
    """The repository its packages are published in."""

    gpg: Gpg = Gpg()
    """The key that signs its packages and repository."""


class Package(_Model):
    """A package to build, keyed by the name of its directory.

    The directory holds ``debian/`` for the Debian family, ``<name>.spec`` for the Fedora family,
    or both.
    """

    orig: str | None = None
    """The URL of the upstream tarball of a 3.0 (quilt) deb source package, where
    ``{upstream_version}`` and ``{package}`` are replaced from debian/changelog; rpm builds use
    the spec's own Source: lines."""

    disable: Mapping[TargetId, Annotated[Tokens, AfterValidator(_not_available)]] = Field(
        default_factory=dict
    )
    """The releases, per distribution id, not to build this package for (release tokens)."""


class ConfigFile(_Model):
    """build.conf."""

    model_config = ConfigDict(title='build.conf')

    distribs: Mapping[TargetId, Distrib]
    """The distributions to build for, keyed by id."""

    packages: Mapping[str, Annotated[Package, BeforeValidator(lambda v: {} if v is None else v)]]
    """The packages to build, keyed by the name of their directory, beside build.conf."""


class BuildConfig(ConfigFile):
    """build.conf, and the directory it was read from."""

    root: Path
    """The directory of build.conf, which every relative path is read from."""


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
    for name, package in parsed.packages.items():
        _check_packaging(path, root, name, package)
    return BuildConfig(distribs=parsed.distribs, packages=parsed.packages, root=root)


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


def _check_packaging(path: Path, root: Path, name: str, package: Package) -> None:
    """Raise ConfigError if the directory of a package has no packaging for what it asks."""
    directory = root / name
    deb = (directory / 'debian').is_dir()
    if not deb and not (directory / f'{name}.spec').is_file():
        raise ConfigError(
            f'{path}: packages.{name}: {directory} has neither debian/ nor {name}.spec'
        )
    if package.orig is not None and not deb:
        raise ConfigError(
            f'{path}: packages.{name}.orig: orig: names the upstream tarball of a deb source'
            f' package, and {directory} has no debian/'
        )
