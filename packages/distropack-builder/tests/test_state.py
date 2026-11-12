# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The state directory: what each release keeps of its last builds."""

import os
from pathlib import Path

import pytest

from distropack_builder import state


def output(tmp_path: Path, name: str) -> Path:
    """A build's output directory: a source package and two binaries."""
    out = tmp_path / f'{name}-output'
    out.mkdir()
    for file in (
        f'{name}_1.0-1.dsc',
        f'{name}_1.0.orig.tar.gz',
        f'{name}_1.0-1_amd64.deb',
        f'{name}-doc_1.0-1_all.deb',
    ):
        (out / file).write_text(file)
    return out


def add(writer: state.ReleaseStateWriter, tmp_path: Path, name: str, rebuild: int = 0) -> None:
    writer.add(
        name,
        static_key='s' * 64,
        env_key='e' * 64,
        base_version='1.0-1~bpo13',
        rebuild=rebuild,
        version=f'1.0-1~bpo13+b{rebuild}' if rebuild else '1.0-1~bpo13',
        snapshot_commit=None,
        packaging_hash='p' * 64,
        outdir=output(tmp_path / str(rebuild), name),
        source_files=[f'{name}_1.0-1.dsc', f'{name}_1.0.orig.tar.gz'],
        arch_all_files=[f'{name}-doc_1.0-1_all.deb'],
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    for n in ('0', '1', '2'):
        (tmp_path / n).mkdir()
    return tmp_path / 'state'


def test_round_trip(root: Path, tmp_path: Path) -> None:
    writer = state.ReleaseStateWriter(root, 'release', 'debian-13-trixie')
    add(writer, tmp_path, 'pdi')
    writer.commit()
    stored = state.ReleaseState(root, 'release', 'debian-13-trixie')
    assert stored.exists()
    pdi = stored.package('pdi')
    assert pdi is not None
    assert (pdi.base_version, pdi.rebuild, pdi.env_key) == ('1.0-1~bpo13', 0, 'e' * 64)
    assert pdi.source_files == ('pdi_1.0-1.dsc', 'pdi_1.0.orig.tar.gz')
    assert (pdi.directory / 'pdi_1.0-1_amd64.deb').read_text() == 'pdi_1.0-1_amd64.deb'
    assert list(stored.packages()) == ['pdi']


def test_abort_keeps_the_previous_state(root: Path, tmp_path: Path) -> None:
    first = state.ReleaseStateWriter(root, 'release', 'r')
    add(first, tmp_path, 'pdi')
    first.commit()
    second = state.ReleaseStateWriter(root, 'release', 'r')
    add(second, tmp_path, 'pdi', rebuild=1)
    second.abort()
    pdi = state.ReleaseState(root, 'release', 'r').package('pdi')
    assert pdi is not None and pdi.rebuild == 0
    assert not (root / 'release' / 'r.new').exists()


def test_commit_with_fewer_packages_drops_the_others(root: Path, tmp_path: Path) -> None:
    first = state.ReleaseStateWriter(root, 'release', 'r')
    add(first, tmp_path, 'pdi')
    add(first, tmp_path, 'paraconf')
    first.commit()
    second = state.ReleaseStateWriter(root, 'release', 'r')
    add(second, tmp_path, 'pdi', rebuild=1)
    second.commit()
    assert list(state.ReleaseState(root, 'release', 'r').packages()) == ['pdi']


@pytest.mark.parametrize('manifest', ['', '{', '{"schema": 0, "packages": {}}', '[]'])
def test_bad_manifest_is_no_state(root: Path, manifest: str) -> None:
    directory = root / 'release' / 'r'
    directory.mkdir(parents=True)
    (directory / 'manifest.json').write_text(manifest)
    stored = state.ReleaseState(root, 'release', 'r')
    assert not stored.exists()
    assert stored.package('pdi') is None
    assert stored.packages() == {}


def test_missing_state(root: Path) -> None:
    stored = state.ReleaseState(root, 'release', 'r')
    assert not stored.exists() and stored.packages() == {}


def test_missing_stored_file_makes_its_package_absent(root: Path, tmp_path: Path) -> None:
    writer = state.ReleaseStateWriter(root, 'release', 'r')
    add(writer, tmp_path, 'pdi')
    add(writer, tmp_path, 'paraconf')
    writer.commit()
    (root / 'release' / 'r' / 'pdi' / 'pdi_1.0-1.dsc').unlink()
    stored = state.ReleaseState(root, 'release', 'r')
    assert stored.package('pdi') is None
    assert list(stored.packages()) == ['paraconf']


def packaging(tmp_path: Path) -> Path:
    directory = tmp_path / 'packaging'
    (directory / 'debian').mkdir(parents=True)
    (directory / 'debian' / 'control').write_text('Source: pdi\n')
    return directory


def test_static_key(tmp_path: Path) -> None:
    pack = packaging(tmp_path)
    source = tmp_path / 'pdi-1.0.tar.gz'
    source.write_bytes(b'upstream')
    key = state.static_key(pack, {'pdi-1.0.tar.gz': source}, 'release')
    assert key == state.static_key(pack, {'pdi-1.0.tar.gz': source}, 'release')
    os.utime(pack / 'debian' / 'control', (0, 0))
    assert key == state.static_key(pack, {'pdi-1.0.tar.gz': source}, 'release')  # not mtimes
    assert key != state.static_key(pack, {'pdi-1.0.tar.gz': source}, 'nightly')
    source.write_bytes(b'other upstream')
    changed_source = state.static_key(pack, {'pdi-1.0.tar.gz': source}, 'release')
    assert changed_source != key
    (pack / 'debian' / 'rules').write_text('#!/usr/bin/make -f\n')
    added = state.static_key(pack, {'pdi-1.0.tar.gz': source}, 'release')
    assert added != changed_source
    (pack / 'debian' / 'control').write_text('Source: pdi\nSection: libs\n')
    assert state.static_key(pack, {'pdi-1.0.tar.gz': source}, 'release') != added


def test_static_key_of_a_directory_unchanged(tmp_path: Path) -> None:
    """The keys Phase 11 stored for a packaging directory stay valid."""
    source = tmp_path / 'pdi-1.0.tar.gz'
    source.write_bytes(b'upstream')
    assert state.static_key(packaging(tmp_path), {'pdi-1.0.tar.gz': source}, 'release') == (
        '996e003a1faa1ac7e055708ecfca109dc2235489cffee0d97ff58a8f3e68e15e'
    )


def test_static_key_of_a_spec_file(tmp_path: Path) -> None:
    directory = tmp_path / 'hello'
    directory.mkdir()
    spec = directory / 'hello.spec'
    spec.write_text('Name: hello\n')
    (directory / 'notes.txt').write_text('not packaging\n')
    key = state.static_key(spec, {}, 'stable')
    os.utime(spec, (0, 0))
    assert state.static_key(spec, {}, 'stable') == key  # not mtimes
    assert state.static_key(directory, {}, 'stable') != key
    spec.write_text('Name: hello\nRelease: 2\n')
    assert state.static_key(spec, {}, 'stable') != key


def test_packaging_key_ignores_sources_and_channel(tmp_path: Path) -> None:
    pack = packaging(tmp_path)
    source = tmp_path / 'pdi-1.0.tar.gz'
    source.write_bytes(b'upstream')
    assert state.packaging_key(pack) == state.static_key(pack, {}, '')
    assert state.packaging_key(pack) != state.static_key(pack, {'pdi-1.0.tar.gz': source}, '')
    assert state.RELEASE_CHANNEL == 'stable'
