import argparse
from collections.abc import Callable, Mapping, Set as AbstractSet
from configparser import ConfigParser
from contextlib import ExitStack
from datetime import UTC, date, datetime
import importlib.resources
import json
import logging
import os
from os import cpu_count, link
from pathlib import Path
import re
from shutil import copy2, copytree, rmtree
import signal
from subprocess import CalledProcessError
import sys
from sys import stderr
from string import Template
from tempfile import mkdtemp, TemporaryDirectory
from typing import Iterable, Set
from urllib.parse import ParseResult, urljoin, urlparse, urlunparse

from debian.changelog import Changelog, format_date
from debian.deb822 import Deb822, _PkgRelationMixin
import docker.errors
from rich.console import Console
import yaml

from distro_releases import (
    Release,
    SelectorError,
    SourceError,
    UnknownDistribution,
    matches,
    select,
)
from distropack_builder import targets
from distropack_builder.bake import REGISTRY
from distropack_builder.context import BuildContext, Runner
from distropack_builder.docker import Cancellation, Engine, Mount
from distropack_builder.docker import run as run_container
from distropack_builder.docker.endpoint import EngineNotFound, RemoteEndpoint
from distropack_builder.downloads import DownloadFailed, fetch_all, orig_name, source_name
from distropack_builder.graph import closure, dependencies
from distropack_builder.publication import published
from distropack_builder.report import Reporter, build_logger, github_mode
from distropack_builder.scheduler import Succeeded, schedule
from distropack_builder.versions import deb_version


def builder_image(dist_release: Release) -> str:
    """The builder image of a release, relative to REGISTRY, as the bake definition tags it.

    The tag is the release's codename in the Debian family, its version elsewhere.
    """
    if targets.is_deb(dist_release.distribution):
        return f'{dist_release.distribution.id}_builder:{dist_release.codename}'
    return f'{dist_release.distribution.id}_builder:{dist_release.version}'


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


class GpgInfo(DockerContainer):
    def __init__(self, context: BuildContext, keyfile, keyid=None, passphrase=None, uid=None):
        super().__init__(context, 'gpg2')
        self._keyid = keyid
        self._passphrase = passphrase
        self._home_dir = Path(mkdtemp(prefix='GNUPG_HOME.', dir=context.workdir))
        self._gpg_bin = self._home_dir / 'bin' / 'gpg'
        self._uid = uid
        self._mount_gnupghome = DockerBindMount('/gnupghome', self._home_dir)

        if self._keyid is not None:
            self._keyid = self._keyid.upper()

        # Build our config
        self._home_dir.chmod(0o700)
        with (self._home_dir / 'passphrase').open('w') as passphrase:
            print(self._passphrase, end='', file=passphrase)
        with (self._home_dir / 'S.gpg-agent').open('w') as gpgagent_socket:
            print('%Assuan%\nsocket=/tmp/S.gpg-agent', file=gpgagent_socket)
        with (self._home_dir / 'S.gpg-agent.ssh').open('w') as gpgagent_socketssh:
            print('%Assuan%\nsocket=/tmp/S.gpg-agent.ssh', file=gpgagent_socketssh)
        with (self._home_dir / 'gpg-agent.conf').open('w') as gpgagent_conf:
            print('extra-socket /tmp/S.gpg-agent.extra', file=gpgagent_conf)
            print('browser-socket /tmp/S.gpg-agent.browser', file=gpgagent_conf)
        with (self._home_dir / 'gpg.conf').open('w') as gpg_conf:
            print('batch', file=gpg_conf)
            print('always-trust', file=gpg_conf)
            print('pinentry-mode loopback', file=gpg_conf)
            print('passphrase-file /gnupghome/passphrase', file=gpg_conf)

        # Import the provided key
        self(
            '--passphrase-file',
            '/gnupghome/passphrase',
            '--import',
            str(Path(keyfile).name),
            mounts=[DockerBindMount('/data', Path(keyfile).parent)],
        )

        # Parse the KEYID & UID from the imported key
        data = self(
            '--with-colons',
            '--with-fingerprint',
            '--fixed-list-mode',
            '--list-secret-keys',
            return_stdout=True,
        )
        in_key = False
        for line in data.splitlines():
            res = line.split(':')
            if res[0] == 'sec':
                in_key = False
                if self._keyid is None or res[4].upper()[-8:] == self._keyid[-8:]:
                    in_key = True
            elif (
                res[0] == 'ssb'
                or res[0] == 'pub'
                or res[0] == 'crt'
                or res[0] == 'crs'
                or res[0] == 'sub'
            ):
                in_key = False
            elif res[0] == 'fpr' and in_key:
                self._keyid = res[9].upper()
            elif res[0] == 'uid' and in_key:
                if self._uid is None or res[9].strip() == self._uid.strip():
                    self._uid = res[9]

        # Mark the key as trusted
        with (self._home_dir / 'gpg.conf').open('a') as gpg_conf:
            print('trusted-key ' + self._keyid, file=gpg_conf)

        # Remove the passphrase from the key
        self('--passphrase-file', '/gnupghome/passphrase', '--passwd', self._keyid)

        # Export the key to a gpg1-compatible keyring
        self(
            '--passphrase-file',
            '/gnupghome/passphrase',
            '--export',
            '--output',
            '/gnupghome/keyring.pgp',
        )

    def __call__(self, *args, mounts: Iterable[DockerBindMount] = [], **kwargs):
        return super().__call__(*args, mounts=[self._mount_gnupghome] + mounts, **kwargs)

    def uid(self):
        """The UID of the key"""
        return self._uid

    def keyid(self):
        """The full key identifier"""
        return self._keyid

    def keyid8(self):
        """The 8-char key identifier"""
        return self._keyid[-8:]

    def gnupghome(self):
        """The directory containing the GPG config"""
        return self._home_dir


class DebTools(DockerContainer):
    def __init__(self, context: BuildContext, gpg: GpgInfo, logger: logging.Logger | None = None):
        super().__init__(context, 'deb_tools', logger)
        self._gpg = gpg

    def __call__(
        self, *args, gpgmount: bool = True, mounts: Iterable[DockerBindMount] = [], **kwargs
    ):
        return super().__call__(
            *args,
            mounts=([DockerBindMount('/gnupghome', self._gpg.gnupghome())] if gpgmount else [])
            + mounts,
            **kwargs,
        )

    def debsign(self, pkg_src_dir: Path, key_id: str):
        return self(
            'debsign',
            '--no-conf',
            '-k',
            key_id,
            workdir=Path('/data') / pkg_src_dir.name,
            mounts=[DockerBindMount('/data', pkg_src_dir.parent)],
        )

    def mk_build_deps(
        self, control_file: Path | str, outdir: Path | str, build_profiles: Iterable[str]
    ):
        build_profiles_params = []
        if build_profiles is not None and len(build_profiles) > 0:
            build_profiles_params = ['--build-profiles', ','.join(build_profiles)]
        return self(
            'mk-build-deps',
            *build_profiles_params,
            str(Path('/input') / Path(control_file).name),
            gpgmount=False,
            mounts=[
                DockerBindMount('/input', Path(control_file).parent),
                DockerBindMount('/data', outdir),
            ],
        )


class RpmTools(DockerContainer):
    def __init__(self, context: BuildContext, gpg: GpgInfo, logger: logging.Logger | None = None):
        super().__init__(context, 'rpm_tools', logger)
        self._gpg = gpg

    def __call__(
        self, *args, gpgmount: bool = True, mounts: Iterable[DockerBindMount] = [], **kwargs
    ):
        return super().__call__(
            *args,
            mounts=([DockerBindMount('/gnupghome', self._gpg.gnupghome())] if gpgmount else [])
            + mounts,
            **kwargs,
        )

    def createrepo(self, directory):
        self('createrepo_c', '.', shm_size='5g', mounts=[DockerBindMount('/data', directory)])

    def rpmspec(self, *args):
        spec_file = Path(args[-1])
        args = args[:-1]
        return self(
            'rpmspec',
            *args,
            str(spec_file.name),
            mounts=[DockerBindMount('/data', spec_file.parent, ro=True)],
            gpgmount=False,
            return_stdout=True,
        )

    def rpmsign(
        self,
        *args,
    ):
        file = Path(args[-1])
        args = args[:-1]
        self('rpmsign', *args, str(file.name), mounts=[DockerBindMount('/data', file.parent)])


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
        self, context: BuildContext, dist_release: Release, base_dir: Path, gpg: GpgInfo, name: str
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
        failures = family(self._context, localrepo, self._gpg, logger=self.logger).build(deps)
        for error in failures.values():
            raise error


class DebPackageBuilder(PackageBuilder):
    family = 'deb'

    def __init__(
        self,
        context: BuildContext,
        dist_release: Release,
        base_dir: Path,
        gpg: GpgInfo,
        name: str,
        orig_url: str | None = None,
    ):
        super().__init__(context, dist_release, base_dir, gpg, name)
        self._debian_dir = base_dir / name / 'debian'
        self._orig_url = orig_url
        self._deb_tools = DebTools(context, gpg, self.logger)
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
            author=self._gpg.uid(),
            date=format_date(),
        )
        with (debian_dir / 'changelog').open('w') as raw_changelog:
            changelog.write_to_open_file(raw_changelog)

        # Put the downloaded orig file in place
        orig = self._orig()
        if orig is not None:
            name = orig_name(changelog.package, changelog.upstream_version, orig, files[orig])
            link_or_copy(files[orig], pkg_dir / name)

        # Generate the dependencies .deb
        deps_dir = wk_dir / 'deps'
        deps_dir.mkdir()
        self._deb_tools.mk_build_deps(
            debian_dir / 'control',
            deps_dir,
            [self.dist_release.distribution.id, self.dist_release.codename],
        )

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
                DockerBindMount('/deps', deps_dir),
                DockerBindMount('/localrepo', localrepo),
            ],
            shm_size='5g',
            as_root=True,
            env=[('DH_VERBOSE', 1)],
        )

        rmtree(localrepo)
        rmtree(deps_dir)

        # Signing the generated packages
        self._deb_tools.debsign(pkg_src_dir, self._gpg.keyid())

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
        gpg: GpgInfo,
        url: str = None,
        name: str = None,
        description: str = None,
        readme_tpl: Path = None,
        logger: logging.Logger | None = None,
    ):
        self._context = context
        self._output_dir = output_dir
        self._gpg = gpg
        self._url = url
        if url is not None:
            self._url = urlnormalize(self._url)
            self._name = name if name is not None else Path(str(urlparse(self._url).path)).name
            self._description = description if description is not None else self._name
        self._readme_tpl = readme_tpl
        self._deb_tools = DebTools(context, gpg, logger)

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
                json.dump(
                    {
                        'architectures': ['amd64', 'source'],
                        'rootDir': '/aptlyroot',
                        'FileSystemPublishEndpoints': {
                            'default': {
                                'rootDir': '/aptlyout',
                                'linkMethod': 'copy',
                            }
                        },
                    },
                    aptly_conf,
                )

            self._output_dir.mkdir(parents=True, exist_ok=True)

            def aptly(*args, data=None):
                self._deb_tools(
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
        listed = set(published) | {r for r in set(kept) | failures.keys() if self.has_repository(r)}
        distribs = set()
        install = ''
        for dist_release in sorted(listed, key=str):
            distribs.add(str(dist_release.distribution))
            if self._readme_tpl is not None:
                with open(
                    self._readme_tpl
                    / ('INSTALL.' + str(dist_release.distribution.id) + '.tpl.html')
                ) as template_file:
                    install += Template(template_file.read()).substitute(
                        dist_release=str(dist_release),
                        codename=str(dist_release.codename),
                        baseurl=str(self._url),
                    )

        if self._url is not None:
            (self._output_dir / (self._name + '-archive-keyring.gpg')).unlink(missing_ok=True)
            self._gpg(
                '--export',
                '--output',
                str(self._name + '-archive-keyring.gpg'),
                mounts=[DockerBindMount('/data', self._output_dir)],
            )
        if self._readme_tpl is not None:
            with open(self._output_dir / 'index.html', mode='w') as readme_file:
                with open(self._readme_tpl / 'README.tpl.html') as template_file:
                    readme_file.write(
                        Template(template_file.read()).substitute(
                            distribs=' '.join(list(distribs)), install=install
                        )
                    )
        return failures

    def _publish(self, aptly, dist_release: Release, pkgs):
        aptly('repo', 'create', dist_release.uid)
        for pkg in pkgs:
            aptly(
                'repo',
                'include',
                '-keyring=/gnupghome/keyring.pgp',
                '-no-remove-files',
                '-repo=' + dist_release.uid,
                '/data',
                data=pkg.outdir,
            )
        publish_repo = [
            'publish',
            'repo',
            '-batch',
            '-force-overwrite',
            '-gpg-key=' + self._gpg.keyid8(),
        ]
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


class RpmPackageBuilder(PackageBuilder):
    family = 'rpm'

    def __init__(
        self,
        context: BuildContext,
        dist_release: Release,
        base_dir: Path,
        gpg: GpgInfo,
        name: str,
        orig_url: str | None = None,
    ):
        # orig: is a deb setting: the spec's Source: lines name the rpm's sources
        super().__init__(context, dist_release, base_dir, gpg, name)
        self._spec = base_dir / name / (name + '.spec')
        self._rpm_tools = RpmTools(context, gpg, self.logger)

        self.bin_depends = {
            re.sub(r'[\( ].*', '', dep)
            for dep in self._rpm_tools.rpmspec('--buildrequires', '-q', self._spec).splitlines()
        }
        self.bin_provides = {
            re.sub(r'[\( ].*', '', dep)
            for dep in self._rpm_tools.rpmspec('--provides', '-q', self._spec).splitlines()
        }
        all_urls = self._rpm_tools.rpmspec('-P', self._spec).splitlines()
        all_urls = [
            url
            for url in all_urls
            if re.match(r'^\s*source[0-9]*\s*:', url, re.IGNORECASE) is not None
        ]
        self._sources = [
            re.sub(r'^\s*source[0-9]*\s*:\s*', '', url, count=1, flags=re.IGNORECASE)
            for url in all_urls
        ]

    @classmethod
    def applies(cls, base_dir: Path, name: str) -> bool:
        return (base_dir / name / (name + '.spec')).is_file()

    def sources(self) -> list[str]:
        return list(self._sources)

    def _build(self, wk_dir: Path, deps, files):
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
        for file in self.outdir.iterdir():
            if file.suffix == '.rpm':
                self._rpm_tools.rpmsign('--define=%_gpg_name ' + self._gpg.uid(), '--resign', file)
        rmtree(pkg_dir)


class RpmRepoBuilder:
    def __init__(
        self,
        context: BuildContext,
        output_dir: Path,
        gpg: GpgInfo,
        url: str = None,
        name: str = None,
        description: str = None,
        readme_tpl: Path = None,
        logger: logging.Logger | None = None,
    ):
        self._context = context
        self._output_dir = output_dir
        self._gpg = gpg
        self._url = url
        self._rpm_tools = RpmTools(context, gpg, logger)
        if url is not None:
            # a trailing slash, so that urljoin appends to the URL rather than replace its end
            self._url = urlnormalize(self._url) + '/'
            self._name = name if name is not None else str(Path(urlparse(str(self._url)).path).name)
            self._description = description if description is not None else self._name
        self._readme_tpl = readme_tpl

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
            (self._output_dir / (self._name + '.key')).unlink(missing_ok=True)
            self._gpg(
                '--export',
                '--armor',
                '--output',
                Path('/data') / (self._name + '.key'),
                mounts=[DockerBindMount('/data', self._output_dir)],
            )

        listed = set(published) | {r for r in set(kept) | failures.keys() if self.has_repository(r)}
        install = ''
        distribs = set()
        for dist_release in sorted(listed, key=str):
            distribs.add(str(dist_release.distribution))
            if self._url is not None and self._readme_tpl is not None:
                with open(
                    self._readme_tpl
                    / ('INSTALL.' + str(dist_release.distribution.id) + '.tpl.html')
                ) as template_file:
                    install += Template(template_file.read()).substitute(
                        dist_release=str(dist_release),
                        codename=str(dist_release.codename),
                        baseurl=str(urljoin(self._url, dist_release.version)),
                    )

        if self._readme_tpl is not None:
            with open(self._output_dir / 'README.html', mode='w') as readme_file:
                with open(self._readme_tpl / 'README.tpl.html') as template_file:
                    readme_file.write(
                        Template(template_file.read()).substitute(
                            distribs=' '.join(list(distribs)), install=install
                        )
                    )
        return failures

    def _publish(self, dist_release: Release):
        release_dir = self._output_dir / dist_release.version
        self._rpm_tools.createrepo(release_dir)
        (release_dir / 'repodata' / 'repomd.xml.asc').unlink(missing_ok=True)
        self._gpg('-b', '-a', 'repodata/repomd.xml', mounts=[DockerBindMount('/data', release_dir)])
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


def release_list(cfg: dict, on: date | None = None) -> Set[Release]:
    """The releases a {distribution id: release tokens} mapping names, as of on."""
    releases = set()
    for dist_id, tokens in cfg.items():
        if not isinstance(tokens, list):
            tokens = [tokens]
        releases |= select(targets.target(dist_id), [str(token) for token in tokens], on)
    return releases


def release_filter(releases: Iterable[Release], dist_filter: Iterable[str]) -> Set[Release]:
    """The releases one of the -D patterns names."""
    return {release for release in releases if any(matches(release, f) for f in dist_filter)}


def configured_releases(cfg: dict, patterns: Set[str], on: date) -> dict[str, Set[Release]]:
    """The releases build.conf names for each of its distributions, as of on.

    With -D patterns, a distribution none of them names gets no release, and its source is not
    read: a source being down only stops the distributions it serves. Every distribution, and every
    distribution a package's disable list names, must be a target.
    """
    for dist_id in cfg['distribs']:
        targets.target(dist_id)
    for pkg_cfg in cfg['packages'].values():
        if isinstance(pkg_cfg, dict):
            for dist_id in pkg_cfg.get('disable', {}):
                targets.target(dist_id)
    result = {}
    for dist_id, distrib_cfg in cfg['distribs'].items():
        distribution = targets.target(dist_id)
        named = {pattern.partition(':')[0] for pattern in patterns}
        if patterns and not named & {distribution.id, distribution.name}:
            result[dist_id] = set()
            continue
        releases = release_list({dist_id: distrib_cfg.get('versions', {})}, on)
        result[dist_id] = release_filter(releases, patterns) if patterns else releases
    return result


def disabled_releases(
    cfg: dict, selected: dict[str, Set[Release]], on: date
) -> dict[str, Set[Release]]:
    """For each package, the selected releases its disable list names, as of on.

    Only the distributions that have selected releases are read.
    """
    result = {}
    for name, pkg_cfg in cfg['packages'].items():
        disable = pkg_cfg.get('disable', {}) if isinstance(pkg_cfg, dict) else {}
        result[name] = release_list(
            {dist_id: tokens for dist_id, tokens in disable.items() if selected.get(dist_id)}, on
        )
    return result


def write_bake_file(output):
    """Write the bake definition of the installed images to output, or to stdout."""
    from distropack_builder import bake

    images_dir = Path(str(importlib.resources.files('distropack_builder') / 'images'))
    definition = json.dumps(bake.bake_definition(images_dir, bake.registry_has), indent=2) + '\n'
    if output is None:
        sys.stdout.write(definition)
    else:
        Path(output).write_text(definition)


def main(
    *, runner: Runner = run_container, connect: Callable[[str | None], Engine] = Engine.connect
):
    base_dir = Path.cwd().absolute()

    parser = argparse.ArgumentParser(
        prog='distropack-build',
        description='Builds deb & RPM repositories, and the Docker images that build them.',
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser(
        'run', description='Builds deb & RPM repositories.', allow_abbrev=False
    )
    images = commands.add_parser(
        'images', description='Works with the Docker images.', allow_abbrev=False
    )
    image_commands = images.add_subparsers(dest='images_command', required=True)
    bake_file = image_commands.add_parser(
        'bake-file',
        description='Writes the Docker bake definition of the images, leaving out the releases'
        ' whose base image the registry does not publish.',
        allow_abbrev=False,
    )
    bake_file.add_argument(
        '-o', '--output', metavar='FILE', help='Where to write the definition (default: stdout)'
    )
    run.add_argument(
        '-p', '--passphrase', action='store', help='The passphrase for the GPG release key'
    )
    run.add_argument('-v', '--verbose', action='store_true', help='Wether to run in verbose mode')
    run.add_argument(
        '-i', '--interactive', action='store_true', help='Wether to run in interactive mode'
    )
    run.add_argument(
        '--docker-host',
        default=os.environ.get('DISTROPACK_DOCKER_HOST'),
        help='The endpoint of the container engine (default: $DISTROPACK_DOCKER_HOST, '
        "$DOCKER_HOST, the current Docker context, Docker's socket, then Podman's)",
    )
    run.add_argument(
        '-D', '--distributions', action='append', nargs='+', help='The distributions to build'
    )
    run.add_argument(
        '-j',
        '--jobs',
        action='store',
        nargs='?',
        type=int,
        default=None,
        help='Number of jobs to run in parallel',
    )
    run.add_argument(
        'build_conf',
        action='store',
        metavar='build.conf',
        nargs='?',
        default='build.conf',
        help='Build configuration file',
    )
    args = parser.parse_args()

    if args.command == 'images':
        write_bake_file(args.output)
        return

    # SIGTERM (kill, timeout, a CI job cancelled) takes the path of Ctrl-C: the builds are
    # cancelled, their containers killed, nothing is published, and the work directory removed.
    previous = signal.signal(signal.SIGTERM, _interrupt)
    try:
        _run(args, parser, base_dir, runner, connect)
    finally:
        signal.signal(signal.SIGTERM, previous)


def _interrupt(signum, frame):
    raise KeyboardInterrupt()


def _run(args, parser, base_dir, runner, connect):
    if args.jobs is None and args.verbose:
        args.jobs = 1
    elif args.jobs is None:
        args.jobs = cpu_count() + 1

    # One date for the whole run, so that a run crossing midnight sees one list of releases.
    on = date.today()
    try:
        with (base_dir / args.build_conf).open('r') as cfg_file:
            cfg = yaml.safe_load(cfg_file)

        # The configuration's errors, and the release data, come before any work.
        patterns = {e for el in args.distributions or [] for e in el}
        selected = configured_releases(cfg, patterns, on)
        disabled = disabled_releases(cfg, selected, on)

        # Then the engine, before any container.
        try:
            engine = connect(args.docker_host)
        except RemoteEndpoint as error:
            parser.error(str(error))
        except (EngineNotFound, docker.errors.DockerException) as error:
            print(f'{parser.prog}: error: {error}', file=sys.stderr)
            sys.exit(1)

        with ExitStack() as stack:
            workdir = Path(stack.enter_context(TemporaryDirectory(prefix='distropack-build.')))
            context = BuildContext(engine, runner, Cancellation(), workdir, args.jobs)
            summary = os.environ.get('GITHUB_STEP_SUMMARY')
            reporter = stack.enter_context(
                Reporter(
                    Console(),
                    verbose=args.verbose,
                    github=github_mode(os.environ),
                    summary=Path(summary) if summary else None,
                )
            )
            failed = run_builds(context, reporter, cfg, base_dir, selected, disabled, args)
        if failed:
            sys.exit(1)

    except (targets.NotATarget, UnknownDistribution, SelectorError) as error:
        parser.error(str(error))
    except SourceError as error:
        print(f'{parser.prog}: error: {error}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        if args.interactive:
            input('Press Enter to clean-up and continue...')
        raise
    except CalledProcessError as err:
        print(
            '\033[31m[ERROR]\033[39m   * Error while running subprocess:\n$ '
            + ' '.join([str(ce) for ce in err.cmd]),
            file=stderr,
        )
        if err.stdout is not None:
            print(err.stdout)
        if err.stderr is not None:
            print(err.stderr, file=stderr)
        print(file=stderr)
        if args.interactive:
            input('Press Enter to clean-up and quit...')
        raise
    except Exception as err:
        print('\033[31m[ERROR]\033[39m   * ' + str(err))
        if args.interactive:
            input('Press Enter to clean-up and continue...')
        raise


def run_builds(context: BuildContext, reporter: Reporter, cfg, base_dir, selected, disabled, args):
    """Download, build and publish; whether any build did not succeed."""
    builders = []
    repo_builders = []
    for dist_id, distrib_cfg in cfg['distribs'].items():
        keyfile = base_dir / distrib_cfg.get('gpg', {}).get('file', 'key.gpg')
        keyid = distrib_cfg.get('gpg', {}).get('id', None)
        uid = distrib_cfg.get('gpg', {}).get('uid', None)
        gpg = GpgInfo(context, keyfile, keyid, args.passphrase, uid)

        dist_builders = []
        family = DebPackageBuilder if targets.is_deb(targets.target(dist_id)) else RpmPackageBuilder
        for dist_release in sorted(selected[dist_id], key=lambda release: release.uid):
            for name, pkg_cfg in cfg['packages'].items():
                if not isinstance(pkg_cfg, dict):
                    pkg_cfg = {'value': pkg_cfg}
                if dist_release in disabled[name] or not family.applies(base_dir, name):
                    continue
                dist_builders.append(
                    family(context, dist_release, base_dir, gpg, name, pkg_cfg.get('orig', None))
                )

        output_path = base_dir / distrib_cfg.get('repository', {}).get('path', 'repositories')
        url = distrib_cfg.get('repository', {}).get('url')
        name = distrib_cfg.get('repository', {}).get('name')
        description = distrib_cfg.get('repository', {}).get('description')
        repo_family = DebRepoBuilder if family is DebPackageBuilder else RpmRepoBuilder
        repo_builder = repo_family(
            context, output_path, gpg, url, name, description, base_dir / 'README.tpl'
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
