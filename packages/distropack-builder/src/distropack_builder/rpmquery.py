# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""What the specs of a release say, read by the release's own rpm, in one container.

rpmspec runs in the target's builder image, so that its macros (`%{?rhel}`, `%{?dist}`) expand as
they do at build time. The image's entrypoint, given `query`, answers for every spec mounted under
`/specs/<package>.spec`, in `/out/<package>/`: `buildrequires`, `provides`, `parsed` (the outputs
of `rpmspec -q --buildrequires`, `rpmspec -q --provides` and `rpmspec -P`), `status` (the exit
status of the first failing command, 0 otherwise) and `errors` (their standard error).
"""

import logging
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from distropack_builder.context import BuildContext
from distropack_builder.docker import Mount


@dataclass(frozen=True)
class SpecInfo:
    """What a spec says, for the builder.

    Attributes:
        buildrequires: the names of its build dependencies, without their versions.
        provides: the names of what its packages provide, without their versions.
        sources: the URLs of its `Source` lines, in order.
    """

    buildrequires: frozenset[str]
    provides: frozenset[str]
    sources: tuple[str, ...]


@dataclass(frozen=True)
class SpecFailed:
    """rpmspec could not read the spec: message says why."""

    message: str


_SOURCE = re.compile(r'^\s*source[0-9]*\s*:\s*', re.IGNORECASE)


def _names(text: str) -> frozenset[str]:
    """The names of a list of dependencies, one per line, versions and qualifiers cut."""
    return frozenset(re.sub(r'[\( ].*', '', line) for line in text.splitlines() if line.strip())


def parse(package: str, answers: Path) -> SpecInfo | SpecFailed:
    """What the query container answered for package's spec, in the directory answers."""
    try:
        status = (answers / 'status').read_text().strip()
        errors = (answers / 'errors').read_text().strip()
        if status != '0':
            return SpecFailed(f'rpmspec cannot read {package}.spec (status {status}): {errors}')
        parsed = (answers / 'parsed').read_text()
        return SpecInfo(
            _names((answers / 'buildrequires').read_text()),
            _names((answers / 'provides').read_text()),
            tuple(
                _SOURCE.sub('', line, count=1)
                for line in parsed.splitlines()
                if _SOURCE.match(line)
            ),
        )
    except OSError as error:
        return SpecFailed(f'no answer for {package}.spec: {error}')


def query(
    context: BuildContext,
    image: str,
    specs: Mapping[str, Path],
    logger: logging.Logger | None = None,
) -> dict[str, SpecInfo | SpecFailed]:
    """Ask the builder image of a release what each of specs says, a {package: spec file} map.

    One container answers for every spec; a spec it cannot read fails alone.

    Raises:
        ContainerFailed: the container itself failed.
        Cancelled: the run was cancelled.
    """
    if not specs:
        return {}
    with tempfile.TemporaryDirectory(prefix='rpmquery.', dir=context.workdir) as scratch:
        out = Path(scratch)
        context.run(
            context.engine,
            image,
            ['query'],
            mounts=[Mount('/out', out)]
            + [Mount(f'/specs/{name}.spec', path, read_only=True) for name, path in specs.items()],
            as_root=True,
            cancellation=context.cancellation,
            logger=logger,
        )
        return {name: parse(name, out / name) for name in specs}
