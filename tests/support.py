"""Constants shared by the test files and the fixtures of conftest.py."""

import gzip
import io
import shutil
import tarfile
from datetime import date
from pathlib import Path

import yaml

from distro_releases import Release, distribution

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / 'tests' / 'characterization' / 'data'
# The recorded release data, shared with distro-releases' own tests.
RELEASE_DATA = REPO_ROOT / 'packages' / 'distro-releases' / 'tests' / 'data'
# The modules load_script reloads, so that each test starts from fresh module state.
MODULES = (
    'distropack_builder.targets',
    'distropack_builder.bake',
    'distropack_builder.builders',
    'distropack_builder.cli',
)
# The names the tests pass to load_script, from before the move, and the modules they load.
SCRIPT_MODULES = {
    'build': 'distropack_builder.bake',
    'pkgbuild': 'distropack_builder.cli',
}
FROZEN_DATE = date(2026, 11, 5)
FIXTURE_PROJECT = REPO_ROOT / 'tests' / 'e2e' / 'project'
FIXTURE_PASSPHRASE = 'test-passphrase'
# The releases the shared fixture build covers: one per family, and Rocky Linux for Phase 10's el.
FIXTURE_RELEASES = {'debian': ['trixie'], 'ubuntu': ['noble'], 'fedora': ['44'], 'rocky': ['10']}


def made_release(dist: str, version: str | None, codename: str | None, order: int) -> Release:
    """A release of dist with only its identity filled in, for tests needing no release data."""
    return Release(
        distribution(dist),
        version,
        codename,
        version or str(codename),
        order,
        None,
        None,
        None,
        None,
        None,
    )


TRIXIE = made_release('debian', '13', 'trixie', 13)
FORKY = made_release('debian', '14', 'forky', 14)
SID = made_release('debian', None, 'sid', 1000)


def write_tarball(source: Path, target: Path) -> None:
    """A reproducible .tar.gz of source, whose single top-level directory is source's name."""

    def reset(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, '', ''
        return info

    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w') as tar:
        for path in [source, *sorted(source.rglob('*'))]:
            tar.add(
                path, arcname=str(path.relative_to(source.parent)), recursive=False, filter=reset
            )
    with gzip.GzipFile(target, 'wb', mtime=0) as gz:
        gz.write(data.getvalue())


def make_project(
    target: Path, orig_base: str, versions: dict[str, list[str]], packages: list[str]
) -> Path:
    """Copy the fixture project to target, limited to some releases and packages.

    Every *.in file of the copy is rendered with @ORIG_BASE@ set to orig_base.
    """
    project_dir = Path(
        shutil.copytree(FIXTURE_PROJECT, target, ignore=shutil.ignore_patterns('upstream'))
    )
    for template in project_dir.rglob('*.in'):
        template.with_suffix('').write_text(template.read_text().replace('@ORIG_BASE@', orig_base))
        template.unlink()
    conf_path = project_dir / 'build.conf'
    conf = yaml.safe_load(conf_path.read_text())
    conf['distribs'] = {
        dist: conf['distribs'][dist] | {'versions': rels} for dist, rels in versions.items()
    }
    conf['packages'] = {name: conf['packages'][name] for name in packages}
    conf_path.write_text(yaml.safe_dump(conf))
    return project_dir
