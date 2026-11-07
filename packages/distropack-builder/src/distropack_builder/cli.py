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
from contextlib import ExitStack, contextmanager
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import FrameType
from typing import Annotated, NoReturn

import docker.errors
import typer
from rich.console import Console

from distro_releases import Release, SelectorError, SourceError, UnknownDistribution
from distropack_builder import bake, config, readme, targets
from distropack_builder.availability import AvailabilityError, image_available
from distropack_builder.builders import builder_image, run_builds
from distropack_builder.config import BuildConfig, ConfigError, Distrib
from distropack_builder.context import BuildContext, Runner
from distropack_builder.docker import Cancellation, Engine
from distropack_builder.docker import run as run_container
from distropack_builder.docker.endpoint import EngineNotFound, RemoteEndpoint
from distropack_builder.downloads import new_client
from distropack_builder.report import Reporter, github_mode
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
_RUN_ERRORS = (AvailabilityError, SigningKeyError, SourceError)


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
    ) -> bool:
        """Build and publish; whether anything failed."""
        # One date for the whole run, so that a run crossing midnight sees one list of releases.
        on = date.today()
        # The configuration's errors, the release data and the images available come before any
        # work; then the engine, before any container.
        loaded, selected = _selection(build_conf, patterns, on)
        readme.check(readme.project_dir(loaded.root))
        if not any(selected.values()):
            typer.echo(f'{PROG}: nothing to build: {build_conf} selects no release', err=True)
            return False
        disabled = disabled_releases(loaded, selected, on)
        try:
            engine = connect(docker_host)
        except (EngineNotFound, docker.errors.DockerException) as error:
            _fail(1, error)

        def signer_for(distrib: Distrib) -> Signer:
            return Signer.load(
                loaded.root / distrib.gpg.file,
                passphrase,
                keyid=distrib.gpg.id,
                uid=distrib.gpg.uid,
            )

        with ExitStack() as stack:
            workdir = Path(stack.enter_context(TemporaryDirectory(prefix='distropack-build.')))
            context = BuildContext(engine, runner, Cancellation(), workdir, jobs)
            summary = os.environ.get('GITHUB_STEP_SUMMARY')
            reporter = stack.enter_context(
                Reporter(
                    Console(),
                    verbose=verbose,
                    github=github_mode(os.environ),
                    summary=Path(summary) if summary else None,
                )
            )
            return run_builds(context, reporter, loaded, selected, disabled, signer_for)

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
    ) -> None:
        """Builds the packages of build.conf, and publishes their signed repositories."""
        # SIGTERM (kill, timeout, a CI job cancelled) takes the path of Ctrl-C: the builds are
        # cancelled, their containers killed, nothing is published, the work directory removed.
        previous = signal.signal(signal.SIGTERM, _interrupt)
        try:
            with _errors():
                failed = build(
                    build_conf,
                    distributions or [],
                    jobs or (os.cpu_count() or 1) + 1,
                    verbose,
                    read_passphrase(passphrase_file, os.environ),
                    docker_host,
                )
        except KeyboardInterrupt:
            typer.echo(f'{PROG}: interrupted', err=True)
            raise typer.Exit(INTERRUPTED) from None
        finally:
            signal.signal(signal.SIGTERM, previous)
        if failed:
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
        if as_json:
            typer.echo(json.dumps([{'distrib': d, 'release': r} for d, r in found], indent=2))
        else:
            for dist_id, release in found:
                typer.echo(f'{dist_id}:{release}')

    return app


app = make_app()


def main() -> None:
    """Run the command line."""
    app(prog_name=PROG)
