"""What the sources share."""

from collections.abc import Iterator
from contextlib import contextmanager

from distro_releases.errors import SourceError


def cpe(prefix: str | None, version: str | None) -> str | None:
    """The CPE of a release, from its distribution's prefix, if both exist."""
    return None if prefix is None or version is None else f'{prefix}:{version}'


@contextmanager
def parsing(url: str) -> Iterator[None]:
    """Turn the errors of parsing what url served into a SourceError naming it.

    A source answering with something else than its data (a maintenance page, a changed format) is
    a source that cannot be read.
    """
    try:
        yield
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise SourceError(url, f'unexpected content: {error!r}') from error
