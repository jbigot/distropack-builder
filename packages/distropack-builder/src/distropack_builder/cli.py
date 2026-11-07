import argparse
from collections.abc import Callable
from contextlib import ExitStack
from datetime import date
import importlib.resources
import json
import os
from os import cpu_count
from pathlib import Path
import signal
from subprocess import CalledProcessError
import sys
from sys import stderr
from tempfile import TemporaryDirectory

import docker.errors
from rich.console import Console

from distro_releases import (
    SelectorError,
    SourceError,
    UnknownDistribution,
)
from distropack_builder import targets
from distropack_builder.availability import AvailabilityError, image_available
from distropack_builder.bake import REGISTRY
from distropack_builder.builders import builder_image, run_builds
from distropack_builder.config import ConfigError, load
from distropack_builder.context import BuildContext, Runner
from distropack_builder.docker import Cancellation, Engine
from distropack_builder.docker import run as run_container
from distropack_builder.docker.endpoint import EngineNotFound, RemoteEndpoint
from distropack_builder.downloads import (
    new_client,
)
from distropack_builder.report import Reporter, github_mode
from distropack_builder.selection import configured_releases, disabled_releases
from distropack_builder.signing import SigningKeyError


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
        config = load(base_dir / args.build_conf)

        # The configuration's errors, the release data and the images available come before any
        # work.
        patterns = {e for el in args.distributions or [] for e in el}
        with new_client() as client:
            selected = configured_releases(
                config,
                patterns,
                on,
                lambda release: image_available(REGISTRY + builder_image(release), client),
            )
        disabled = disabled_releases(config, selected, on)

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
            failed = run_builds(context, reporter, config, selected, disabled, args)
        if failed:
            sys.exit(1)

    except (ConfigError, targets.NotATarget, UnknownDistribution, SelectorError) as error:
        parser.error(str(error))
    except (SourceError, SigningKeyError, AvailabilityError) as error:
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
