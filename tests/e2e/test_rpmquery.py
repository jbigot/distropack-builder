# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The spec queries, answered by the real builder images: macros expand as at build time."""

import pytest

from distropack_builder.bake import PROTOCOL, REGISTRY
from distropack_builder.context import BuildContext
from distropack_builder.docker import Cancellation, Engine, run
from distropack_builder.rpmquery import SpecFailed, SpecInfo, query

HEADER = """Name:           {name}
Version:        1.0
Release:        1%{{?dist}}
Summary:        {name}
License:        MIT
Source0:        https://example.invalid/{name}-%{{version}}%{{?dist}}.tar.gz
"""
SPECS = {
    'always': HEADER + 'BuildRequires:  hello-lib\n',
    'on-el': HEADER + '%if 0%{{?rhel}}\nBuildRequires:  hello-lib\n%endif\n',
    'broken': HEADER + 'Bogus:          tag\n',
}
pytestmark = [pytest.mark.e2e, pytest.mark.enable_socket]

IMAGES = {'.fc44': f'fedora_builder:44-p{PROTOCOL}', '.el10': f'el_builder:10-p{PROTOCOL}'}


@pytest.fixture(scope='module')
def answers(
    docker_engine: None, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, dict[str, SpecInfo | SpecFailed]]:
    """The answers of each image, by dist tag."""
    work = tmp_path_factory.mktemp('rpmquery')
    specs = {}
    for name, text in SPECS.items():
        specs[name] = work / f'{name}.spec'
        specs[name].write_text(text.format(name=name) + f'\n%description\n{name}\n')
    context = BuildContext(Engine.connect(), run, Cancellation(), work, 1)
    return {dist: query(context, REGISTRY + image, specs) for dist, image in IMAGES.items()}


def info(answer: SpecInfo | SpecFailed) -> SpecInfo:
    assert isinstance(answer, SpecInfo), answer
    return answer


@pytest.mark.parametrize('dist', sorted(IMAGES))
def test_unconditional_build_requires(
    answers: dict[str, dict[str, SpecInfo | SpecFailed]], dist: str
) -> None:
    assert 'hello-lib' in info(answers[dist]['always']).buildrequires


def test_conditional_build_requires(answers: dict[str, dict[str, SpecInfo | SpecFailed]]) -> None:
    """A BuildRequires inside %if 0%{?rhel} counts on EL only (note 165)."""
    assert 'hello-lib' in info(answers['.el10']['on-el']).buildrequires
    assert 'hello-lib' not in info(answers['.fc44']['on-el']).buildrequires


@pytest.mark.parametrize('dist', sorted(IMAGES))
def test_sources_per_release(
    answers: dict[str, dict[str, SpecInfo | SpecFailed]], dist: str
) -> None:
    assert info(answers[dist]['always']).sources == (
        f'https://example.invalid/always-1.0{dist}.tar.gz',
    )
    assert info(answers[dist]['always']).provides >= {'always'}


@pytest.mark.parametrize('dist', sorted(IMAGES))
def test_broken_spec_fails_alone(
    answers: dict[str, dict[str, SpecInfo | SpecFailed]], dist: str
) -> None:
    broken = answers[dist]['broken']
    assert isinstance(broken, SpecFailed) and 'Bogus' in broken.message
