# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The metadata of rpm repositories, written by the createrepo_c bindings.

The only module that imports createrepo_c. Its wheel bundles a librpm that reads its configuration
from /usr/lib/rpm and prints an error at import where that does not exist: on a host without rpm,
librpm is pointed at an empty configuration shipped with the builder, which is all that writing
repository metadata needs.
"""

import os
import shutil
from collections.abc import MutableMapping
from importlib.resources import files
from pathlib import Path

SYSTEM_RPMRC = Path('/usr/lib/rpm/rpmrc')


def configure_rpm(environ: MutableMapping[str, str], system_rpmrc: Path) -> None:
    """Point librpm at the empty configuration, unless the system or the user provides one."""
    if 'RPM_CONFIGDIR' not in environ and not system_rpmrc.exists():
        environ['RPM_CONFIGDIR'] = str(files('distropack_builder') / 'rpm_config')


configure_rpm(os.environ, SYSTEM_RPMRC)

import createrepo_c  # noqa: E402  (librpm reads its configuration once, when loaded)


def write(directory: Path) -> None:
    """The metadata of the rpms under directory, in directory/repodata, as `createrepo_c .`.

    The writer writes in place: the previous metadata are set aside meanwhile, and put back when
    writing fails, so that a release whose publication fails keeps its last published metadata.
    """
    repodata = directory / 'repodata'
    previous = directory / '.repodata.previous'
    shutil.rmtree(previous, ignore_errors=True)
    if repodata.exists():
        repodata.rename(previous)
    try:
        rpms = sorted(directory.rglob('*.rpm'))
        with createrepo_c.RepositoryWriter(str(directory), num_packages=len(rpms)) as writer:
            for rpm in rpms:
                writer.add_pkg_from_file(str(rpm))
    except BaseException:
        shutil.rmtree(repodata, ignore_errors=True)
        if previous.exists():
            previous.rename(repodata)
        raise
    shutil.rmtree(previous, ignore_errors=True)


def read_primary(path: Path) -> list[tuple[str, str, str, str]]:
    """The packages of a primary metadata file, compressed or not: (name, arch, evr, sha256).

    evr is `[epoch:]version-release`, the epoch written only when not 0; sha256 the package's
    checksum, its `pkgId`.
    """
    found: list[tuple[str, str, str, str]] = []

    def package(pkg: createrepo_c.Package) -> None:
        epoch = f'{pkg.epoch}:' if pkg.epoch not in (None, '', '0') else ''
        found.append((pkg.name, pkg.arch, f'{epoch}{pkg.version}-{pkg.release}', pkg.pkgId))

    createrepo_c.xml_parse_primary(str(path), pkgcb=package, do_files=0)
    return found
