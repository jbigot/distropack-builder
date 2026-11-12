# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

from pathlib import Path

import pytest

from distropack_builder.rpmquery import SpecFailed, SpecInfo, parse


def answers(tmp_path: Path, status: str = '0\n', errors: str = '', **files: str) -> Path:
    out = tmp_path / 'p'
    out.mkdir()
    for name in ('buildrequires', 'provides', 'parsed'):
        (out / name).write_text(files.get(name, ''))
    (out / 'status').write_text(status)
    (out / 'errors').write_text(errors)
    return out


def test_parse(tmp_path: Path) -> None:
    out = answers(
        tmp_path,
        buildrequires='make\ncmake >= 3.10\npkgconfig(zlib)\n\n',
        provides='p = 1.0-1.fc44\np-devel(x86-64) = 1.0-1.fc44\n',
        parsed='Name: p\nSource0: https://x/p-1.0.tar.gz\nsource1 :  https://x/a.patch\n'
        'Sourcery: no\nURL: https://x\nVersion: 1.0\nRelease: 1.fc44\n',
    )
    assert parse('p', out) == SpecInfo(
        buildrequires=frozenset({'make', 'cmake', 'pkgconfig'}),
        provides=frozenset({'p', 'p-devel'}),
        sources=('https://x/p-1.0.tar.gz', 'https://x/a.patch'),
        version='1.0',
        release='1.fc44',
    )


def test_parse_failure(tmp_path: Path) -> None:
    out = answers(tmp_path, status='1\n', errors='error: line 3: Unknown tag: Bogus\n')
    failed = parse('p', out)
    assert isinstance(failed, SpecFailed)
    assert 'p.spec' in failed.message and 'Unknown tag: Bogus' in failed.message


def test_parse_missing_answers(tmp_path: Path) -> None:
    """A spec the container never answered is a failure, not an empty spec."""
    assert isinstance(parse('p', tmp_path / 'p'), SpecFailed)


@pytest.mark.parametrize('status', ['', 'x\n'])
def test_parse_bad_status(tmp_path: Path, status: str) -> None:
    assert isinstance(parse('p', answers(tmp_path, status=status)), SpecFailed)


def test_spec_version_and_release_expanded(tmp_path: Path) -> None:
    out = answers(
        tmp_path,
        parsed='Name: p\nVersion:  1.0\nRelease:\t1.fc44\n%package devel\nVersion: 9\nRelease: 9\n',
    )
    info = parse('p', out)
    assert isinstance(info, SpecInfo)
    assert (info.version, info.release) == ('1.0', '1.fc44')


@pytest.mark.parametrize('parsed', ['Name: p\nVersion: 1.0\n', 'Name: p\nRelease: 1\n'])
def test_spec_without_release_fails(tmp_path: Path, parsed: str) -> None:
    assert parse('p', answers(tmp_path, parsed=parsed)) == SpecFailed('p: no Version or Release')
