# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The sources file of a package: what each file its build downloads must be.

One `SHA512 (<file>) = <digest>` line per file, in Fedora's format, sorted by name, in the
package's directory. Every downloaded file must match its line, unless uscan verified its upstream
signature; a line for a file no build downloads is ignored.
"""

import functools
import hashlib
import re
from collections.abc import Mapping
from pathlib import Path

from distropack_builder.downloads import DownloadFailed

FILE = 'sources'


class SourcesError(ValueError):
    """A sources file that cannot be read: its message names the file and the line."""


_LINE = re.compile(r'^SHA512 \((?P<name>[^)/]+)\) = (?P<digest>[0-9a-f]{128})$')


@functools.cache
def _digest(path: Path) -> str:
    """The SHA-512 of the file at path, computed once per run however many builds check it."""
    digest = hashlib.sha512()
    with path.open('rb') as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def line(name: str, path: Path) -> str:
    """The sources line of the file at path, named name."""
    return f'SHA512 ({name}) = {_digest(path)}'


def read(package_dir: Path) -> dict[str, str]:
    """The digests the sources file of package_dir lists, by file name; none without the file.

    Raises:
        SourcesError: a line is not a `SHA512 (<file>) = <digest>` line.
    """
    path = package_dir / FILE
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    result = {}
    for number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        found = _LINE.match(raw.strip())
        if found is None:
            raise SourcesError(f'{path}:{number}: not a SHA512 (<file>) = <digest> line')
        result[found['name']] = found['digest']
    return result


def check(name: str, path: Path, expected: Mapping[str, str]) -> DownloadFailed | None:
    """Why the file at path, named name, is not what expected lists for it; None when it is."""
    if name not in expected:
        return DownloadFailed(name, f'no sources line for {name}')
    if _digest(path) != expected[name]:
        return DownloadFailed(name, f'{name} does not match its sources line')
    return None
