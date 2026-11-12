# SPDX-FileCopyrightText: 2020-2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The command line of distropack-build: run, images, config and releases.

Every expected error ends the command with one `distropack-build: error:` line on stderr: status 2
for what the command line or build.conf asks wrongly, 1 for what failed while doing it. An
unexpected exception is a bug, and ends with Python's traceback.
"""

import importlib.resources
import json
import os
import signal
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager, suppress
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import FrameType
from typing import Annotated, NoReturn

import docker.errors
import typer
from rich.console import Console

from distro_releases import Release, SelectorError, SourceError, UnknownDistribution
from distropack_builder import bake, config, readme, snapshots, targets
from distropack_builder import release as release_tool
from distropack_builder.availability import AvailabilityError, image_available
from distropack_builder.builders import run_builds, write_sources
from distropack_builder.config import AVAILABLE, BuildConfig, ConfigError, Distrib
from distropack_builder.context import BuildContext, Runner
from distropack_builder.docker import Cancellation, Engine
from distropack_builder.docker import run as run_container
from distropack_builder.docker.endpoint import EngineNotFound, RemoteEndpoint
from distropack_builder.downloads import new_client
from distropack_builder.github import GithubOutputError, write_outputs
from distropack_builder.inputs import builder_image, changed, fetch
from distropack_builder.report import Reporter, github_mode
from distropack_builder.rpmquery import SpecFailed
from distropack_builder.selection import configured_releases, disabled_releases
from distropack_builder.signing import Signer, SigningKeyError

PROG = 'distropack-build'
# The environment variable the passphrase of the signing key comes from, without a file.
PASSPHRASE_ENV = 'DISTROPACK_GPG_PASSPHRASE'
# The status of a run interrupted by Ctrl-C or SIGTERM, as a shell reports SIGINT.
INTERRUPTED = 130


class PassphraseError(Exception):
    """A passphrase file that cannot be read."""


# The errors in what the user asked, and those met while doing it.
_USAGE_ERRORS = (
    ConfigError,
    PassphraseError,
    RemoteEndpoint,
    SelectorError,
    readme.TemplateError,
    UnknownDistribution,
    targets.NotATarget,
)
_RUN_ERRORS = (AvailabilityError, GithubOutputError, SigningKeyError, SourceError)


def _fail(status: int, error: Exception) -> NoReturn:
    """End the command with status, and the error on one line."""
    typer.echo(f'{PROG}: error: {error}', err=True)
    raise typer.Exit(status) from error


@contextmanager
def _errors() -> Iterator[None]:
    """Turn the expected errors into one line and their status."""
    try:
        yield
    except _USAGE_ERRORS as error:
        _fail(2, error)
    except _RUN_ERRORS as error:
        _fail(1, error)


def read_passphrase(file: Path | None, environ: Mapping[str, str]) -> str | None:
    r"""The passphrase of the signing key: file's content, or else PASSPHRASE_ENV's value.

    The file's final line ending, `\n` or `\r\n`, is not part of the passphrase.

    Raises:
        PassphraseError: the file cannot be read, or is not UTF-8.
    """
    if file is None:
        return environ.get(PASSPHRASE_ENV)
    try:
        text = file.read_bytes().decode()
    except OSError as error:
        raise PassphraseError(f'cannot read {file}: {error.strerror}') from None
    except UnicodeDecodeError:
        raise PassphraseError(f'{file} is not UTF-8') from None
    return text.removesuffix('\r\n') if text.endswith('\r\n') else text.removesuffix('\n')


def _selection(
    build_conf: Path, patterns: list[str], on: date
) -> tuple[BuildConfig, dict[str, frozenset[Release]]]:
    """build.conf, and the releases it and the -D patterns select, as of on."""
    loaded = config.load(build_conf)
    with new_client() as client:
        selected = configured_releases(
            loaded,
            set(patterns),
            on,
            lambda release: image_available(bake.REGISTRY + builder_image(release), client),
        )
    return loaded, selected


def _unpublished(
    loaded: BuildConfig,
    selected: Mapping[str, frozenset[Release]],
    on: date,
    has_local: Callable[[str], bool],
) -> dict[Release, str]:
    """The selected releases whose builder image neither the engine has nor the registry publishes.

    The distributions whose versions hold `available` already kept only the published ones. An
    image the engine has (baked locally, or pulled) is not asked of the registry.

    Raises:
        AvailabilityError: the registry did not tell whether it publishes an image.
    """
    missing: dict[Release, str] = {}
    with new_client() as client:
        for dist_id, distrib in loaded.distribs.items():
            if AVAILABLE in distrib.versions:
                continue
            for release in sorted(selected[dist_id]):
                image = bake.REGISTRY + builder_image(release)
                if not has_local(image) and not image_available(image, client):
                    missing[release] = _why_unpublished(release, on)
    return missing


def _why_unpublished(release: Release, on: date) -> str:
    """Why the registry may not publish the builder image of release, as of on."""
    if not release.is_eol(on) or release.is_maintained(on):
        return 'not published yet'
    if release.extended_eol_date is not None and release.extended_eol_date > (
        release.eol_date or date.min
    ):
        return f'its extended support ended on {release.extended_eol_date}'
    return f'end of life since {release.eol_date}'


def _given(loaded: BuildConfig, channel: str, values: list[str]) -> dict[str, str]:
    """The commits --snapshot gives, by package, checked against channel; exits 2 on an error."""
    given: dict[str, str] = {}
    for value in values:
        package, separator, commit = value.partition('=')
        if not separator or not package or not commit:
            _fail(2, ValueError(f'--snapshot {value}: expected PACKAGE=COMMIT'))
        given[package] = commit
    try:
        snapshots.check_given(loaded, channel, given)
    except ValueError as error:
        _fail(2, error)
    return given


def _interrupt(signum: int, frame: FrameType | None) -> None:
    """Handle SIGTERM as Ctrl-C."""
    raise KeyboardInterrupt()


BuildConf = Annotated[
    Path, typer.Argument(metavar='BUILD_CONF', help='The build configuration.', show_default=True)
]
Patterns = Annotated[
    list[str] | None,
    typer.Option(
        '-D',
        '--distributions',
        metavar='PATTERN',
        help='Only the releases this pattern names: a distribution (debian, or its name), or one'
        ' of its releases (debian:trixie, fedora:44); repeat it for several.',
        show_default=False,
    ),
]
Output = Annotated[
    Path | None,
    typer.Option('-o', '--output', metavar='FILE', help='Where to write it (default: stdout).'),
]


def _write(text: str, output: Path | None) -> None:
    """Write text to output, or to stdout."""
    if output is None:
        typer.echo(text, nl=False)
    else:
        output.write_text(text)


def _compact(value: object) -> str:
    """The JSON of value, on one line, as a GitHub Actions output."""
    return json.dumps(value, separators=(',', ':'))


def _write_outputs_once_stopped(outputs: Mapping[str, str]) -> None:
    """Write outputs for a command already ending on an error, which says why it ended."""
    with suppress(GithubOutputError):
        write_outputs(os.environ, outputs)


def _release_name(release: Release) -> str:
    """A release as its image tag names it: its codename in the Debian family, else its version."""
    name = release.codename if targets.is_deb(release.distribution) else release.version
    return release.uid if name is None else name


def make_app(
    *, runner: Runner = run_container, connect: Callable[[str | None], Engine] = Engine.connect
) -> typer.Typer:
    """The command line, running its containers with runner on the engine connect gives."""
    app = typer.Typer(
        name=PROG,
        help='Builds deb and rpm packages and their signed repositories, in containers.',
        no_args_is_help=True,
        add_completion=False,
        pretty_exceptions_enable=False,
    )
    images = typer.Typer(help='Works with the Docker images.', no_args_is_help=True)
    config_app = typer.Typer(help='Works with build.conf.', no_args_is_help=True)
    releases = typer.Typer(help='Works with the releases to build for.', no_args_is_help=True)
    app.add_typer(images, name='images')
    app.add_typer(config_app, name='config')
    app.add_typer(releases, name='releases')

    def build(
        build_conf: Path,
        patterns: list[str],
        jobs: int,
        verbose: bool,
        passphrase: str | None,
        docker_host: str | None,
        download_dir: Path | None = None,
        check_reproducible: bool = False,
        state_dir: Path | None = None,
        publish: bool = True,
        build_packages: bool = True,
        channel: str | None = None,
        snapshot: list[str] | None = None,
    ) -> tuple[bool, dict[str, str]]:
        """Build and publish; whether anything failed, and the repositories by distribution id."""
        if not build_packages and not publish:
            _fail(2, ValueError('--publish-only and --no-publish exclude each other'))
        if not build_packages and state_dir is None:
            _fail(2, ValueError('--publish-only needs --state-dir'))
        # One date for the whole run, so that a run crossing midnight sees one list of releases.
        on = date.today()
        # The configuration's errors, the release data and the images available come before any
        # work; then the engine, before any container.
        loaded, selected = _selection(build_conf, patterns, on)
        if channel is not None and channel not in loaded.channels:
            _fail(2, ValueError(f'unknown channel {channel}'))
        given = _given(loaded, channel or loaded.release_channel, snapshot or [])
        # publishing from lasting states publishes every configured release, whatever -D selects
        all_releases = (
            _selection(build_conf, [], on)[1] if patterns and publish and state_dir else None
        )
        readme.check(readme.project_dir(loaded.root))
        # the repositories are removed before they are written again: never what a run keeps
        repositories = [
            loaded.root / loaded.channel_repository(dist_id, name).path
            for dist_id in loaded.distribs
            for name in loaded.channels
        ]
        for kept in (state_dir, download_dir):
            if kept is not None and any(
                kept.resolve().is_relative_to(repository.resolve()) for repository in repositories
            ):
                _fail(2, ValueError(f'{kept} must not be inside a repository directory'))
        # The repositories this run publishes, for the step running it under GitHub Actions.
        output_dirs = {
            dist_id: str(loaded.root / distrib.repository.path)
            for dist_id, distrib in loaded.distribs.items()
            if selected[dist_id]
        }
        # an rpm snapshot channel is a repository of its own, keyed <distribution id>.<channel>
        output_dirs |= {
            f'{dist_id}.{name}': str(loaded.root / loaded.channel_repository(dist_id, name).path)
            for dist_id in output_dirs.copy()
            if not targets.is_deb(targets.target(dist_id))
            for name in loaded.snapshot_channels()
            if loaded.channel_packages(name)
        }
        if not output_dirs:
            typer.echo(f'{PROG}: nothing to build: {build_conf} selects no release', err=True)
            return False, output_dirs
        try:
            engine = connect(docker_host)
        except (EngineNotFound, docker.errors.DockerException) as error:
            _fail(1, error)
        # a release whose builder image is missing fails once, here, and builds nothing
        has_local = engine.has_image if engine is not None else (lambda image: False)
        # publishing alone needs no builder image
        unpublished = _unpublished(loaded, selected, on, has_local) if build_packages else {}
        selected = {
            dist_id: frozenset(r for r in releases if r not in unpublished)
            for dist_id, releases in selected.items()
        }
        disabled = disabled_releases(loaded, selected, on)

        def signer_for(distrib: Distrib) -> Signer:
            return Signer.load(
                loaded.root / distrib.gpg.file,
                passphrase,
                keyid=distrib.gpg.id,
                uid=distrib.gpg.uid,
            )

        with ExitStack() as stack:
            workdir = Path(stack.enter_context(TemporaryDirectory(prefix='distropack-build.')))
            context = BuildContext(
                engine, runner, Cancellation(), workdir, jobs, check_reproducible
            )
            summary = os.environ.get('GITHUB_STEP_SUMMARY')
            reporter = stack.enter_context(
                Reporter(
                    Console(),
                    verbose=verbose,
                    github=github_mode(os.environ),
                    summary=Path(summary) if summary else None,
                )
            )
            for release, reason in unpublished.items():
                reporter.release_failed(release, f'no builder image for {release}: {reason}')
            failed = run_builds(
                context,
                reporter,
                loaded,
                selected,
                disabled,
                signer_for,
                set(unpublished),
                download_dir.resolve() if download_dir else None,
                state_dir.resolve() if state_dir else None,
                publish=publish,
                build=build_packages,
                all_releases=all_releases,
                channel=channel or loaded.release_channel,
                snapshots=given,
            )
        return failed or bool(unpublished), output_dirs

    @app.command()
    def run(
        build_conf: BuildConf = Path('build.conf'),
        distributions: Patterns = None,
        jobs: Annotated[
            int | None,
            typer.Option(
                '-j',
                '--jobs',
                min=1,
                help='How many builds run at once, and how many jobs each one runs (default: the'
                ' number of processors, plus one).',
                show_default=False,
            ),
        ] = None,
        verbose: Annotated[
            bool, typer.Option('-v', '--verbose', help="Show every build's output, as it comes.")
        ] = False,
        passphrase_file: Annotated[
            Path | None,
            typer.Option(
                '--passphrase-file',
                metavar='FILE',
                exists=True,
                dir_okay=False,
                help='The file holding the passphrase of the signing key (default:'
                f' ${PASSPHRASE_ENV}).',
            ),
        ] = None,
        docker_host: Annotated[
            str | None,
            typer.Option(
                '--docker-host',
                metavar='URL',
                envvar='DISTROPACK_DOCKER_HOST',
                help='The endpoint of the container engine (default: $DOCKER_HOST, the current'
                " Docker context, Docker's socket, then Podman's).",
            ),
        ] = None,
        check_reproducible: Annotated[
            bool,
            typer.Option(
                '--check-reproducible',
                help='Build every package twice, varying what must not matter (build path, host'
                ' name, umask, time zone, locale, parallelism, time), and fail the packages whose'
                " files differ, with diffoscope's report.",
            ),
        ] = False,
        download_dir: Annotated[
            Path | None,
            typer.Option(
                '--download-dir',
                metavar='DIR',
                file_okay=False,
                help='Where to keep the downloaded sources from run to run, instead of a'
                ' temporary directory: a source already there is not downloaded again.',
            ),
        ] = None,
        state_dir: Annotated[
            Path | None,
            typer.Option(
                '--state-dir',
                metavar='DIR',
                file_okay=False,
                help='Where each release keeps the packages of its last complete build, to reuse'
                ' them, or rebuild them binary-only, when nothing but their build environment'
                ' changed (default: a temporary directory, so that everything is built).',
            ),
        ] = None,
        no_publish: Annotated[
            bool,
            typer.Option(
                '--no-publish',
                help="Build, and keep the releases' states, but write no repository.",
            ),
        ] = False,
        publish_only: Annotated[
            bool,
            typer.Option(
                '--publish-only',
                help='Build nothing: write the repositories from the states of --state-dir, every'
                ' release of build.conf that has one.',
            ),
        ] = False,
        channel: Annotated[
            str | None,
            typer.Option(
                '--channel',
                metavar='NAME',
                help="The channel to build (default: build.conf's release channel).",
            ),
        ] = None,
        snapshot: Annotated[
            list[str] | None,
            typer.Option(
                '--snapshot',
                metavar='PACKAGE=COMMIT',
                help="Build this commit of the package, on its channel's ref, instead of the one"
                ' snapshots.json records or the head; repeat it for several.',
                show_default=False,
            ),
        ] = None,
    ) -> None:
        """Builds the packages of build.conf, and publishes their signed repositories."""
        # SIGTERM (kill, timeout, a CI job cancelled) takes the path of Ctrl-C: the builds are
        # cancelled, their containers killed, nothing is published, the work directory removed.
        previous = signal.signal(signal.SIGTERM, _interrupt)
        # Under GitHub Actions, a run that an error or an interruption stops is a failed one too.
        stopped = {'failed': 'true', 'output-dirs': '{}'}
        try:
            with _errors():
                failed, output_dirs = build(
                    build_conf,
                    distributions or [],
                    jobs or (os.cpu_count() or 1) + 1,
                    verbose,
                    read_passphrase(passphrase_file, os.environ),
                    docker_host,
                    download_dir,
                    check_reproducible,
                    state_dir,
                    not no_publish,
                    not publish_only,
                    channel,
                    snapshot,
                )
        except typer.Exit:
            _write_outputs_once_stopped(stopped)
            raise
        except KeyboardInterrupt:
            typer.echo(f'{PROG}: interrupted', err=True)
            _write_outputs_once_stopped(stopped)
            raise typer.Exit(INTERRUPTED) from None
        finally:
            signal.signal(signal.SIGTERM, previous)
        with _errors():
            write_outputs(
                os.environ, {'failed': str(failed).lower(), 'output-dirs': _compact(output_dirs)}
            )
        if failed:
            raise typer.Exit(1)

    @app.command('fetch')
    def fetch_command(
        download_dir: Annotated[
            Path,
            typer.Option(
                '--download-dir',
                metavar='DIR',
                file_okay=False,
                help='Where to write the downloads and snapshots.json, for run to use.',
            ),
        ],
        build_conf: BuildConf = Path('build.conf'),
        distributions: Patterns = None,
        channel: Annotated[
            str | None,
            typer.Option(
                '--channel',
                metavar='NAME',
                help="The channel to fetch for (default: build.conf's release channel).",
            ),
        ] = None,
        snapshot: Annotated[
            list[str] | None,
            typer.Option(
                '--snapshot',
                metavar='PACKAGE=COMMIT',
                help="Build this commit of the package, on its channel's ref, instead of the"
                ' head; repeat it for several.',
                show_default=False,
            ),
        ] = None,
        state_dir: Annotated[
            Path | None,
            typer.Option(
                '--state-dir',
                metavar='DIR',
                file_okay=False,
                help='The states to compare with: print changed=true when a run would build'
                ' anything they do not hold, changed=false otherwise.',
            ),
        ] = None,
        docker_host: Annotated[
            str | None,
            typer.Option(
                '--docker-host',
                metavar='URL',
                envvar='DISTROPACK_DOCKER_HOST',
                help='The endpoint of the container engine.',
            ),
        ] = None,
    ) -> None:
        """Resolves a channel's snapshots and downloads its sources, once for many runs.

        Every run given the same --download-dir builds the commits recorded in its
        snapshots.json, even when their branches moved meanwhile.
        """
        on = date.today()
        with _errors():
            loaded, selected = _selection(build_conf, distributions or [], on)
        name = channel or loaded.release_channel
        if name not in loaded.channels:
            _fail(2, ValueError(f'unknown channel {name}'))
        given = _given(loaded, name, snapshot or [])
        try:
            engine = connect(docker_host)
        except (EngineNotFound, docker.errors.DockerException) as error:
            _fail(1, error)
        has_local = engine.has_image if engine is not None else (lambda image: False)
        with _errors():
            unpublished = _unpublished(loaded, selected, on, has_local)
        for release, reason in unpublished.items():
            typer.echo(f'{PROG}: no builder image for {release}: {reason}', err=True)
        selected = {
            dist_id: frozenset(r for r in releases if r not in unpublished)
            for dist_id, releases in selected.items()
        }
        disabled = disabled_releases(loaded, selected, on)
        download_dir.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='distropack-build.') as workdir:
            jobs = (os.cpu_count() or 1) + 1
            context = BuildContext(engine, runner, Cancellation(), Path(workdir), jobs)
            found = fetch(
                context,
                loaded,
                name,
                selected,
                disabled,
                download_dir.resolve(),
                given,
                resolve=True,
            )
        failures = [
            *(str(f) for f in found.snapshots.values() if isinstance(f, Exception)),
            *(f'{n}: {f}' for n, f in found.origs.items() if isinstance(f, Exception)),
            *(
                f'{n} on {uid}: {f.message}'
                for (n, uid), f in found.specs.items()
                if isinstance(f, SpecFailed)
            ),
            *(str(f) for f in found.files.values() if isinstance(f, Exception)),
        ]
        if state_dir is not None:
            moved = changed(loaded, name, found, state_dir.resolve(), selected, disabled)
            typer.echo(f'changed={str(moved).lower()}')
            with _errors():
                write_outputs(os.environ, {'changed': str(moved).lower()})
        for failure in failures:
            typer.echo(f'{PROG}: {failure}', err=True)
        if failures:
            raise typer.Exit(1)

    @app.command('pending')
    def pending_command(
        build_conf: Annotated[
            Path, typer.Argument(metavar='BUILD_CONF', help='The build configuration.')
        ],
        channel: Annotated[str, typer.Argument(metavar='CHANNEL', help='The snapshot channel.')],
        packages: Annotated[
            list[str], typer.Argument(metavar='PACKAGE...', help='The packages whose changes wait.')
        ],
    ) -> None:
        """Copies the packaging of packages to <name>-<channel>/, pending the next release.

        The snapshot channel builds from that directory, where its changes wait for the release
        that needs them. Run it in a clean git working tree; it commits nothing.
        """
        with _errors():
            loaded = config.load(build_conf)
        try:
            release_tool.pending(loaded, channel, packages, datetime.now(UTC))
        except release_tool.ReleaseError as error:
            _fail(2, error)

    @app.command('release')
    def release_command(
        build_conf: Annotated[
            Path, typer.Argument(metavar='BUILD_CONF', help='The build configuration.')
        ],
        version: Annotated[
            str, typer.Argument(metavar='VERSION', help='The upstream release tag (1.3.0-rc1).')
        ],
        from_channel: Annotated[
            str | None,
            typer.Option(
                '--from',
                metavar='CHANNEL',
                help='The channel whose pending changes the release takes (default: the only'
                ' one that has some).',
            ),
        ] = None,
        ported: Annotated[
            bool,
            typer.Option(
                '--ported',
                help="Go on although packages' directories changed since their changes were set"
                ' pending: those changes are in the pending directories already.',
            ),
        ] = False,
    ) -> None:
        """Makes the pending packaging the packaging, and writes the release's entries.

        Every package release.packages selects gets a changelog entry for VERSION, `-` made `~`
        (1.3.0-rc1 is 1.3.0~rc1). Run it in a clean git working tree; it commits nothing.
        """
        with _errors():
            loaded = config.load(build_conf)
        try:
            release_tool.release(
                loaded, version, from_channel=from_channel, ported=ported, now=datetime.now(UTC)
            )
        except release_tool.ReleaseError as error:
            _fail(2, error)
        except release_tool.UnportedCommits as error:
            typer.echo(f'{PROG}: error: {error}', err=True)
            raise typer.Exit(1) from error

    @app.command('sources')
    def sources_command(
        build_conf: Annotated[
            Path, typer.Argument(metavar='BUILD_CONF', help='The build configuration.')
        ],
        packages: Annotated[
            list[str], typer.Argument(metavar='PACKAGE...', help='The packages to write it for.')
        ],
        distributions: Patterns = None,
        docker_host: Annotated[
            str | None,
            typer.Option(
                '--docker-host',
                metavar='URL',
                envvar='DISTROPACK_DOCKER_HOST',
                help='The endpoint of the container engine.',
            ),
        ] = None,
    ) -> None:
        """Writes the sources file of packages: the SHA-512 of each file their builds download.

        Run it when a package's upstream version changes, and review what it writes: run then
        refuses any downloaded file that does not match its line.
        """
        with _errors():
            loaded, selected = _selection(build_conf, distributions or [], date.today())
        unknown = [name for name in packages if name not in loaded.packages]
        if unknown:
            typer.echo(f'{PROG}: {unknown[0]} is not a package of {build_conf}', err=True)
            raise typer.Exit(2)
        disabled = disabled_releases(loaded, selected, date.today())
        try:
            engine = connect(docker_host)
        except (EngineNotFound, docker.errors.DockerException) as error:
            _fail(1, error)
        with TemporaryDirectory(prefix='distropack-build.') as workdir:
            jobs = (os.cpu_count() or 1) + 1
            context = BuildContext(engine, runner, Cancellation(), Path(workdir), jobs)
            failures = write_sources(context, loaded, selected, disabled, packages)
        for name, failure in failures.items():
            if failure is None:
                typer.echo(f'{PROG}: {name} downloads nothing for the releases selected', err=True)
            else:
                typer.echo(f'{PROG}: {name}: {failure}', err=True)
        if any(failure is not None for failure in failures.values()):
            raise typer.Exit(1)

    @images.command('bake-file')
    def bake_file(output: Output = None) -> None:
        """Writes the Docker bake definition of the images.

        It leaves out the releases whose base image the registry does not publish.
        """
        images_dir = Path(str(importlib.resources.files('distropack_builder') / 'images'))
        definition = bake.bake_definition(images_dir, bake.registry_has)
        _write(json.dumps(definition, indent=2) + '\n', output)

    @config_app.command('schema')
    def config_schema(output: Output = None) -> None:
        """Writes the JSON Schema of build.conf."""
        _write(json.dumps(config.schema(), indent=2) + '\n', output)

    @releases.command('list')
    def releases_list(
        build_conf: BuildConf = Path('build.conf'),
        distributions: Patterns = None,
        as_json: Annotated[
            bool, typer.Option('--json', help='Print a JSON list of {distrib, release} objects.')
        ] = False,
    ) -> None:
        """Prints the releases run would build for.

        One distribution:release per line, as -D takes them.
        """
        with _errors():
            loaded, selected = _selection(build_conf, distributions or [], date.today())
        found = [
            (dist_id, _release_name(release))
            for dist_id in loaded.distribs
            for release in sorted(selected[dist_id])
        ]
        listed = [{'distrib': d, 'release': r} for d, r in found]
        with _errors():
            write_outputs(os.environ, {'releases': _compact(listed)})
        if as_json:
            typer.echo(json.dumps(listed, indent=2))
        else:
            for dist_id, release in found:
                typer.echo(f'{dist_id}:{release}')

    return app


app = make_app()


def main() -> None:
    """Run the command line."""
    app(prog_name=PROG)
