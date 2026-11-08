# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The versioned documentation site: one version per release tag, plus main."""

import re
import subprocess
from pathlib import Path

import pytest

from workspace_helpers import git, tag

pytestmark = pytest.mark.packaging


def build_site(repo: Path, out: Path) -> Path:
    """The versioned site of repo, built into out; out."""
    subprocess.run(
        ['uv', 'run', '-q', '--group', 'docs', 'sphinx-polyversion', 'docs/poly.py', str(out)],
        cwd=repo,
        check=True,
    )
    return out


def versions(site: Path) -> set[str]:
    return {path.parent.name for path in site.glob('*/index.html')}


def redirect(site: Path) -> str:
    match = re.search(r'url=([^"]+)"', (site / 'index.html').read_text())
    assert match, 'the root page redirects nowhere'
    return match[1]


def test_without_tag_builds_main_alone(repo: Path, tmp_path: Path) -> None:
    site = build_site(repo, tmp_path / 'site')
    assert versions(site) == {'main'}
    assert redirect(site) == 'main/'


def test_root_opens_the_highest_version(repo: Path, tmp_path: Path) -> None:
    tag(repo, 'distropack-builder-v1.9.0', 'HEAD~1')
    tag(repo, 'distropack-builder-v1.10.0')
    tag(repo, 'distro-releases-v5.0.0')
    tag(repo, 'v9.0.0')
    site = build_site(repo, tmp_path / 'site')
    assert versions(site) == {'main', '1.9.0', '1.10.0'}
    assert redirect(site) == '1.10.0/'
    switcher = (site / '1.9.0' / 'index.html').read_text()
    for name in ('main', '1.9.0', '1.10.0'):
        assert f'../{name}/index.html' in switcher, name


def test_a_failing_version_fails_the_build(repo: Path, tmp_path: Path) -> None:
    broken = repo / 'docs' / 'broken.md'
    broken.write_text('# Broken\n\n{ref}`no-such-label`\n')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', 'A page with a dangling reference')
    tag(repo, 'distropack-builder-v1.0.0')
    git(repo, 'rm', '-q', 'docs/broken.md')
    git(repo, 'commit', '-q', '-m', 'Remove it')
    result = subprocess.run(
        [
            'uv',
            'run',
            '-q',
            '--group',
            'docs',
            'sphinx-polyversion',
            'docs/poly.py',
            str(tmp_path / 'site'),
        ],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert result.returncode != 0
    assert '1.0.0' in result.stdout
