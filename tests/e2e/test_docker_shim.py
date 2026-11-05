"""The test-only docker shim drops --user from `docker run`, so master runs on rootless engines."""

import json
import os
import stat
import subprocess
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import ModuleType

import pytest

from conftest import DOCKER_SHIM_DIR


@pytest.fixture
def shim() -> ModuleType:
    """The shim as a module named docker_shim, so as not to shadow docker-py."""
    loader = SourceFileLoader('docker_shim', str(DOCKER_SHIM_DIR / 'docker'))
    spec = spec_from_loader('docker_shim', loader)
    assert spec is not None
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.parametrize('args,expected', [
    (['run', '--rm', '--user', '1000:1000', '--mount', 'type=bind', 'img', 'cmd'],
     ['run', '--rm', '--mount', 'type=bind', 'img', 'cmd']),
    (['run', '--rm', '--user=1000:1000', '--workdir', '/data', 'img'],
     ['run', '--rm', '--workdir', '/data', 'img']),
    (['run', '--rm', '--shm-size', '5g', 'img', 'tool', '--user', 'x'],
     ['run', '--rm', '--shm-size', '5g', 'img', 'tool', '--user', 'x']),
    (['info', '--user', 'x'], ['info', '--user', 'x']),
    ([], []),
], ids=['separate', 'joined', 'after-image', 'not-run', 'empty'])
def test_strip_user(shim: ModuleType, args: list[str], expected: list[str]) -> None:
    assert shim.strip_user(args) == expected


def test_shim_execs_real_docker(tmp_path: Path) -> None:
    fake = tmp_path / 'docker'
    fake.write_text('#!/bin/sh\n'
                    'exec python3 -c \'import json, sys; print(json.dumps(sys.argv[1:]))\' "$@"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = os.environ | {'PATH': os.pathsep.join([str(DOCKER_SHIM_DIR), str(tmp_path),
                                                 os.environ['PATH']])}
    result = subprocess.run(['docker', 'run', '--user', '1:1', 'img'], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ['run', 'img']
