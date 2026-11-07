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
    """The metadata of the rpms under directory, in directory/repodata, as `createrepo_c .`."""
    shutil.rmtree(directory / 'repodata', ignore_errors=True)  # the previous run's, if any
    rpms = sorted(directory.rglob('*.rpm'))
    with createrepo_c.RepositoryWriter(str(directory), num_packages=len(rpms)) as writer:
        for rpm in rpms:
            writer.add_pkg_from_file(str(rpm))
