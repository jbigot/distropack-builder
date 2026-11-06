"""The subprocesses running the builder read the recorded release data, never the network."""

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import yaml

Project = Callable[[dict[str, list[str]], list[str]], Path]


def run_python(code: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True)


def test_subprocess_reads_recorded_releases(
    load_script: Callable[[str], ModuleType], pkgbuild_env: dict[str, str]
) -> None:
    debian = next(load_script('distinfo.py').distributions(id='debian'))
    result = run_python(
        'from distro_releases.distinfo import distributions\n'
        'debian = next(distributions(id="debian"))\n'
        'print(",".join(str(r.codename) for r in debian.releases()))',
        pkgbuild_env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ','.join(str(r.codename) for r in debian.releases())


def test_subprocess_refuses_unrecorded_urls(pkgbuild_env: dict[str, str]) -> None:
    result = run_python(
        'import urllib.request\nurllib.request.urlopen("https://example.invalid/x")', pkgbuild_env
    )
    assert result.returncode != 0
    assert 'no recording for https://example.invalid/x' in result.stderr


def test_subprocess_reaches_the_local_server(
    project: Project, pkgbuild_env: dict[str, str]
) -> None:
    conf = yaml.safe_load(
        (project({'debian': ['trixie']}, ['hello-lib']) / 'build.conf').read_text()
    )
    url = conf['packages']['hello-lib']['orig'].format(upstream_version='1.0', package='hello-lib')
    result = run_python(
        f'import urllib.request, sys\nprint(len(urllib.request.urlopen({url!r}).read()))',
        pkgbuild_env,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout) > 0
