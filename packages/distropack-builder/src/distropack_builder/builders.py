# SPDX-FileCopyrightText: 2020-2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The builds of the packages, in their release's builder image, and their repositories.

A package builder builds one package for one release; a repository builder publishes the
outputs of a family's builds, signed, with its page. run_builds drives a whole run.
"""

import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence, Set
from configparser import ConfigParser
from dataclasses import dataclass
from datetime import UTC, datetime
from os import link
from pathlib import Path
from shutil import copy2, copytree, rmtree
from tempfile import TemporaryDirectory
from typing import Any, ClassVar, Literal
from urllib.parse import urlparse

import jinja2
from debian.changelog import Changelog, format_date
from debian.deb822 import Deb822, _PkgRelationMixin

from distro_releases import Release
from distropack_builder import readme, repodata, rpmquery, targets
from distropack_builder.bake import DEB_TOOLS, PROTOCOL, REGISTRY, tool_image
from distropack_builder.config import BuildConfig, Distrib
from distropack_builder.context import BuildContext
from distropack_builder.docker import Mount
from distropack_builder.downloads import DownloadFailed, fetch_all, orig_name, source_name
from distropack_builder.graph import closure, dependencies
from distropack_builder.publication import published
from distropack_builder.report import Reporter, build_logger
from distropack_builder.rpmquery import SpecFailed, SpecInfo
from distropack_builder.scheduler import Succeeded, schedule
from distropack_builder.signing import Signer
from distropack_builder.versions import deb_version

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
    ) -> None:
        """Run the image with args; raises ContainerFailed if it fails."""
        self._context.run(
            self._context.engine,
            self._image,
            args,
            mounts=mounts,
            env=env,
            shm_size=shm_size,
            as_root=as_root,
            cancellation=self._context.cancellation,
            logger=self._logger,
        )


# The configuration of aptly: its database in /aptlyroot, what it publishes in /aptlyout. It reads
# the clear-signed .dsc with its own OpenPGP code: gpg needs a home to write to, which the user a
# tool container runs as under rootful Docker does not have in the image.
APTLY_CONFIG = {
    'architectures': ['amd64', 'source'],
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


class PackageBuilder:
    """The build of one package for one release, the base of the builders of each family."""

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

    def build(self, deps: Set['PackageBuilder'], files: Files) -> None:
        """Build, with the outputs of deps in the local repository, and files downloaded."""
        for url in self.sources():
            failure = files[url]
            if isinstance(failure, DownloadFailed):
                raise failure
        wk_dir = self._context.workdir / f'{self.name}.{self.dist_release.uid}.{self.family}-build'
        wk_dir.mkdir()
        self.outdir = wk_dir / 'output'
        self.outdir.mkdir()
        self._build(wk_dir, self.outdir, set(deps) | {self}, files)

    def _build(self, wk_dir: Path, outdir: Path, deps: Set['PackageBuilder'], files: Files) -> None:
        """Build in wk_dir, the packages written to outdir."""
        raise NotImplementedError

    def _local_repository(
        self,
        family: type['DebRepoBuilder'] | type['RpmRepoBuilder'],
        localrepo: Path,
        deps: Set['PackageBuilder'],
    ) -> None:
        """Publish the outputs of deps at localrepo, unsigned, for the builder container."""
        failures = family(
            self._context, localrepo, self._gpg, logger=self.logger, sign=False
        ).build(deps)
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
        orig_url: str | None = None,
    ) -> None:
        """The build of base_dir/name/debian; orig_url, build.conf's orig:, for 3.0 (quilt)."""
        super().__init__(context, dist_release, base_dir, gpg, name)
        self._debian_dir = base_dir / name / 'debian'
        self._orig_url = orig_url
        self._format = source_format(self._debian_dir)
        with (self._debian_dir / 'changelog').open() as raw_changelog:
            self._changelog = Changelog(raw_changelog)

        deps_kind = ['build-depends', 'build-depends-indep', 'build-depends-arch']
        with (self._debian_dir / 'control').open() as raw_control:
            control_paragraphs = Deb822.iter_paragraphs(
                raw_control, strict={'whitespace-separates-paragraphs': False}
            )
            self.bin_provides = {pkg['Package'] for pkg in control_paragraphs if 'Package' in pkg}
        with (self._debian_dir / 'control').open() as raw_control:
            control = Control(raw_control)
            self.bin_depends = {
                om['name'] for dl in deps_kind for am in control.relations[dl] or () for om in am
            }

    @classmethod
    def applies(cls, base_dir: Path, name: str) -> bool:
        """Whether the package has a debian/ directory."""
        return (base_dir / name / 'debian').is_dir()

    def _orig(self) -> str | None:
        """The URL of the upstream tarball, for a 3.0 (quilt) package that names one."""
        if self._format != '3.0 (quilt)' or self._orig_url is None:
            return None
        return self._orig_url.format(
            upstream_version=self._changelog.upstream_version, package=self._changelog.package
        )

    def sources(self) -> list[str]:
        """The upstream tarball, if the package has one."""
        orig = self._orig()
        return [] if orig is None else [orig]

    def _build(self, wk_dir: Path, outdir: Path, deps: Set[PackageBuilder], files: Files) -> None:
        pkg_dir = wk_dir / 'pkg'
        pkg_dir.mkdir()
        release_codename = codename(self.dist_release)

        # a fresh copy of the changelog: each release adds its own entry
        with (self._debian_dir / 'changelog').open() as raw_changelog:
            changelog = Changelog(raw_changelog)
        source_dir = pkg_dir / f'{changelog.package}-{changelog.upstream_version}'
        if self._format == '3.0 (quilt)':
            if self._orig_url is None:
                raise ValueError('a 3.0 (quilt) package needs an orig: URL in build.conf')
            debian_dir = Path(copytree(self._debian_dir, source_dir / 'debian'))
        elif self._format == '3.0 (native)':
            debian_dir = Path(copytree(self._debian_dir.parent, source_dir)) / 'debian'
        else:
            raise ValueError(f'unsupported deb source format: {self._format}')

        # the rebuild's own changelog entry
        changelog.new_block(
            package=changelog.package,
            version=deb_version(
                str(changelog.version), changelog.date, self.dist_release, datetime.now(UTC)
            ),
            distributions=release_codename,
            urgency=changelog.urgency,
            changes=['  * Rebuild for ' + str(self.dist_release)],
            author=self._gpg.uid,
            date=format_date(),
        )
        with (debian_dir / 'changelog').open('w') as raw_changelog:
            changelog.write_to_open_file(raw_changelog)

        orig = self._orig()
        if orig is not None:
            file = files[orig]
            assert isinstance(file, Path)  # build raised the download's failure
            # the upstream version of the entry packaged, not of the rebuild's new one
            name = orig_name(changelog.package, self._changelog.upstream_version, orig, file)
            link_or_copy(file, pkg_dir / name)

        localrepo = wk_dir / 'repo'
        self._local_repository(DebRepoBuilder, localrepo, deps)

        profiles = f'{self.dist_release.distribution.id},{release_codename}'
        Container(self._context, builder_image(self.dist_release), self.logger)(
            f'-j{self._context.jobs}',
            '-sa',
            f'-P{profiles}',
            mounts=[Mount('/src', pkg_dir), Mount('/localrepo', localrepo)],
            shm_size='5g',
            as_root=True,
            env={'DH_VERBOSE': '1', 'DISTROPACK_BUILD_PROFILES': profiles},
        )
        rmtree(localrepo)

        # Sign the source package: nothing reads the .changes, aptly adds the .dsc as it is
        for dsc in pkg_dir.glob('*.dsc'):
            dsc.write_bytes(self._gpg.clearsign(dsc.read_bytes()))

        for file in pkg_dir.iterdir():
            if file.is_file():
                file.rename(outdir / file.name)
        rmtree(pkg_dir)


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

    def _build(self, wk_dir: Path, outdir: Path, deps: Set[PackageBuilder], files: Files) -> None:
        if isinstance(self._spec_info, SpecFailed):
            raise ValueError(self._spec_info.message)
        pkg_dir = wk_dir / 'pkg'
        pkg_dir.mkdir()
        copy2(self._spec, pkg_dir / self._spec.name)

        localrepo = wk_dir / 'repo'
        self._local_repository(RpmRepoBuilder, localrepo, deps)

        for url in self._sources:
            file = files[url]
            assert isinstance(file, Path)  # build raised the download's failure
            link_or_copy(file, pkg_dir / source_name(url))

        Container(self._context, builder_image(self.dist_release), self.logger)(
            mounts=[
                Mount('/src', pkg_dir, read_only=True),
                Mount('/output', outdir),
                Mount('/localrepo', localrepo / version(self.dist_release), read_only=True),
            ],
            shm_size='5g',
            as_root=True,
        )
        rmtree(localrepo)

        rpms = sorted(file.name for file in outdir.iterdir() if file.suffix == '.rpm')
        if rpms:
            RpmSign(self._context, self.logger).sign(self._gpg, outdir, rpms)
        rmtree(pkg_dir)


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
        logger: logging.Logger | None = None,
        sign: bool = True,
    ) -> None:
        """The repository in output_dir; with a url, its keyring and its page from pages."""
        self._context = context
        self._output_dir = output_dir
        self._gpg = gpg
        self._public = None if url is None else Public.of(url, name, description)
        self._pages = pages
        self._aptly = Aptly(context, logger)
        self._sign = sign

    def has_repository(self, dist_release: Release) -> bool:
        """Whether the output directory has a repository of dist_release already."""
        return (self._output_dir / 'dists' / codename(dist_release) / 'Release').is_file()

    def build(self, pkgs: Iterable[PackageBuilder], kept: Set[Release] = frozenset()) -> Failures:
        """Publish the releases of pkgs, each on its own; the releases whose publication failed.

        The page lists the releases published, and those of kept (left as they were) whose
        repository is still there. Without a release to publish, nothing is touched.
        """
        built = [
            p
            for p in pkgs
            if targets.is_deb(p.dist_release.distribution)
            and p.outdir is not None
            and p.outdir.is_dir()
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

            for dist_release in sorted({p.dist_release for p in built}, key=str):
                try:
                    self._publish(
                        aptly, dist_release, [p for p in built if p.dist_release == dist_release]
                    )
                    published.append(dist_release)
                except Exception as error:
                    failures[dist_release] = error

        if published and self._public is not None:
            public = self._public
            keyring = self._output_dir / f'{public.name}-archive-keyring.gpg'
            keyring.write_bytes(self._gpg.public_key(armored=False))
            listed = set(published) | {r for r in kept | failures.keys() if self.has_repository(r)}
            _write_page(self._output_dir, self._pages, 'deb', public, listed)
        return failures

    def _publish(
        self, aptly: Callable[..., None], dist_release: Release, pkgs: list[PackageBuilder]
    ) -> None:
        """Publish dist_release, with the outputs of pkgs, under its codename and its suite."""
        aptly('repo', 'create', dist_release.uid)
        for pkg in pkgs:
            aptly('repo', 'add', dist_release.uid, '/data', data=pkg.outdir)
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
        logger: logging.Logger | None = None,
        sign: bool = True,
    ) -> None:
        """The repository in output_dir; with a url, its key, .repo files and page from pages."""
        self._context = context
        self._output_dir = output_dir
        self._gpg = gpg
        self._public = None if url is None else Public.of(url, name, description)
        self._pages = pages
        self._sign = sign

    def has_repository(self, dist_release: Release) -> bool:
        """Whether the output directory has a repository of dist_release already."""
        return (self._output_dir / version(dist_release) / 'repodata' / 'repomd.xml').is_file()

    def build(self, pkgs: Iterable[PackageBuilder], kept: Set[Release] = frozenset()) -> Failures:
        """Publish the releases of pkgs, each on its own; the releases whose publication failed.

        The page lists the releases published, and those of kept (left as they were) whose
        repository is still there. Without a release to publish, nothing is touched.
        """
        built = [
            (p.dist_release, p.outdir)
            for p in pkgs
            if targets.is_rpm(p.dist_release.distribution)
            and p.outdir is not None
            and p.outdir.is_dir()
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
            _write_page(self._output_dir, self._pages, 'rpm', public, listed)
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
) -> None:
    """Write index.html, the page of the releases listed, if there are templates."""
    if pages is not None:
        page = readme.Page.of(family, public.url, public.name, public.description, listed)
        (output_dir / 'index.html').write_text(readme.render(pages, page))


def run_builds(
    context: BuildContext,
    reporter: Reporter,
    config: BuildConfig,
    selected: Mapping[str, Set[Release]],
    disabled: Mapping[str, Set[Release]],
    signer_for: Callable[[Distrib], Signer],
) -> bool:
    """Download, build and publish; whether any build or publication did not succeed."""
    builders: list[PackageBuilder] = []
    repo_builders: list[tuple[str, DebRepoBuilder | RpmRepoBuilder, list[PackageBuilder]]] = []
    root = config.root
    pages = readme.environment(readme.project_dir(root))
    for dist_id, distrib in config.distribs.items():
        gpg = signer_for(distrib)
        deb = targets.is_deb(targets.target(dist_id))
        dist_builders: list[PackageBuilder] = []
        for dist_release in sorted(selected[dist_id], key=lambda release: release.uid):
            if deb:
                dist_builders += [
                    DebPackageBuilder(
                        context, dist_release, root, gpg, name, config.packages[name].orig
                    )
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
        repo_family = DebRepoBuilder if deb else RpmRepoBuilder
        repo_builder = repo_family(
            context,
            root / repository.path,
            gpg,
            repository.url,
            repository.name,
            repository.description,
            pages,
        )
        repo_builders.append((dist_id, repo_builder, dist_builders))
        builders += dist_builders

    downloads = context.workdir / 'downloads'
    downloads.mkdir()
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
    failed = any(not isinstance(outcome, Succeeded) for outcome in outcomes.values())
    for dist_id, repo_builder, dist_builders in repo_builders:
        kept = {b.dist_release for b in dist_builders} - releases
        try:
            failures = {
                str(release): error
                for release, error in repo_builder.build(
                    [b for b in dist_builders if b.dist_release in releases], kept
                ).items()
            }
        except Exception as error:
            failures = {dist_id: error}
        for what, failure in failures.items():
            reporter.publication_failed(what, failure)
            failed = True
    return failed
