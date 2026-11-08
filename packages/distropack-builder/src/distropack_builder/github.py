# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The outputs a command gives the GitHub Actions step running it."""

from collections.abc import Mapping
from pathlib import Path


class GithubOutputError(Exception):
    """The file GITHUB_OUTPUT names cannot be written."""


def write_outputs(environ: Mapping[str, str], outputs: Mapping[str, str]) -> None:
    """Append outputs, as `name=value` lines, to the file GITHUB_OUTPUT names in environ.

    Nothing happens when GITHUB_OUTPUT is not set, outside GitHub Actions.

    Raises:
        ValueError: a value holds a line break, which this form cannot carry.
        GithubOutputError: the file cannot be written.
    """
    path = environ.get('GITHUB_OUTPUT')
    if not path:
        return
    for name, value in outputs.items():
        if '\n' in value or '\r' in value:
            raise ValueError(f'the output {name} holds a line break')
    text = ''.join(f'{name}={value}\n' for name, value in outputs.items())
    try:
        with Path(path).open('a') as file:
            file.write(text)
    except OSError as error:
        raise GithubOutputError(f'cannot write the outputs to {path}: {error.strerror}') from None
