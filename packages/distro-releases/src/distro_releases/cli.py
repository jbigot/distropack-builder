"""The command line of distro-releases."""

import json
from datetime import date, datetime
from typing import Annotated

import typer

from distro_releases.catalog import distribution, distributions
from distro_releases.errors import SelectorError, SourceError, UnknownDistribution
from distro_releases.model import Release
from distro_releases.selectors import select

app = typer.Typer(no_args_is_help=True, add_completion=False)

COLUMNS = ('DISTRIBUTION', 'VERSION', 'CODENAME', 'SUITE', 'RELEASED', 'EOL', 'STATUS')


@app.callback()
def main() -> None:
    """Release data of Linux distributions: which releases exist and which are supported."""


def status(release: Release, on: date) -> str:
    """`eol`, `supported` or `unreleased`, as of on."""
    if release.is_eol(on):
        return 'eol'
    return 'supported' if release.is_supported(on) else 'unreleased'


def selected(selectors: list[str], on: date) -> list[Release]:
    """The releases the selectors name, by distribution then in time."""
    if not selectors:
        selectors = [d.id for d in distributions()]
    result: set[Release] = set()
    for selector in selectors:
        dist_id, _, tokens = selector.partition(':')
        result |= select(distribution(dist_id), tokens.split(',') if tokens else ['supported'], on)
    return sorted(result, key=lambda r: (r.distribution.id, r.order))


def as_json(release: Release, on: date) -> dict[str, str | None]:
    """One release as a JSON object."""

    def iso(day: date | None) -> str | None:
        return None if day is None else day.isoformat()

    return {
        'distribution': release.distribution.id,
        'version': release.version,
        'codename': release.codename,
        'name': release.name,
        'suite': release.suite,
        'release_date': iso(release.release_date),
        'eol_date': iso(release.eol_date),
        'status': status(release, on),
    }


def table(releases: list[Release], on: date) -> str:
    """The releases as an aligned table, `-` for an absent value."""
    rows = [COLUMNS] + [
        tuple(
            value or '-'
            for value in (
                r.distribution.id,
                r.version,
                r.codename,
                r.suite,
                r.release_date and r.release_date.isoformat(),
                r.eol_date and r.eol_date.isoformat(),
                status(r, on),
            )
        )
        for r in releases
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(len(COLUMNS))]
    return '\n'.join(
        '  '.join(v.ljust(w) for v, w in zip(row, widths, strict=True)).rstrip() for row in rows
    )


@app.command('list')
def list_releases(
    selectors: Annotated[
        list[str] | None,
        typer.Argument(
            help='DISTRIBUTION for its supported releases, or DISTRIBUTION:TOKENS with the '
            'release tokens of build.conf, comma-separated (debian:bookworm,+ or debian:all). '
            'None: the supported releases of every distribution.',
            show_default=False,
        ),
    ] = None,
    on: Annotated[
        datetime | None,
        typer.Option(
            formats=['%Y-%m-%d'], help='The date the status is computed for; today by default.'
        ),
    ] = None,
    as_json_: Annotated[bool, typer.Option('--json', help='Print a JSON list of objects.')] = False,
) -> None:
    """List releases with their dates, suite and status."""
    day = on.date() if on else date.today()
    try:
        releases = selected(selectors or [], day)
    except (UnknownDistribution, SelectorError) as error:
        typer.echo(f'distro-releases: {error}', err=True)
        raise typer.Exit(2) from error
    except SourceError as error:
        typer.echo(f'distro-releases: {error}', err=True)
        raise typer.Exit(1) from error
    if as_json_:
        typer.echo(json.dumps([as_json(r, day) for r in releases], indent=2))
    else:
        typer.echo(table(releases, day))
