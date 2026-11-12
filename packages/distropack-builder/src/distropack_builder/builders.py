# SPDX-FileCopyrightText: 2020-2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The builds of the packages, in their release's builder image, and their repositories.

A package builder builds one package for one release; a repository builder publishes the
outputs of a family's builds, signed, with its page. run_builds drives a whole run.
"""

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence, Set
from configparser import ConfigParser
from dataclasses import dataclass
from os import link
from pathlib import Path
from shutil import copy2, copytree, rmtree
from tempfile import TemporaryDirectory
from typing import Any, ClassVar, Literal
from urllib.parse import urlparse

import jinja2
from debian.changelog import Changelog
from debian.deb822 import Deb822, Dsc, _PkgRelationMixin
from debian.debian_support import Version

from distro_releases import Release
from distropack_builder import readme, repodata, rpmquery, signing, sources, targets
from distropack_builder.bake import DEB_TOOLS, PROTOCOL, REGISTRY, tool_image
from distropack_builder.config import BuildConfig, Distrib, ExtraRepository, expand
from distropack_builder.context import BuildContext
from distropack_builder.docker import ContainerFailed, Mount, RunResult
from distropack_builder.downloads import DownloadFailed, fetch_all, source_name
from distropack_builder.graph import closure, dependencies
from distropack_builder.publication import published
from distropack_builder.report import Reporter, Unreproducible, build_logger
from distropack_builder.rpmquery import SpecFailed, SpecInfo
from distropack_builder.scheduler import Reused, Succeeded, schedule
from distropack_builder.signing import Signer
from distropack_builder.state import (
    RELEASE_CHANNEL,
    ReleaseState,
    ReleaseStateWriter,
    StoredPackage,
    packaging_key,
    static_key,
)
from distropack_builder.uscan import OrigFiles, fetch_origs
from distropack_builder.versions import (
    binnmu_entry,
    deb_base_version,
    deb_version,
    rpm_changelog_entry,
    rpm_compare,
    rpm_release,
)

# The host name of the build containers, the same in every run.
BUILD_HOSTNAME = 'builder'
# The package files reproducibility is checked on; .changes and .buildinfo record the build.
PACKAGE_FILES = ('.deb', '.rpm', '.dsc')
# How many differing files of a build diffoscope reports on.
DIFFOSCOPE_REPORTS = 3


# The files a build needs downloaded: each URL's file, or why it could not be.
Files = Mapping[str, Path | DownloadFailed]


def codename(release: Release) -> str:
    """The codename of a release of the Debian family, which all have one."""
    if release.codename is None:
        raise ValueError(f'{release} has no codename')
    return release.codename


def version(release: Release) -> str:
    """The version of a release of the Fedora family, which all have one."""
    if release.version is None:
        raise ValueError(f'{release} has no version')
    return release.version


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


def link_or_copy(source: Path, target: Path) -> None:
    """Make target a hard link to source where the file system allows it, a copy elsewhere."""
    try:
        link(source, target)
    except OSError:
        copy2(source, target)


class Container:
    """An image of the registry, run in a new container at each call."""

    def __init__(
        self, context: BuildContext, image: str, logger: logging.Logger | None = None
    ) -> None:
        """The image, relative to REGISTRY, run in context, its output logged to logger."""
        self._context = context
        self._image = REGISTRY + image
        self._logger = logger

    def __call__(
        self,
        *args: str,
        mounts: Sequence[Mount] = (),
        shm_size: str | None = None,
        as_root: bool = False,
        env: Mapping[str, str] | None = None,
        workdir: str | None = None,
    ) -> RunResult:
        """Run the image with args; raises ContainerFailed if it fails."""
        return self._context.run(
            self._context.engine,
            self._image,
            args,
            mounts=mounts,
            env=env,
            workdir=workdir,
            shm_size=shm_size,
            as_root=as_root,
            cancellation=self._context.cancellation,
            logger=self._logger,
        )


# The one architecture packages are built and published for: binNMU entries and aptly name it.
ARCHITECTURE = 'amd64'
# The binNMU's changelog entry, beside the stored source package in its /src.
BINNMU_CHANGELOG = 'binnmu.changelog'

# The configuration of aptly: its database in /aptlyroot, what it publishes in /aptlyout. It reads
# the clear-signed .dsc with its own OpenPGP code: gpg needs a home to write to, which the user a
# tool container runs as under rootful Docker does not have in the image.
APTLY_CONFIG = {
    'architectures': [ARCHITECTURE, 'source'],
    'gpgProvider': 'internal',
    'rootDir': '/aptlyroot',
    'FileSystemPublishEndpoints': {
        'default': {
            'rootDir': '/aptlyout',
            'linkMethod': 'copy',
        }
    },
}


class Aptly(Container):
    """aptly, alone in its image: it publishes unsigned, the Signer signs."""

    def __init__(self, context: BuildContext, logger: logging.Logger | None = None) -> None:
        """aptly, run in context."""
        super().__init__(context, tool_image(DEB_TOOLS), logger)


class RpmSign(Container):
    """rpmsign and gpg, alone in their image: rpm has no other way to sign a package."""

    def __init__(self, context: BuildContext, logger: logging.Logger | None = None) -> None:
        """rpmsign, run in context."""
        super().__init__(context, tool_image('rpm_sign'), logger)

    def sign(self, signer: Signer, directory: Path, names: list[str]) -> None:
        """Sign the rpms names of directory, with the key of signer, given to gpg locked."""
        with signer.gpg_secret(self._context.workdir) as secret:
            self(
                'sign',
                signer.fingerprint,
                *names,
                mounts=[Mount('/data', directory), Mount('/secret', secret, read_only=True)],
            )


class Control(Deb822, _PkgRelationMixin):
    """debian/control's source paragraph, its build dependencies parsed as relations."""

    _relationship_fields = ['build-depends', 'build-depends-indep', 'build-depends-arch']

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """The paragraph, as Deb822 reads it."""
        Deb822.__init__(self, *args, **kwargs)
        _PkgRelationMixin.__init__(self, *args, **kwargs)


class UnversionedChange(Exception):
    """A package whose sources or packaging changed, its version not: it cannot be built."""

    def __init__(self, package: str) -> None:
        """The failure of package, which says what to do."""
        super().__init__(
            f'{package}: sources or packaging changed without a new version '
            '(add a debian/changelog entry / bump Release in the spec)'
        )


class LowerVersion(Exception):
    """A package whose version is below the one its release's state stores: never published."""

    def __init__(self, package: str, version: str, stored: str) -> None:
        """The failure of package, at version below stored."""
        super().__init__(f'{package}: version {version} is below the stored {stored}')


class PackageBuilder:
    """The build of one package for one release, the base of the builders of each family.

    Its mode follows what the release's state stores of it (stored): without it, or for another
    base version, a build from source; for the same base version and static key, a rebuild
    candidate, which the dependency container finds unneeded when the environment is the stored
    one (the stored package is reused) and otherwise rebuilds with the next rebuild number; for the
    same base version and another static key, a failure, before any container.
    """

    # The suffix of the build's work directory.
    family: ClassVar[Literal['deb', 'rpm']]

    def __init__(
        self, context: BuildContext, dist_release: Release, base_dir: Path, gpg: Signer, name: str
    ) -> None:
        """The build of the package name of base_dir for dist_release, signed by gpg."""
        self._context = context
        self.dist_release = dist_release
        self._base_dir = base_dir
        self._gpg = gpg
        self.name = name
        self.logger = build_logger(name, dist_release)
        self.outdir: Path | None = None
        self.bin_provides: set[str] = set()
        self.bin_depends: set[str] = set()
        # the repositories of other packages its dependencies may come from, set by run_builds
        self.extras: Sequence[ExtraRepository] = ()
        # what the release's state stores of the package, set by run_builds
        self.stored: StoredPackage | None = None
        # the environment key of a rebuild candidate: a dependency container that installs the
        # same environment builds nothing
        self._known_env: str | None = None
        # what this build's dependency container installed, as its sha256, and whether it was
        # _known_env: then no package was built
        self.env_key: str | None = None
        self.unchanged = False
        # what build() made of it: its versions, keys, and which of its files are its source
        # package and its Architecture: all packages
        self.base_version = ''
        self.rebuild = 0
        self.version = ''
        self.static_key = ''
        self.packaging_hash = ''
        self.source_files: list[str] = []
        self.arch_all_files: list[str] = []

    def __str__(self) -> str:
        """The package's name, as the report names the build a skipped one waited for."""
        return self.name

    @classmethod
    def applies(cls, base_dir: Path, name: str) -> bool:
        """Whether the package has packaging for this family."""
        raise NotImplementedError

    def sources(self) -> list[str]:
        """The URLs of the files the build needs downloaded."""
        return []

    def build(self, deps: Set['PackageBuilder'], files: Files) -> Reused | None:
        """Build, with the outputs of deps in the local repository, and files downloaded.

        Returns:
            Reused() when the stored package stands, which is then the output; None when built.

        Raises:
            DownloadFailed: a file the build needs could not be downloaded, or does not match
                the package's sources line.
            UnversionedChange: the sources or packaging changed, the base version did not.
        """
        expected = sources.read(self._base_dir / self.name)
        for url in self.sources():
            file = files[url]
            if isinstance(file, DownloadFailed):
                raise file
            mismatch = sources.check(source_name(url), file, expected)
            if mismatch is not None:
                raise mismatch
        self._check_own_downloads(expected)
        self._prepare()
        packaging = self._packaging()
        self.base_version = self._base_version()
        self.static_key = static_key(packaging, self._source_files(files), RELEASE_CHANNEL)
        self.packaging_hash = packaging_key(packaging)
        stored = self.stored
        if stored is not None and self._below(self.base_version, stored.base_version):
            raise LowerVersion(self.name, self.base_version, stored.base_version)
        if stored is None or stored.base_version != self.base_version:
            self.rebuild = 0
        elif stored.static_key != self.static_key:
            raise UnversionedChange(self.name)
        else:
            self.rebuild = stored.rebuild + 1
            self._known_env = stored.env_key
            if not self._rebuilds_binaries():
                return self._reuse(stored)
        self.version = self._version(self.rebuild)
        wk_dir = self._context.workdir / f'{self.name}.{self.dist_release.uid}.{self.family}-build'
        wk_dir.mkdir()
        self.outdir = wk_dir / 'output'
        self.outdir.mkdir()
        self._build(wk_dir, self.outdir, set(deps) | {self}, files)
        if self.unchanged:
            assert stored is not None  # only a rebuild candidate knows an environment
            return self._reuse(stored)
        return None

    def _reuse(self, stored: StoredPackage) -> Reused:
        """Make the stored package the output, with what it was built from."""
        self.unchanged = True
        self.outdir = stored.directory
        self.rebuild = stored.rebuild
        self.version = stored.version
        self.env_key = stored.env_key
        self.source_files = list(stored.source_files)
        self.arch_all_files = list(stored.arch_all_files)
        return Reused()

    def _prepare(self) -> None:
        """Raise when the packaging cannot say its version."""

    def _packaging(self) -> Path:
        """The packaging the static key covers."""
        raise NotImplementedError

    def _source_files(self, files: Files) -> dict[str, Path]:
        """The source files the static key covers, by name: those downloaded for the build."""
        raise NotImplementedError

    def _base_version(self) -> str:
        """The version of a first build for the release, without rebuild number."""
        raise NotImplementedError

    def _version(self, rebuild: int) -> str:
        """The full version of the rebuild-th build of the base version."""
        raise NotImplementedError

    def _rebuilds_binaries(self) -> bool:
        """Whether a rebuild of the same source makes any package."""
        return True

    def _below(self, version: str, other: str) -> bool:
        """Whether the base version version sorts before other, in the family's order."""
        raise NotImplementedError

    def _check_own_downloads(self, expected: Mapping[str, str]) -> None:
        """Raise DownloadFailed for a file the builder downloaded itself that is not expected."""

    def _build(self, wk_dir: Path, outdir: Path, deps: Set['PackageBuilder'], files: Files) -> None:
        """Build in wk_dir, the packages written to outdir."""
        raise NotImplementedError

    def _run_builder(
        self,
        *,
        deps_args: Sequence[str] = (),
        build_args: Sequence[str] = (),
        deps_mounts: Sequence[Mount],
        build_mounts: Sequence[Mount],
        outputs: str,
        env: Mapping[str, str] | None = None,
    ) -> None:
        """Build in two containers of the release's builder image, as sbuild and mock do.

        The first installs the build dependencies, with the engine's default network, and is
        committed to a temporary image; the second builds from that image, with no network, as
        the archives' builders do, its host name fixed. The temporary image is removed after it,
        failed or not. Checking reproducibility, a third container builds again from the same
        image, varied, into another directory than build_mounts' outputs one, and the packages of
        both are compared (Unreproducible).
        """
        # the builder image's name and tag, unique: distropack-build/fedora_builder-<id>:44-p3
        name, _, tag = builder_image(self.dist_release).partition(':')
        temporary = f'distropack-build/{name}-{uuid.uuid4().hex[:12]}:{tag}'
        buildenv = self._context.workdir / f'{self.name}.{self.dist_release.uid}.buildenv'
        buildenv.mkdir()
        deps_mounts = [*deps_mounts, Mount('/buildenv', buildenv), *self._extra_mounts()]
        deps_env = dict(env or {})
        if self._known_env is not None:
            deps_env['DISTROPACK_KNOWN_ENV'] = self._known_env
        run = self._context.run
        common: dict[str, Any] = {
            'env': env,
            'shm_size': '5g',
            'as_root': True,
            'cancellation': self._context.cancellation,
            'logger': self.logger,
        }
        run(
            self._context.engine,
            REGISTRY + builder_image(self.dist_release),
            ['deps', *deps_args],
            mounts=deps_mounts,
            commit_to=temporary,
            commit_unless=buildenv / 'unchanged',
            **{**common, 'env': deps_env},
        )
        installed = buildenv / 'installed'
        if not installed.is_file():
            raise RuntimeError('the builder image reported no environment')
        self.env_key = hashlib.sha256(installed.read_bytes()).hexdigest()
        if (buildenv / 'unchanged').exists():
            self.unchanged = True
            return
        check = self._context.check_reproducible
        first = next(m.source for m in build_mounts if m.target == outputs)
        # a binNMU's stored source, which the build reads and does not make
        inputs = {path.name for path in first.iterdir()}
        try:
            run(
                self._context.engine,
                temporary,
                ['build', *build_args],
                mounts=build_mounts,
                network=False,
                remove_image=not check,
                hostname=BUILD_HOSTNAME,
                **common,
            )
        except BaseException:
            if check and self._context.engine is not None:
                self._context.engine.remove_image(temporary)
            raise
        if check:
            self._check_reproducible(temporary, build_args, build_mounts, outputs, common, inputs)

    def _check_reproducible(
        self,
        image: str,
        build_args: Sequence[str],
        build_mounts: Sequence[Mount],
        outputs: str,
        common: Mapping[str, Any],
        inputs: Set[str] = frozenset(),
    ) -> None:
        """Build again from image, varied, and compare the packages of both builds, not inputs.

        What must not matter varies, as reprotest varies it: the umask, the time zone, the locale,
        the parallelism, the time (a later start), and for deb the build path and the host name:
        rpm records both in every package (the build host, and the spec expanded in the source
        rpm), by design, and its archives' builders fix them, as mock's /builddir.

        Raises:
            Unreproducible: a package file differs, or one build made a package the other did not.
        """
        first = next(m.source for m in build_mounts if m.target == outputs)
        second = self._context.workdir / f'{self.name}.{self.dist_release.uid}.again'
        second.mkdir()
        mounts = [m if m.target != outputs else Mount(outputs, second) for m in build_mounts]
        jobs = [arg for arg in build_args if not arg.startswith('-j')]
        varied_jobs = (
            ['-j1' if self._context.jobs > 1 else '-j2'] if len(jobs) < len(build_args) else []
        )
        deb = self.family == 'deb'
        env = {
            **(common.get('env') or {}),
            'TZ': 'Pacific/Kiritimati',
            'LC_ALL': 'C.UTF-8',
            'DISTROPACK_UMASK': '0002',
            **({'DISTROPACK_BUILD_PATH': '/tmp/varied/build'} if deb else {}),
        }
        self._context.run(
            self._context.engine,
            image,
            ['build', *jobs, *varied_jobs],
            mounts=mounts,
            network=False,
            remove_image=True,
            hostname=f'{BUILD_HOSTNAME}-again' if deb else BUILD_HOSTNAME,
            **{**common, 'env': env},
        )
        packages = {
            path.name
            for directory in (first, second)
            for path in directory.iterdir()
            if path.is_file() and path.name.endswith(PACKAGE_FILES) and path.name not in inputs
        }
        differing = sorted(
            name
            for name in packages
            if not (first / name).is_file()
            or not (second / name).is_file()
            or (first / name).read_bytes() != (second / name).read_bytes()
        )
        if differing:
            # its reports go to the build's output, shown with the failure: each differing pair,
            # since one difference often follows from another (a binary rpm records its source
            # rpm's digest)
            pairs = [n for n in differing if (first / n).is_file() and (second / n).is_file()]
            for name in pairs[:DIFFOSCOPE_REPORTS]:
                self._context.run(
                    self._context.engine,
                    REGISTRY + tool_image(DEB_TOOLS),
                    ['diffoscope', '--text', '-', f'/first/{name}', f'/second/{name}'],
                    mounts=[
                        Mount('/first', first, read_only=True),
                        Mount('/second', second, read_only=True),
                    ],
                    check=False,
                    cancellation=self._context.cancellation,
                    logger=self.logger,
                )
            raise Unreproducible(differing)

    def _extra_mounts(self) -> list[Mount]:
        """The configuration of the extra repositories in /extrarepos, and their file:// trees.

        For the Debian family, a deb822 .sources file each; for the rpm families, a .repo file
        each; with each one's key beside it, as given. A file:// repository's directory is
        mounted at /extramirrors/<name>: not under the read-only /extrarepos, where the engine
        could not make its mount point.
        """
        if not self.extras:
            return []
        directory = self._context.workdir / f'{self.name}.{self.dist_release.uid}.extrarepos'
        directory.mkdir()
        mounts = [Mount('/extrarepos', directory, read_only=True)]
        for index, extra in enumerate(self.extras):
            name = extra.name or f'extra-{index}'
            url = expand(extra.url, self.dist_release)
            if url.startswith('file://'):
                mirror = f'/extramirrors/{name}'
                mounts.append(Mount(mirror, Path(url.removeprefix('file://')), read_only=True))
                url = f'file://{mirror}' + ('/' if url.endswith('/') else '')
            key = f'/extrarepos/{name}.asc'
            # armored, whatever the file: apt before 3.0 reads a Signed-By file by its extension
            armored = signing.armored((self._base_dir / extra.key).read_bytes())
            (directory / f'{name}.asc').write_bytes(armored)
            if self.family == 'deb':
                (directory / f'{name}.sources').write_text(
                    f'Types: deb\nURIs: {url}\nSuites: {expand(extra.suite, self.dist_release)}'
                    f'\nComponents: {" ".join(extra.components)}\nSigned-By: {key}\n'
                )
            else:
                (directory / f'{name}.repo').write_text(
                    f'[{name}]\nname={name}\nbaseurl={url}\nenabled=1\ngpgcheck=1\n'
                    f'repo_gpgcheck=1\ngpgkey=file://{key}\n'
                )
        return mounts

    def _local_repository(
        self,
        family: type['DebRepoBuilder'] | type['RpmRepoBuilder'],
        localrepo: Path,
        deps: Set['PackageBuilder'],
    ) -> None:
        """Publish the outputs of deps at localrepo, unsigned, for the builder container."""
        failures = family(
            self._context, localrepo, self._gpg, logger=self.logger, sign=False
        ).build(Output(dep.dist_release, dep.outdir) for dep in deps if dep.outdir is not None)
        for error in failures.values():
            raise error


class DebPackageBuilder(PackageBuilder):
    """The build of a deb source package, and its binary packages, with dpkg-buildpackage."""

    family = 'deb'

    def __init__(
        self,
        context: BuildContext,
        dist_release: Release,
        base_dir: Path,
        gpg: Signer,
        name: str,
    ) -> None:
        """The build of base_dir/name/debian."""
        super().__init__(context, dist_release, base_dir, gpg, name)
        self._debian_dir = base_dir / name / 'debian'
        self._format = source_format(self._debian_dir)
        # the upstream tarball of a 3.0 (quilt) source, which run_builds downloads with uscan
        self.orig: OrigFiles | DownloadFailed | None = None
        with (self._debian_dir / 'changelog').open() as raw_changelog:
            self._changelog = Changelog(raw_changelog)

        deps_kind = ['build-depends', 'build-depends-indep', 'build-depends-arch']
        with (self._debian_dir / 'control').open() as raw_control:
            control_paragraphs = Deb822.iter_paragraphs(
                raw_control, strict={'whitespace-separates-paragraphs': False}
            )
            binaries = [pkg for pkg in control_paragraphs if 'Package' in pkg]
            self.bin_provides = {pkg['Package'] for pkg in binaries}
            # whether a binNMU, which builds the Architecture: any packages only, makes any
            self._arch_any = any(pkg.get('Architecture', '').strip() != 'all' for pkg in binaries)
        with (self._debian_dir / 'control').open() as raw_control:
            control = Control(raw_control)
            self.bin_depends = {
                om['name'] for dl in deps_kind for am in control.relations[dl] or () for om in am
            }

    @classmethod
    def applies(cls, base_dir: Path, name: str) -> bool:
        """Whether the package has a debian/ directory."""
        return (base_dir / name / 'debian').is_dir()

    @property
    def needs_orig(self) -> bool:
        """Whether the source holds an upstream tarball, which its debian/watch downloads."""
        return self._format == '3.0 (quilt)'

    def _check_own_downloads(self, expected: Mapping[str, str]) -> None:
        """The upstream tarball matches its sources line, unless uscan verified its signature."""
        if isinstance(self.orig, OrigFiles) and not self.orig.verified:
            mismatch = sources.check_any(self.orig.upstream.name, self.orig.upstream, expected)
            if mismatch is not None:
                raise mismatch

    def _prepare(self) -> None:
        """Raise when the upstream tarball a 3.0 (quilt) source needs is missing."""
        if self.needs_orig:
            if self.orig is None:
                raise ValueError(f'the upstream tarball of {self.name} was not downloaded')
            if isinstance(self.orig, DownloadFailed):
                raise self.orig
        elif self._format != '3.0 (native)':
            raise ValueError(f'unsupported deb source format: {self._format}')

    def _packaging(self) -> Path:
        """debian/, or the whole source of a native package."""
        return self._debian_dir if self.needs_orig else self._debian_dir.parent

    def _source_files(self, files: Files) -> dict[str, Path]:
        """The upstream tarball of a 3.0 (quilt) source, none for a native one."""
        if isinstance(self.orig, OrigFiles):
            return {self.orig.orig.name: self.orig.orig}
        return {}

    def _base_version(self) -> str:
        """The changelog's version, made release-specific (`~bpo<version>`) where it has one."""
        return deb_base_version(str(self._changelog.version), self.dist_release.version)

    def _version(self, rebuild: int) -> str:
        """The base version, then `+b<rebuild>` for a binNMU."""
        return deb_version(self.base_version, rebuild)

    def _rebuilds_binaries(self) -> bool:
        """Whether there are Architecture: any packages, the only ones a binNMU rebuilds."""
        return self._arch_any

    def _below(self, version: str, other: str) -> bool:
        """Debian's order."""
        return Version(version) < Version(other)

    def _source_tree(self, pkg_dir: Path) -> None:
        """Lay out the source in pkg_dir: its tree, and the upstream tarball of a quilt one."""
        # a fresh copy of the changelog, to which the release may add its own entry
        with (self._debian_dir / 'changelog').open() as raw_changelog:
            changelog = Changelog(raw_changelog)
        source_dir = pkg_dir / f'{changelog.package}-{changelog.upstream_version}'
        if self.needs_orig:
            debian_dir = Path(copytree(self._debian_dir, source_dir / 'debian'))
        else:
            debian_dir = Path(copytree(self._debian_dir.parent, source_dir)) / 'debian'

        # the release's own changelog entry when the base version is not the changelog's,
        # versioned and dated from the packaging alone, so that the same inputs give the same
        # package (dpkg derives SOURCE_DATE_EPOCH from this date)
        if self.base_version != str(changelog.version):
            changelog.new_block(
                package=changelog.package,
                version=self.version,
                distributions=codename(self.dist_release),
                urgency=changelog.urgency,
                changes=['  * Rebuild for ' + str(self.dist_release)],
                author=self._gpg.uid,
                date=changelog.date,
            )
            with (debian_dir / 'changelog').open('w') as raw_changelog:
                changelog.write_to_open_file(raw_changelog)

        if isinstance(self.orig, OrigFiles):
            # named after the upstream version of the entry packaged, not the rebuild's new one
            link_or_copy(self.orig.orig.resolve(), pkg_dir / self.orig.orig.name)

    def _binnmu_source(self, pkg_dir: Path, stored: StoredPackage) -> None:
        """Put in pkg_dir copies of the stored source package and the binNMU's changelog entry.

        Copies, not links: the build container takes ownership of what /src holds.
        """
        for name in stored.source_files:
            copy2(stored.directory / name, pkg_dir / name)
        entry = binnmu_entry(
            self._changelog.package,
            self.version,
            codename(self.dist_release),
            self._gpg.uid,
            self._changelog.date,
            ARCHITECTURE,
        )
        (pkg_dir / BINNMU_CHANGELOG).write_text(entry + '\n')

    def _build(self, wk_dir: Path, outdir: Path, deps: Set[PackageBuilder], files: Files) -> None:
        pkg_dir = wk_dir / 'pkg'
        pkg_dir.mkdir()
        stored = self.stored if self.rebuild > 0 else None
        if stored is None:
            self._source_tree(pkg_dir)
        else:
            self._binnmu_source(pkg_dir, stored)

        localrepo = wk_dir / 'repo'
        self._local_repository(DebRepoBuilder, localrepo, deps)

        release_codename = codename(self.dist_release)
        profiles = f'{self.dist_release.distribution.id},{release_codename}'
        self._run_builder(
            build_args=[
                f'-j{self._context.jobs}',
                '-sa' if stored is None else '-B',
                f'-P{profiles}',
            ],
            deps_mounts=[Mount('/src', pkg_dir), Mount('/localrepo', localrepo)],
            build_mounts=[Mount('/src', pkg_dir)],
            outputs='/src',
            env={'DH_VERBOSE': '1', 'DISTROPACK_BUILD_PROFILES': profiles},
        )
        rmtree(localrepo)
        if self.unchanged:  # nothing built: the stored packages stand
            rmtree(pkg_dir)
            return

        if stored is None:
            # Sign the source package: nothing reads the .changes, aptly adds the .dsc as it is
            self.source_files = []
            for dsc in sorted(pkg_dir.glob('*.dsc')):
                with dsc.open() as raw_dsc:
                    listed = [entry['name'] for entry in Dsc(raw_dsc).get('Files', [])]
                self.source_files += [dsc.name, *listed]
                dsc.write_bytes(self._gpg.clearsign(dsc.read_bytes()))
        else:
            # the binNMU made its binaries only: the release keeps the stored source package and
            # Architecture: all packages
            for name in [*stored.source_files, BINNMU_CHANGELOG]:
                (pkg_dir / name).unlink()
            for name in [*stored.source_files, *stored.arch_all_files]:
                link_or_copy(stored.directory / name, outdir / name)
            self.source_files = list(stored.source_files)

        for file in pkg_dir.iterdir():
            if file.is_file():
                file.rename(outdir / file.name)
        rmtree(pkg_dir)
        if stored is None:
            self.arch_all_files = sorted(path.name for path in outdir.glob('*_all.deb'))
        else:
            self.arch_all_files = list(stored.arch_all_files)


# A spec's Release lines, its %changelog line, and the date of a changelog entry, in the short
# (`Thu Nov 05 2026`) or the long form (`Thu Nov 05 12:00:00 UTC 2026`).
_RELEASE = re.compile(r'^(?P<tag>Release\s*:\s*)\S.*?[ \t]*$', re.IGNORECASE | re.MULTILINE)
_CHANGELOG = re.compile(r'^%changelog[ \t]*\n', re.MULTILINE)
_ENTRY_DATE = re.compile(
    r'\*\s+(?P<date>\w{3}\s+\w{3}\s+\d{1,2}(?:\s+\d\d:\d\d:\d\d(?:\s+\S+)?)?\s+\d{4})\s'
)


class RpmPackageBuilder(PackageBuilder):
    """The build of a source rpm, and its binary rpms, with rpmbuild."""

    family = 'rpm'

    def __init__(
        self,
        context: BuildContext,
        dist_release: Release,
        base_dir: Path,
        gpg: Signer,
        name: str,
        *,
        spec: SpecInfo | SpecFailed,
    ) -> None:
        """The build of base_dir/name/name.spec, which the release's rpm read as spec says."""
        super().__init__(context, dist_release, base_dir, gpg, name)
        self._spec = self.spec_file(base_dir, name)
        self._spec_info = spec
        self._sources: list[str] = []
        if isinstance(spec, SpecInfo):
            self.bin_depends = set(spec.buildrequires)
            self.bin_provides = set(spec.provides)
            self._sources = list(spec.sources)

    @staticmethod
    def spec_file(base_dir: Path, name: str) -> Path:
        """The spec of the package name of base_dir."""
        return base_dir / name / (name + '.spec')

    @classmethod
    def applies(cls, base_dir: Path, name: str) -> bool:
        """Whether the package has a spec."""
        return cls.spec_file(base_dir, name).is_file()

    def sources(self) -> list[str]:
        """The URLs of the spec's Source: lines."""
        return list(self._sources)

    def _prepare(self) -> None:
        """Raise when the release's rpm could not read the spec."""
        if isinstance(self._spec_info, SpecFailed):
            raise ValueError(self._spec_info.message)

    def _info(self) -> SpecInfo:
        assert isinstance(self._spec_info, SpecInfo)  # _prepare raised otherwise
        return self._spec_info

    def _packaging(self) -> Path:
        """The spec, as written."""
        return self._spec

    def _source_files(self, files: Files) -> dict[str, Path]:
        """The files of the spec's Source: lines, by name."""
        found: dict[str, Path] = {}
        for url in self._sources:
            file = files[url]
            assert isinstance(file, Path)  # build raised the download's failure
            found[source_name(url)] = file
        return found

    def _base_version(self) -> str:
        """The spec's Version-Release, expanded by the release's rpm."""
        return f'{self._info().version}-{self._info().release}'

    def _below(self, version: str, other: str) -> bool:
        """Rpm's order."""
        return rpm_compare(version, other) < 0

    def _version(self, rebuild: int) -> str:
        """The spec's Version, and its Release with `.<rebuild>` after it."""
        return f'{self._info().version}-{rpm_release(self._info().release, rebuild)}'

    def _build(self, wk_dir: Path, outdir: Path, deps: Set[PackageBuilder], files: Files) -> None:
        pkg_dir = wk_dir / 'pkg'
        pkg_dir.mkdir()
        spec = self._spec.read_text()
        (pkg_dir / self._spec.name).write_text(spec if self.rebuild == 0 else self._rebuilt(spec))

        localrepo = wk_dir / 'repo'
        self._local_repository(RpmRepoBuilder, localrepo, deps)

        for url in self._sources:
            file = files[url]
            assert isinstance(file, Path)  # build raised the download's failure
            link_or_copy(file, pkg_dir / source_name(url))

        self._run_builder(
            deps_mounts=[
                Mount('/src', pkg_dir, read_only=True),
                Mount('/localrepo', localrepo / version(self.dist_release), read_only=True),
            ],
            build_mounts=[Mount('/src', pkg_dir, read_only=True), Mount('/output', outdir)],
            outputs='/output',
        )
        rmtree(localrepo)
        rmtree(pkg_dir)
        if self.unchanged:  # nothing built: the stored packages stand
            return

        rpms = sorted(file.name for file in outdir.iterdir() if file.suffix == '.rpm')
        if rpms:
            RpmSign(self._context, self.logger).sign(self._gpg, outdir, rpms)
        self.source_files = [name for name in rpms if name.endswith('.src.rpm')]
        self.arch_all_files = []

    def _rebuilt(self, spec: str) -> str:
        """The spec of the rebuild-th rebuild: its Release `.<n>` after, and why, in %changelog.

        Every Release line becomes the release the base version was read from, as the release's
        rpm expanded it, `.<n>` after: a macro (`%autorelease`, which takes what follows it as its
        arguments) or a conditional choosing between lines cannot drop the suffix. The changelog
        entry is dated as the spec's top one, so that the same inputs give the same packages: rpm
        takes the build time from that date. A spec without %changelog, or whose first entry has
        no date (`%autochangelog`), gets none: nothing in it dates one.
        """
        release = rpm_release(self._info().release, self.rebuild)
        spec, count = _RELEASE.subn(lambda line: line['tag'] + release, spec)
        if count == 0:
            raise ValueError(f'{self._spec.name} has no Release line')
        changelog = _CHANGELOG.search(spec)
        if changelog is None:
            return spec
        top = _ENTRY_DATE.match(spec[changelog.end() :].lstrip())
        if top is None:
            return spec
        entry = rpm_changelog_entry(self._info().version, release, self._gpg.uid, top['date'])
        return spec[: changelog.end()] + entry + spec[changelog.end() :]


@dataclass(frozen=True)
class Public:
    """Where a repository is served, and how it names itself."""

    url: str
    """Its URL, without a trailing slash."""
    name: str
    """Its short name, in its keyring and .repo file names."""
    description: str
    """Its label."""

    @classmethod
    def of(cls, url: str, name: str | None, description: str | None) -> 'Public':
        """The repository at url; its name the URL's last path component by default."""
        url = url.rstrip('/')
        name = name if name is not None else Path(urlparse(url).path).name
        return cls(url, name, description if description is not None else name)


# The releases whose publication failed, and why.
Failures = dict[Release, Exception]


@dataclass(frozen=True)
class Output:
    """The packages of one build for one release, as a repository publishes them."""

    release: Release
    """The release they are built for."""
    directory: Path
    """The directory holding them: a build's output, or a package of a release's state."""


class DebRepoBuilder:
    """A deb repository, published by aptly, its Release files signed by the Signer."""

    def __init__(
        self,
        context: BuildContext,
        output_dir: Path,
        gpg: Signer,
        url: str | None = None,
        name: str | None = None,
        description: str | None = None,
        pages: jinja2.Environment | None = None,
        extras: Sequence[tuple[ExtraRepository, str]] = (),
        logger: logging.Logger | None = None,
        sign: bool = True,
    ) -> None:
        """The repository in output_dir; with a url, its keyring and its page from pages."""
        self._context = context
        self._output_dir = output_dir
        self._gpg = gpg
        self._public = None if url is None else Public.of(url, name, description)
        self._pages = pages
        self._extras = extras
        self._aptly = Aptly(context, logger)
        self._sign = sign

    def has_repository(self, dist_release: Release) -> bool:
        """Whether the output directory has a repository of dist_release already."""
        return (self._output_dir / 'dists' / codename(dist_release) / 'Release').is_file()

    def build(self, outputs: Iterable[Output], kept: Set[Release] = frozenset()) -> Failures:
        """Publish the releases of outputs, each on its own; the releases whose publication failed.

        The page lists the releases published, and those of kept (left as they were) whose
        repository is still there. Without a release to publish, nothing is touched.
        """
        built = [
            o for o in outputs if targets.is_deb(o.release.distribution) and o.directory.is_dir()
        ]
        if not built:
            return {}

        published: list[Release] = []
        failures: Failures = {}
        with TemporaryDirectory(prefix='PKGBAPTLY.', dir=self._context.workdir) as tmp:
            tmp_dir = Path(tmp)
            (tmp_dir / 'aptly.conf').write_text(json.dumps(APTLY_CONFIG))
            self._output_dir.mkdir(parents=True, exist_ok=True)

            def aptly(*args: str, data: Path | None = None) -> None:
                mounts = [Mount('/aptlyroot', tmp_dir), Mount('/aptlyout', self._output_dir)]
                if data is not None:
                    mounts.append(Mount('/data', data))
                self._aptly(
                    'aptly', '-config=/aptlyroot/aptly.conf', *args, shm_size='5g', mounts=mounts
                )

            for dist_release in sorted({o.release for o in built}, key=str):
                try:
                    self._publish(
                        aptly,
                        dist_release,
                        [o.directory for o in built if o.release == dist_release],
                    )
                    published.append(dist_release)
                except Exception as error:
                    failures[dist_release] = error

        if published and self._public is not None:
            public = self._public
            keyring = self._output_dir / f'{public.name}-archive-keyring.gpg'
            keyring.write_bytes(self._gpg.public_key(armored=False))
            listed = set(published) | {r for r in kept | failures.keys() if self.has_repository(r)}
            _write_page(self._output_dir, self._pages, 'deb', public, listed, self._extras)
        return failures

    def _publish(
        self, aptly: Callable[..., None], dist_release: Release, directories: list[Path]
    ) -> None:
        """Publish dist_release, with the packages of directories, under its codename and suite."""
        aptly('repo', 'create', dist_release.uid)
        for directory in directories:
            aptly('repo', 'add', dist_release.uid, '/data', data=directory)
        publish_repo = ['publish', 'repo', '-force-overwrite', '-skip-signing']
        if self._public is not None:
            publish_repo += [
                '-label=' + self._public.description,
                '-origin=' + self._public.name,
            ]

        # like the Debian archive, publish the release under its codename and under its
        # suite, each Release file naming both so that apt accepts either in sources.list
        names = ['-codename=' + codename(dist_release)]
        if dist_release.suite is not None:
            names.append('-suite=' + dist_release.suite)
        for distribution in [dist_release.codename, dist_release.suite]:
            if distribution is not None:
                aptly(
                    *publish_repo,
                    *names,
                    '-distribution=' + distribution,
                    dist_release.uid,
                    'filesystem:default:',
                )
                if self._sign:
                    self._sign_release(self._output_dir / 'dists' / distribution / 'Release')
        # the .buildinfo beside its .dsc, which aptly does not publish: the environment a package
        # was built in, for anyone to rebuild it there
        for directory in directories:
            for buildinfo in directory.glob('*.buildinfo'):
                for dsc in directory.glob('*.dsc'):
                    for in_pool in (self._output_dir / 'pool').rglob(dsc.name):
                        copy2(buildinfo, in_pool.parent / buildinfo.name)

    def _sign_release(self, release: Path) -> None:
        """Write Release.gpg and InRelease beside release, whose trailing spaces go first.

        A clear signature drops them (RFC 9580), so that InRelease's text would otherwise differ
        from Release: apt reads either.
        """
        text = b'\n'.join(line.rstrip(b' \t') for line in release.read_bytes().split(b'\n'))
        release.write_bytes(text)
        (release.parent / 'Release.gpg').write_bytes(self._gpg.detached(text))
        (release.parent / 'InRelease').write_bytes(self._gpg.clearsign(text))


class RpmRepoBuilder:
    """An rpm repository: one directory per release, its metadata signed by the Signer."""

    def __init__(
        self,
        context: BuildContext,
        output_dir: Path,
        gpg: Signer,
        url: str | None = None,
        name: str | None = None,
        description: str | None = None,
        pages: jinja2.Environment | None = None,
        extras: Sequence[tuple[ExtraRepository, str]] = (),
        logger: logging.Logger | None = None,
        sign: bool = True,
    ) -> None:
        """The repository in output_dir; with a url, its key, .repo files and page from pages."""
        self._context = context
        self._output_dir = output_dir
        self._gpg = gpg
        self._public = None if url is None else Public.of(url, name, description)
        self._pages = pages
        self._extras = extras
        self._sign = sign

    def has_repository(self, dist_release: Release) -> bool:
        """Whether the output directory has a repository of dist_release already."""
        return (self._output_dir / version(dist_release) / 'repodata' / 'repomd.xml').is_file()

    def build(self, outputs: Iterable[Output], kept: Set[Release] = frozenset()) -> Failures:
        """Publish the releases of outputs, each on its own; the releases whose publication failed.

        The page lists the releases published, and those of kept (left as they were) whose
        repository is still there. Without a release to publish, nothing is touched.
        """
        built = [
            (o.release, o.directory)
            for o in outputs
            if targets.is_rpm(o.release.distribution) and o.directory.is_dir()
        ]
        if not built:
            return {}

        releases: set[Release] = set()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        for dist_release, outdir in built:
            release_dir = self._output_dir / version(dist_release)
            release_dir.mkdir(parents=True, exist_ok=True)
            for file in outdir.iterdir():
                if file.suffix == '.rpm' and len(file.suffixes) >= 2:
                    releases.add(dist_release)
                    rpmdir = release_dir / file.suffixes[-2][1:]
                    rpmdir.mkdir(exist_ok=True)
                    link_or_copy(file, rpmdir / file.name)

        published: list[Release] = []
        failures: Failures = {}
        for dist_release in sorted(releases, key=str):
            try:
                self._publish(dist_release)
                published.append(dist_release)
            except Exception as error:
                failures[dist_release] = error

        if published and self._public is not None:
            public = self._public
            key = self._output_dir / f'{public.name}.key'
            key.write_bytes(self._gpg.public_key(armored=True))
            listed = set(published) | {r for r in kept | failures.keys() if self.has_repository(r)}
            _write_page(self._output_dir, self._pages, 'rpm', public, listed, self._extras)
        return failures

    def _publish(self, dist_release: Release) -> None:
        """Write the metadata of dist_release's directory, signed, and its .repo file."""
        release_dir = self._output_dir / version(dist_release)
        repodata.write(release_dir)
        if self._sign:
            repomd = release_dir / 'repodata' / 'repomd.xml'
            repomd.with_name('repomd.xml.asc').write_bytes(self._gpg.detached(repomd.read_bytes()))
        if self._public is not None:
            public = self._public
            repo = ConfigParser()
            repo.add_section(public.name)
            repo.set(public.name, 'name', f'{public.description} ({dist_release})')
            repo.set(public.name, 'type', 'rpm-md')
            repo.set(public.name, 'baseurl', f'{public.url}/{version(dist_release)}')
            repo.set(public.name, 'gpgcheck', '1')
            repo.set(public.name, 'repo_gpgcheck', '1')
            repo.set(public.name, 'gpgkey', f'{public.url}/{public.name}.key')
            repo.set(public.name, 'enabled', '1')
            with (release_dir / f'{public.name}.repo').open('w') as repo_file:
                repo.write(repo_file, space_around_delimiters=False)


def _write_page(
    output_dir: Path,
    pages: jinja2.Environment | None,
    family: Literal['deb', 'rpm'],
    public: Public,
    listed: Set[Release],
    extras: Sequence[tuple[ExtraRepository, str]] = (),
) -> None:
    """Write index.html, the page of the releases listed, if there are templates."""
    if pages is not None:
        page = readme.Page.of(family, public.url, public.name, public.description, listed, extras)
        (output_dir / 'index.html').write_text(readme.render(pages, page))


def run_builds(
    context: BuildContext,
    reporter: Reporter,
    config: BuildConfig,
    selected: Mapping[str, Set[Release]],
    disabled: Mapping[str, Set[Release]],
    signer_for: Callable[[Distrib], Signer],
    unbuilt: Set[Release] = frozenset(),
    download_dir: Path | None = None,
    state_dir: Path | None = None,
    *,
    publish: bool = True,
    build: bool = True,
    all_releases: Mapping[str, Set[Release]] | None = None,
) -> bool:
    """Download, build and publish; whether any build or publication did not succeed.

    unbuilt are releases failed before any build (their builder image is missing): their
    repositories are left as they were, and stay listed, as those of the releases whose builds fail.
    download_dir, if given, keeps the downloads from run to run; a temporary one otherwise.
    state_dir holds the releases' states (a temporary one by default: nothing to reuse): each build
    starts from what its release's state stores of its package, and once every build is settled,
    a release all of whose builds were built or reused has its state replaced by them; the others
    keep theirs.

    Without build, nothing is built: the repositories are published from the states. Without
    publish, no repository is written. Publishing from a state_dir given removes the repositories
    and writes them again from the state of every release of all_releases (the configuration's,
    selected releases by default), so that they hold, and their pages list, the releases with a
    state; with the temporary one, only the releases of this run are published, and the
    repositories of the others are left as they were.
    """
    states = state_dir if state_dir is not None else context.workdir / 'state'
    builders: list[PackageBuilder] = []
    repo_builders: list[tuple[str, DebRepoBuilder | RpmRepoBuilder, list[PackageBuilder]]] = []
    output_dirs: set[Path] = set()
    root = config.root
    pages = readme.environment(readme.project_dir(root))
    for dist_id, distrib in config.distribs.items():
        gpg = signer_for(distrib)
        deb = targets.is_deb(targets.target(dist_id))
        dist_builders: list[PackageBuilder] = []
        for dist_release in sorted(selected[dist_id] if build else (), key=lambda r: r.uid):
            if deb:
                dist_builders += [
                    DebPackageBuilder(context, dist_release, root, gpg, name)
                    for name in config.packages
                    if dist_release not in disabled[name] and DebPackageBuilder.applies(root, name)
                ]
                continue
            names = [
                name
                for name in config.packages
                if dist_release not in disabled[name] and RpmPackageBuilder.applies(root, name)
            ]
            # every spec of the release, read by the release's own rpm, in one container
            specs = rpmquery.query(
                context,
                REGISTRY + builder_image(dist_release),
                {name: RpmPackageBuilder.spec_file(root, name) for name in names},
            )
            dist_builders += [
                RpmPackageBuilder(context, dist_release, root, gpg, name, spec=specs[name])
                for name in names
            ]

        repository = distrib.repository
        output_dirs.add(root / repository.path)
        repo_family = DebRepoBuilder if deb else RpmRepoBuilder
        repo_builder = repo_family(
            context,
            root / repository.path,
            gpg,
            repository.url,
            repository.name,
            repository.description,
            pages,
            [
                (extra, signing.fingerprint((root / extra.key).read_bytes()))
                for extra in distrib.extra_repositories
            ],
        )
        for builder in dist_builders:
            builder.extras = distrib.extra_repositories
            stored = ReleaseState(states, RELEASE_CHANNEL, builder.dist_release.uid)
            builder.stored = stored.package(builder.name)
        repo_builders.append((dist_id, repo_builder, dist_builders))
        builders += dist_builders

    debs = [b for b in builders if isinstance(b, DebPackageBuilder) and b.needs_orig]
    keep_dir = download_dir or context.workdir
    origs = fetch_origs(context, root, sorted({b.name for b in debs}), keep_dir / 'origs')
    for deb_builder in debs:
        deb_builder.orig = origs[deb_builder.name]
    downloads = keep_dir / 'downloads'
    downloads.mkdir(parents=True, exist_ok=True)
    files = fetch_all(
        [url for builder in builders for url in builder.sources()],
        downloads,
        jobs=context.jobs,
        cancellation=context.cancellation,
    )

    graph = dependencies(builders)
    reporter.expect(len(builders))
    outcomes = schedule(
        graph,
        lambda builder: builder.build(closure(graph, builder), files),
        jobs=context.jobs,
        cancellation=context.cancellation,
        on_start=lambda builder: reporter.started(builder.name, builder.dist_release),
        on_finish=lambda builder, outcome: reporter.finished(
            builder.name, builder.dist_release, outcome
        ),
    )
    reporter.summarize({(b.name, b.dist_release): outcome for b, outcome in outcomes.items()})

    releases = published(outcomes)
    _commit_states(states, [b for b in builders if b.dist_release in releases])
    if build:
        for release in sorted({b.dist_release for b in builders} - releases | unbuilt, key=str):
            reporter.state_kept(release)
    failed = any(not isinstance(outcome, Succeeded | Reused) for outcome in outcomes.values())
    if not publish:
        return failed

    # with states that last, the repositories are theirs alone, written again
    from_states = state_dir is not None
    if from_states:
        for output_dir in output_dirs:
            rmtree(output_dir, ignore_errors=True)
    for dist_id, repo_builder, dist_builders in repo_builders:
        if from_states:
            to_publish = (all_releases or selected)[dist_id]
            kept: set[Release] = set()
        else:
            to_publish = {b.dist_release for b in dist_builders} & releases
            kept = {b.dist_release for b in dist_builders} - releases
            kept |= {r for r in unbuilt if r.distribution.id == dist_id}
        outputs: list[Output] = []
        for release in sorted(to_publish, key=str):
            stored = ReleaseState(states, RELEASE_CHANNEL, release.uid)
            if not stored.exists():
                reporter.no_state(release)
                continue
            outputs += [Output(release, p.directory) for _, p in sorted(stored.packages().items())]
        try:
            failures = {
                str(release): error for release, error in repo_builder.build(outputs, kept).items()
            }
        except Exception as error:
            failures = {dist_id: error}
        for what, failure in failures.items():
            reporter.publication_failed(what, failure)
            failed = True
    return failed


def _commit_states(states: Path, builders: Sequence[PackageBuilder]) -> None:
    """Make the builds of each of their releases its state, the packages of no build dropped."""
    for release in sorted({b.dist_release for b in builders}, key=str):
        writer = ReleaseStateWriter(states, RELEASE_CHANNEL, release.uid)
        for builder in (b for b in builders if b.dist_release == release):
            assert builder.outdir is not None and builder.env_key is not None  # built or reused
            writer.add(
                builder.name,
                static_key=builder.static_key,
                env_key=builder.env_key,
                base_version=builder.base_version,
                rebuild=builder.rebuild,
                version=builder.version,
                snapshot_commit=None,
                packaging_hash=builder.packaging_hash,
                outdir=builder.outdir,
                source_files=builder.source_files,
                arch_all_files=builder.arch_all_files,
            )
        writer.commit()


def write_sources(
    context: BuildContext,
    config: BuildConfig,
    selected: Mapping[str, Set[Release]],
    disabled: Mapping[str, Set[Release]],
    names: Sequence[str],
) -> dict[str, DownloadFailed | None]:
    """Write the sources file of each package named: the line of each file its builds download.

    The deb upstream tarball comes from uscan; the rpm sources are those the spec names in the
    first selected release of each rpm distribution that builds the package. A package that
    downloads nothing gets no file.

    Returns:
        The packages whose sources could not all be downloaded, and why (theirs is not written),
        and the packages that download nothing for the releases selected, with None.
    """
    root = config.root
    files: dict[str, dict[str, Path]] = {name: {} for name in names}
    failures: dict[str, DownloadFailed] = {}
    quilt = [
        name
        for name in names
        if DebPackageBuilder.applies(root, name)
        and source_format(root / name / 'debian') == '3.0 (quilt)'
        and any(
            targets.is_deb(targets.target(dist_id)) and selected[dist_id] - disabled[name]
            for dist_id in config.distribs
        )
    ]
    for name, orig in fetch_origs(context, root, quilt, context.workdir / 'origs').items():
        if isinstance(orig, DownloadFailed):
            failures[name] = orig
        else:
            files[name][orig.upstream.name] = orig.upstream
    urls: dict[str, list[str]] = {}
    for dist_id in config.distribs:
        if targets.is_deb(targets.target(dist_id)):
            continue
        for name in names:
            releases = sorted(selected[dist_id] - disabled[name], key=lambda r: r.uid)
            if not releases or not RpmPackageBuilder.applies(root, name):
                continue
            try:
                spec = rpmquery.query(
                    context,
                    REGISTRY + builder_image(releases[0]),
                    {name: RpmPackageBuilder.spec_file(root, name)},
                )[name]
            except ContainerFailed as error:
                failures[name] = DownloadFailed(name, f'its spec could not be read: {error}')
                continue
            if isinstance(spec, SpecFailed):
                failures[name] = DownloadFailed(name, spec.message)
                continue
            urls.setdefault(name, []).extend(spec.sources)
    downloads = context.workdir / 'downloads'
    downloads.mkdir()
    fetched = fetch_all(
        [url for found in urls.values() for url in found],
        downloads,
        jobs=context.jobs,
        cancellation=context.cancellation,
    )
    for name, found in urls.items():
        for url in found:
            file = fetched[url]
            if isinstance(file, DownloadFailed):
                failures.setdefault(name, file)
            else:
                files[name][source_name(url)] = file
    result: dict[str, DownloadFailed | None] = dict(failures)
    for name in names:
        if name in failures:
            continue
        if not files[name]:
            result[name] = None
            continue
        lines = [sources.line(file, path) for file, path in sorted(files[name].items())]
        (root / name / sources.FILE).write_text(''.join(f'{line}\n' for line in lines))
    return result
