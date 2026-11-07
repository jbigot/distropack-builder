from collections.abc import Callable, Mapping, Set as AbstractSet
from configparser import ConfigParser
from datetime import UTC, datetime
import json
import logging
from os import link
from pathlib import Path
from shutil import copy2, copytree, rmtree
from tempfile import TemporaryDirectory
from typing import Iterable
from urllib.parse import ParseResult, urljoin, urlparse, urlunparse

from debian.changelog import Changelog, format_date
from debian.deb822 import Deb822, _PkgRelationMixin
import jinja2

from distro_releases import (
    Release,
)
from distropack_builder import targets
from distropack_builder.bake import PROTOCOL, REGISTRY, tool_image
from distropack_builder.config import BuildConfig, Distrib
from distropack_builder.context import BuildContext
from distropack_builder.docker import Mount
from distropack_builder.downloads import (
    DownloadFailed,
    fetch_all,
    orig_name,
    source_name,
)
from distropack_builder.graph import closure, dependencies
from distropack_builder.publication import published
from distropack_builder.report import Reporter, build_logger
from distropack_builder import readme, repodata, rpmquery
from distropack_builder.rpmquery import SpecFailed, SpecInfo
from distropack_builder.scheduler import Succeeded, schedule
from distropack_builder.signing import Signer
from distropack_builder.versions import deb_version


def builder_image(dist_release: Release) -> str:
    """The builder image of a release, relative to REGISTRY, as the bake definition tags it.

    The tag is the release's codename in the Debian family, its version elsewhere.
    """
    if targets.is_deb(dist_release.distribution):
        return f'{dist_release.distribution.id}_builder:{dist_release.codename}-p{PROTOCOL}'
    return f'{dist_release.distribution.id}_builder:{dist_release.version}-p{PROTOCOL}'


def urlnormalize(url):
    su = urlparse(url)
    su = ParseResult(su.scheme, su.netloc, str(Path(su.path)), su.params, su.query, su.fragment)
    return urlunparse(su)


def source_format(debian_dir: Path) -> str:
    """The format of a deb source package, as debian/source/format says, 1.0 without it."""
    try:
        return (debian_dir / 'source' / 'format').read_text().strip()
    except FileNotFoundError:
        return '1.0'


def link_or_copy(source: Path, target: Path):
    """target, a hard link to source where the file system allows it, a copy elsewhere."""
    try:
        link(source, target)
    except OSError:
        copy2(source, target)


class DockerBindMount:
    def __init__(self, target: Path | str, source: Path | str, ro: bool = False):
        self.source = Path(source)
        self.target = Path(target)
        self.ro = ro


class DockerContainer:
    def __init__(self, context: BuildContext, img_name, logger: logging.Logger | None = None):
        self._context = context
        self._img_name = REGISTRY + img_name
        self._logger = logger

    def __call__(
        self,
        *args,
        mounts: Iterable[DockerBindMount] = [],
        shm_size: str | None = None,
        workdir: Path | str | None = None,
        return_stdout: bool = False,
        as_root: bool = False,
        env: Iterable[tuple[str, any]] = [],
    ):
        result = self._context.run(
            self._context.engine,
            self._img_name,
            [str(arg) for arg in args],
            mounts=[Mount(str(m.target), m.source, m.ro) for m in mounts],
            env={str(name): str(value) for name, value in env},
            workdir=None if workdir is None else str(workdir),
            shm_size=shm_size,
            as_root=as_root,
            cancellation=self._context.cancellation,
            logger=self._logger,
        )
        return result.stdout if return_stdout else None


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


class Aptly(DockerContainer):
    """aptly, alone in its image: it publishes unsigned, the Signer signs."""

    def __init__(self, context: BuildContext, logger: logging.Logger | None = None):
        super().__init__(context, tool_image('aptly'), logger)


class RpmSign(DockerContainer):
    """rpmsign and gpg, alone in their image: rpm has no other way to sign a package."""

    def __init__(self, context: BuildContext, logger: logging.Logger | None = None):
        super().__init__(context, tool_image('rpm_sign'), logger)

    def sign(self, signer: Signer, directory: Path, names: list[str]):
        """Sign the rpms names of directory, with the key of signer, given to gpg locked."""
        with signer.gpg_secret(self._context.workdir) as secret:
            self(
                'sign',
                signer.fingerprint,
                *names,
                mounts=[
                    DockerBindMount('/data', directory),
                    DockerBindMount('/secret', secret, ro=True),
                ],
            )


class Control(Deb822, _PkgRelationMixin):
    _relationship_fields = ['build-depends', 'build-depends-indep', 'build-depends-arch']

    def __init__(self, *args, **kwargs):
        Deb822.__init__(self, *args, **kwargs)
        _PkgRelationMixin.__init__(self, *args, **kwargs)


class PackageBuilder:
    """The build of one package for one release, the base of the builders of each family."""

    # The suffix of the build's work directory.
    family = None

    def __init__(
        self, context: BuildContext, dist_release: Release, base_dir: Path, gpg: Signer, name: str
    ):
        self._context = context
        self.dist_release = dist_release
        self._base_dir = base_dir
        self._gpg = gpg
        self.name = name
        self.logger = build_logger(name, dist_release)
        self.outdir = None
        self.bin_provides = set()
        self.bin_depends = set()

    def __str__(self):
        """The package's name, as the report names the build a skipped one waited for."""
        return self.name

    @classmethod
    def applies(cls, base_dir: Path, name: str) -> bool:
        """Whether the package has packaging for this family."""
        raise NotImplementedError

    def sources(self) -> list[str]:
        """The URLs of the files the build needs downloaded."""
        return []

    def build(
        self, deps: AbstractSet['PackageBuilder'], files: Mapping[str, Path | DownloadFailed]
    ):
        """Build, with the outputs of deps in the local repository, and files downloaded."""
        for url in self.sources():
            if isinstance(files[url], DownloadFailed):
                raise files[url]
        wk_dir = self._context.workdir / f'{self.name}.{self.dist_release.uid}.{self.family}-build'
        wk_dir.mkdir()
        self.outdir = wk_dir / 'output'
        self.outdir.mkdir()
        self._build(wk_dir, set(deps) | {self}, files)

    def _build(self, wk_dir: Path, deps, files):
        raise NotImplementedError

    def _local_repository(self, family, localrepo: Path, deps):
        """The repository of the outputs of deps, at localrepo, for the builder container."""
        failures = family(
            self._context, localrepo, self._gpg, logger=self.logger, sign=False
        ).build(deps)
        for error in failures.values():
            raise error


class DebPackageBuilder(PackageBuilder):
    family = 'deb'

    def __init__(
        self,
        context: BuildContext,
        dist_release: Release,
        base_dir: Path,
        gpg: Signer,
        name: str,
        orig_url: str | None = None,
    ):
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
                om['name'] for dl in deps_kind for am in control.relations[dl] for om in am
            }

    @classmethod
    def applies(cls, base_dir: Path, name: str) -> bool:
        return (base_dir / name / 'debian').is_dir()

    def _orig(self):
        """The URL of the upstream tarball, for a 3.0 (quilt) package that names one."""
        if self._format != '3.0 (quilt)' or self._orig_url is None:
            return None
        return self._orig_url.format(
            upstream_version=self._changelog.upstream_version, package=self._changelog.package
        )

    def sources(self) -> list[str]:
        orig = self._orig()
        return [] if orig is None else [orig]

    def _build(self, wk_dir: Path, deps, files):
        pkg_dir = wk_dir / 'pkg'
        pkg_dir.mkdir()

        # Copy the data
        fmt = self._format
        # a fresh copy of the changelog: each release adds its own entry
        with (self._debian_dir / 'changelog').open() as raw_changelog:
            changelog = Changelog(raw_changelog)
        if fmt == '3.0 (quilt)':
            if self._orig_url is None:
                raise Exception('a 3.0 (quilt) package needs an orig: URL in build.conf')
            pkg_src_dir = pkg_dir / (changelog.package + '-' + changelog.upstream_version)
            debian_dir = Path(copytree(self._debian_dir, pkg_src_dir / 'debian'))
        elif fmt == '3.0 (native)':
            pkg_src_dir = Path(
                copytree(
                    self._debian_dir.parent,
                    pkg_dir / (changelog.package + '-' + changelog.upstream_version),
                )
            )
            debian_dir = pkg_src_dir / 'debian'
        else:
            raise Exception('Unsupported deb package format: ' + fmt)

        # Generate the automated entry in changelog
        version = deb_version(
            str(changelog.version), changelog.date, self.dist_release, datetime.now(UTC)
        )
        changelog.new_block(
            package=changelog.package,
            version=version,
            distributions=self.dist_release.codename,
            urgency=changelog.urgency,
            changes=['  * Rebuild for ' + str(self.dist_release)],
            author=self._gpg.uid,
            date=format_date(),
        )
        with (debian_dir / 'changelog').open('w') as raw_changelog:
            changelog.write_to_open_file(raw_changelog)

        # Put the downloaded orig file in place
        orig = self._orig()
        if orig is not None:
            # the upstream version of the entry packaged, not of the rebuild's new one
            upstream = self._changelog.upstream_version
            name = orig_name(changelog.package, upstream, orig, files[orig])
            link_or_copy(files[orig], pkg_dir / name)

        # Build a repo with all dependencies
        localrepo = wk_dir / 'repo'
        self._local_repository(DebRepoBuilder, localrepo, deps)

        # Build the package in docker
        DockerContainer(self._context, builder_image(self.dist_release), self.logger)(
            '-j' + str(self._context.jobs),
            '-sa',
            '-P' + self.dist_release.distribution.id + ',' + self.dist_release.codename,
            mounts=[
                DockerBindMount('/src', pkg_dir),
                DockerBindMount('/localrepo', localrepo),
            ],
            shm_size='5g',
            as_root=True,
            env=[
                ('DH_VERBOSE', 1),
                (
                    'DISTROPACK_BUILD_PROFILES',
                    self.dist_release.distribution.id + ',' + self.dist_release.codename,
                ),
            ],
        )

        rmtree(localrepo)

        # Sign the source package: nothing reads the .changes, aptly adds the .dsc as it is
        for dsc in pkg_dir.glob('*.dsc'):
            dsc.write_bytes(self._gpg.clearsign(dsc.read_bytes()))

        # Moving the generated packages to the output directory
        for file in pkg_dir.iterdir():
            if file.is_file():
                file.rename(self.outdir / file.name)
        rmtree(pkg_dir)


class DebRepoBuilder:
    def __init__(
        self,
        context: BuildContext,
        output_dir: Path,
        gpg: Signer,
        url: str = None,
        name: str = None,
        description: str = None,
        pages: jinja2.Environment | None = None,
        logger: logging.Logger | None = None,
        sign: bool = True,
    ):
        self._context = context
        self._output_dir = output_dir
        self._gpg = gpg
        self._url = url
        if url is not None:
            self._url = urlnormalize(self._url)
            self._name = name if name is not None else Path(str(urlparse(self._url).path)).name
            self._description = description if description is not None else self._name
        self._pages = pages
        self._aptly = Aptly(context, logger)
        self._sign = sign

    def has_repository(self, dist_release: Release) -> bool:
        """Whether the output directory has a repository of dist_release already."""
        return (self._output_dir / 'dists' / dist_release.codename / 'Release').is_file()

    def build(self, pkgs: Iterable[DebPackageBuilder], kept: AbstractSet[Release] = frozenset()):
        """Publish the releases of pkgs, each on its own; the releases whose publication failed.

        The README lists the releases published, and those of kept (left as they were) whose
        repository is still there. Without a release to publish, nothing is touched.
        """
        pkgs = [
            p
            for p in pkgs
            if targets.is_deb(p.dist_release.distribution)
            and p.outdir is not None
            and p.outdir.is_dir()
        ]
        if len(pkgs) == 0:
            return {}

        published = []
        failures = {}
        with TemporaryDirectory(prefix='PKGBAPTLY.', dir=self._context.workdir) as tmp_dir:
            tmp_dir = Path(tmp_dir)

            with (tmp_dir / 'aptly.conf').open('w') as aptly_conf:
                json.dump(APTLY_CONFIG, aptly_conf)

            self._output_dir.mkdir(parents=True, exist_ok=True)

            def aptly(*args, data=None):
                self._aptly(
                    'aptly',
                    '-config=/aptlyroot/aptly.conf',
                    *args,
                    shm_size='5g',
                    mounts=[
                        DockerBindMount('/aptlyroot', tmp_dir),
                        DockerBindMount('/aptlyout', self._output_dir),
                    ]
                    + ([DockerBindMount('/data', data)] if data is not None else []),
                )

            for dist_release in sorted({p.dist_release for p in pkgs}, key=str):
                try:
                    self._publish(
                        aptly, dist_release, [p for p in pkgs if p.dist_release == dist_release]
                    )
                    published.append(dist_release)
                except Exception as error:
                    failures[dist_release] = error

        if not published:
            return failures
        if self._url is not None:
            keyring = self._output_dir / (self._name + '-archive-keyring.gpg')
            keyring.write_bytes(self._gpg.public_key(armored=False))
            listed = set(published) | {
                r for r in set(kept) | failures.keys() if self.has_repository(r)
            }
            if self._pages is not None:
                page = readme.Page.of('deb', self._url, self._name, self._description, listed)
                (self._output_dir / 'index.html').write_text(readme.render(self._pages, page))
        return failures

    def _publish(self, aptly, dist_release: Release, pkgs):
        aptly('repo', 'create', dist_release.uid)
        for pkg in pkgs:
            aptly('repo', 'add', dist_release.uid, '/data', data=pkg.outdir)
        publish_repo = ['publish', 'repo', '-force-overwrite', '-skip-signing']
        if self._url is not None:
            publish_repo += ['-label=' + self._description, '-origin=' + self._name]

        # like the Debian archive, publish the release under its codename and under its
        # suite, each Release file naming both so that apt accepts either in sources.list
        names = ['-codename=' + dist_release.codename]
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

    def _sign_release(self, release: Path):
        """Release.gpg and InRelease beside release, whose trailing spaces go first.

        A clear signature drops them (RFC 9580), so that InRelease's text would otherwise differ
        from Release: apt reads either.
        """
        text = b'\n'.join(line.rstrip(b' \t') for line in release.read_bytes().split(b'\n'))
        release.write_bytes(text)
        (release.parent / 'Release.gpg').write_bytes(self._gpg.detached(text))
        (release.parent / 'InRelease').write_bytes(self._gpg.clearsign(text))


class RpmPackageBuilder(PackageBuilder):
    family = 'rpm'

    def __init__(
        self,
        context: BuildContext,
        dist_release: Release,
        base_dir: Path,
        gpg: Signer,
        name: str,
        orig_url: str | None = None,
        *,
        spec: SpecInfo | SpecFailed,
    ):
        # orig: is a deb setting: the spec's Source: lines name the rpm's sources
        super().__init__(context, dist_release, base_dir, gpg, name)
        self._spec = self.spec_file(base_dir, name)
        self._spec_info = spec
        self._sources = []
        if isinstance(spec, SpecInfo):
            self.bin_depends = set(spec.buildrequires)
            self.bin_provides = set(spec.provides)
            self._sources = list(spec.sources)

    @staticmethod
    def spec_file(base_dir: Path, name: str) -> Path:
        return base_dir / name / (name + '.spec')

    @classmethod
    def applies(cls, base_dir: Path, name: str) -> bool:
        return (base_dir / name / (name + '.spec')).is_file()

    def sources(self) -> list[str]:
        return list(self._sources)

    def _build(self, wk_dir: Path, deps, files):
        if isinstance(self._spec_info, SpecFailed):
            raise Exception(self._spec_info.message)
        pkg_dir = wk_dir / 'pkg'
        pkg_dir.mkdir()
        spec = pkg_dir / self._spec.name
        copy2(self._spec, spec)

        localrepo = wk_dir / 'repo'
        self._local_repository(RpmRepoBuilder, localrepo, deps)

        for url in self._sources:
            link_or_copy(files[url], pkg_dir / source_name(url))

        DockerContainer(self._context, builder_image(self.dist_release), self.logger)(
            mounts=[
                DockerBindMount('/src', pkg_dir, ro=True),
                DockerBindMount('/output', self.outdir),
                DockerBindMount('/localrepo', localrepo / self.dist_release.version, ro=True),
            ],
            shm_size='5g',
            as_root=True,
        )
        rmtree(localrepo)

        # Sign the generated packages
        rpms = sorted(file.name for file in self.outdir.iterdir() if file.suffix == '.rpm')
        if rpms:
            RpmSign(self._context, self.logger).sign(self._gpg, self.outdir, rpms)
        rmtree(pkg_dir)


class RpmRepoBuilder:
    def __init__(
        self,
        context: BuildContext,
        output_dir: Path,
        gpg: Signer,
        url: str = None,
        name: str = None,
        description: str = None,
        pages: jinja2.Environment | None = None,
        logger: logging.Logger | None = None,
        sign: bool = True,
    ):
        self._context = context
        self._output_dir = output_dir
        self._gpg = gpg
        self._url = url
        self._sign = sign
        if url is not None:
            # a trailing slash, so that urljoin appends to the URL rather than replace its end
            self._url = urlnormalize(self._url) + '/'
            self._name = name if name is not None else str(Path(urlparse(str(self._url)).path).name)
            self._description = description if description is not None else self._name
        self._pages = pages

    def has_repository(self, dist_release: Release) -> bool:
        """Whether the output directory has a repository of dist_release already."""
        return (self._output_dir / dist_release.version / 'repodata' / 'repomd.xml').is_file()

    def build(self, pkgs: Iterable[RpmPackageBuilder], kept: AbstractSet[Release] = frozenset()):
        """Publish the releases of pkgs, each on its own; the releases whose publication failed.

        The README lists the releases published, and those of kept (left as they were) whose
        repository is still there. Without a release to publish, nothing is touched.
        """
        pkgs = [
            p
            for p in pkgs
            if targets.is_rpm(p.dist_release.distribution)
            and p.outdir is not None
            and p.outdir.is_dir()
        ]
        if len(pkgs) == 0:
            return {}

        releases_set = set()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        for pkg in pkgs:
            release_dir = self._output_dir / pkg.dist_release.version
            release_dir.mkdir(parents=True, exist_ok=True)
            for file in pkg.outdir.iterdir():
                if file.suffix == '.rpm' and len(file.suffixes) >= 2:
                    releases_set.add(pkg.dist_release)
                    rpmdir = release_dir / file.suffixes[-2][1:]
                    rpmdir.mkdir(exist_ok=True)
                    link_or_copy(file, rpmdir / file.name)

        published = []
        failures = {}
        for dist_release in sorted(releases_set, key=str):
            try:
                self._publish(dist_release)
                published.append(dist_release)
            except Exception as error:
                failures[dist_release] = error
        if not published:
            return failures

        if self._url is not None:
            (self._output_dir / (self._name + '.key')).write_bytes(
                self._gpg.public_key(armored=True)
            )
            listed = set(published) | {
                r for r in set(kept) | failures.keys() if self.has_repository(r)
            }
            if self._pages is not None:
                page = readme.Page.of('rpm', self._url, self._name, self._description, listed)
                (self._output_dir / 'index.html').write_text(readme.render(self._pages, page))
        return failures

    def _publish(self, dist_release: Release):
        release_dir = self._output_dir / dist_release.version
        repodata.write(release_dir)
        if self._sign:
            repomd = release_dir / 'repodata' / 'repomd.xml'
            repomd.with_name('repomd.xml.asc').write_bytes(self._gpg.detached(repomd.read_bytes()))
        if self._url is not None:
            repo_data = ConfigParser()
            repo_data.add_section(self._name)
            repo_data.set(self._name, 'name', self._description + ' (' + str(dist_release) + ')')
            repo_data.set(self._name, 'type', 'rpm-md')
            repo_data.set(self._name, 'baseurl', urljoin(self._url, dist_release.version))
            repo_data.set(self._name, 'gpgcheck', '1')
            repo_data.set(self._name, 'repo_gpgcheck', '1')
            repo_data.set(self._name, 'gpgkey', urljoin(self._url, self._name + '.key'))
            repo_data.set(self._name, 'enabled', '1')
            with (release_dir / (self._name + '.repo')).open('w') as repo_file:
                repo_data.write(repo_file, space_around_delimiters=False)


def run_builds(
    context: BuildContext,
    reporter: Reporter,
    config: BuildConfig,
    selected: Mapping[str, AbstractSet[Release]],
    disabled: Mapping[str, AbstractSet[Release]],
    signer_for: Callable[[Distrib], Signer],
) -> bool:
    """Download, build and publish; whether any build did not succeed."""
    builders = []
    repo_builders = []
    root = config.root
    pages = readme.environment(readme.project_dir(root))
    for dist_id, distrib in config.distribs.items():
        gpg = signer_for(distrib)

        dist_builders = []
        family = DebPackageBuilder if targets.is_deb(targets.target(dist_id)) else RpmPackageBuilder
        for dist_release in sorted(selected[dist_id], key=lambda release: release.uid):
            names = [
                name
                for name in config.packages
                if dist_release not in disabled[name] and family.applies(root, name)
            ]
            specs = {}
            if family is RpmPackageBuilder:
                # every spec of the release, read by the release's own rpm, in one container
                specs = rpmquery.query(
                    context,
                    REGISTRY + builder_image(dist_release),
                    {name: RpmPackageBuilder.spec_file(root, name) for name in names},
                )
            for name in names:
                extra = {'spec': specs[name]} if name in specs else {}
                dist_builders.append(
                    family(
                        context,
                        dist_release,
                        root,
                        gpg,
                        name,
                        config.packages[name].orig,
                        **extra,
                    )
                )

        repository = distrib.repository
        repo_family = DebRepoBuilder if family is DebPackageBuilder else RpmRepoBuilder
        repo_builder = repo_family(
            context,
            root / repository.path,
            gpg,
            repository.url,
            repository.name,
            repository.description,
            pages,
        )
        repo_builders.append(((dist_id, repo_builder), dist_builders))
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
    for (dist_id, repo_builder), dist_builders in repo_builders:
        kept = {b.dist_release for b in dist_builders} - releases
        try:
            failures = repo_builder.build(
                [b for b in dist_builders if b.dist_release in releases], kept
            )
        except Exception as error:
            failures = {dist_id: error}
        for what, error in failures.items():
            reporter.publication_failed(str(what), error)
            failed = True
    return failed
