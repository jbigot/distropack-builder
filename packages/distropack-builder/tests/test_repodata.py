import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import zstandard

from distropack_builder import repodata

RPMS = Path(__file__).parent / 'data' / 'rpms'
NS = {
    'repo': 'http://linux.duke.edu/metadata/repo',
    'common': 'http://linux.duke.edu/metadata/common',
}


def test_write(tmp_path: Path) -> None:
    shutil.copytree(RPMS, tmp_path / 'noarch')
    repodata.write(tmp_path)
    repomd = ET.parse(tmp_path / 'repodata' / 'repomd.xml').getroot()
    locations = {
        str(data.get('type')): str(data.find('repo:location', NS).get('href'))  # type: ignore[union-attr]
        for data in repomd.findall('repo:data', NS)
    }
    assert sorted(locations) == ['filelists', 'other', 'primary']
    with (tmp_path / locations['primary']).open('rb') as compressed:
        primary = ET.fromstring(zstandard.ZstdDecompressor().stream_reader(compressed).read())
    packages = primary.findall('common:package', NS)
    assert sorted(str(p.findtext('common:name', namespaces=NS)) for p in packages) == [
        'hello-app',
        'hello-lib',
    ]
    hrefs = sorted(str(p.find('common:location', NS).get('href')) for p in packages)  # type: ignore[union-attr]
    assert hrefs == [f'noarch/{name}-1.0-1.fc44.noarch.rpm' for name in ('hello-app', 'hello-lib')]


def test_no_rpmrc_error() -> None:
    """Importing the module prints nothing, on a host without rpm as on one with it."""
    result = subprocess.run(
        [sys.executable, '-c', 'import distropack_builder.repodata'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stderr == ''


def test_rpm_config_without_rpm(tmp_path: Path) -> None:
    environ: dict[str, str] = {}
    repodata.configure_rpm(environ, tmp_path / 'absent' / 'rpmrc')
    config = Path(environ['RPM_CONFIGDIR'])
    assert (config / 'rpmrc').is_file() and (config / 'macros').is_file()


def test_real_rpm_config_kept(tmp_path: Path) -> None:
    rpmrc = tmp_path / 'rpmrc'
    rpmrc.write_text('')
    environ: dict[str, str] = {}
    repodata.configure_rpm(environ, rpmrc)
    assert 'RPM_CONFIGDIR' not in environ


def test_explicit_rpm_config_kept(tmp_path: Path) -> None:
    environ = {'RPM_CONFIGDIR': '/somewhere'}
    repodata.configure_rpm(environ, tmp_path / 'absent')
    assert environ == {'RPM_CONFIGDIR': '/somewhere'}
