# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The subprocesses running the builder read the recorded release data, never the network."""

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import respx

from distro_releases import distribution
from support import source0

Project = Callable[[dict[str, list[str]], list[str]], Path]


def run_python(code: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True)


def test_subprocess_reads_recorded_releases(
    recorded_http: respx.MockRouter, pkgbuild_env: dict[str, str]
) -> None:
    debian = distribution('debian').releases()
    result = run_python(
        'from distro_releases import distribution\n'
        'print(",".join(str(r.codename) for r in distribution("debian").releases()))',
        pkgbuild_env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ','.join(str(r.codename) for r in debian)


def test_subprocess_refuses_unrecorded_http(pkgbuild_env: dict[str, str]) -> None:
    result = run_python('import httpx\nhttpx.get("https://example.invalid/x")', pkgbuild_env)
    assert result.returncode != 0
    assert 'https://example.invalid/x' in result.stderr


def test_subprocess_reaches_the_local_server(
    project: Project, pkgbuild_env: dict[str, str]
) -> None:
    project_dir = project({'fedora': ['44']}, ['hello-lib'])
    url = source0(project_dir / 'hello-lib' / 'hello-lib.spec')
    result = run_python(
        f'import httpx\nprint(len(httpx.get({url!r}).raise_for_status().content))',
        pkgbuild_env,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout) > 0
