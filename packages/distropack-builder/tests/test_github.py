# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

from pathlib import Path

import pytest

from distropack_builder.github import GithubOutputError, write_outputs


def test_write_outputs_appends(tmp_path: Path) -> None:
    output = tmp_path / 'output'
    output.write_text('earlier=1\n')
    write_outputs({'GITHUB_OUTPUT': str(output)}, {'failed': 'false', 'output-dirs': '{}'})
    assert output.read_text() == 'earlier=1\nfailed=false\noutput-dirs={}\n'


def test_write_outputs_without_github_output_does_nothing(tmp_path: Path) -> None:
    write_outputs({}, {'failed': 'false'})
    assert list(tmp_path.iterdir()) == []


def test_newline_in_value_is_a_bug(tmp_path: Path) -> None:
    output = tmp_path / 'output'
    with pytest.raises(ValueError):
        write_outputs({'GITHUB_OUTPUT': str(output)}, {'failed': 'a\nb'})
    assert not output.exists()


def test_unwritable_output_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(GithubOutputError, match='cannot write the outputs to'):
        write_outputs({'GITHUB_OUTPUT': str(tmp_path / 'missing' / 'output')}, {'failed': 'true'})
