"""The fixture project is rendered and served as the e2e tests expect."""

import tarfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

import yaml

Project = Callable[[dict[str, list[str]], list[str]], Path]


def test_project_is_narrowed_and_rendered(project: Project) -> None:
    project_dir = project({'fedora': ['44']}, ['hello-lib'])
    conf = yaml.safe_load((project_dir / 'build.conf').read_text())
    assert {d: c['versions'] for d, c in conf['distribs'].items()} == {'fedora': ['44']}
    assert list(conf['packages']) == ['hello-lib']
    assert not list(project_dir.rglob('*.in'))
    assert not (project_dir / 'upstream').exists()
    spec = (project_dir / 'hello-lib' / 'hello-lib.spec').read_text()
    assert 'Source0:        http://127.0.0.1:' in spec


def test_upstream_tarball_is_served(project: Project, tmp_path: Path) -> None:
    project_dir = project({'debian': ['trixie']}, ['hello-lib'])
    conf = yaml.safe_load((project_dir / 'build.conf').read_text())
    url = conf['packages']['hello-lib']['orig'].format(upstream_version='1.0', package='hello-lib')
    with urllib.request.urlopen(url) as response:
        (tmp_path / 'orig.tar.gz').write_bytes(response.read())
    with tarfile.open(tmp_path / 'orig.tar.gz') as tar:
        assert sorted(tar.getnames()) == [
            'hello-lib-1.0',
            'hello-lib-1.0/Makefile',
            'hello-lib-1.0/hello.sh',
        ]
