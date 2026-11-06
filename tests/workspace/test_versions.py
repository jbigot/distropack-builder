"""Each package takes its version from its own tag family, from a checkout and from an archive."""

from pathlib import Path

import pytest

from workspace_helpers import git_archive, tag, version_of

pytestmark = pytest.mark.packaging


def test_version_ignores_the_other_family(repo: Path) -> None:
    tag(repo, 'distro-releases-v1.2.0', 'HEAD~1')
    tag(repo, 'distropack-builder-v9.0.0')  # nearer
    assert version_of(repo, 'distro-releases').startswith('1.2.1.dev1')
    assert version_of(repo, 'distropack-builder') == '9.0.0'


def test_builder_version_ignores_the_other_family(repo: Path) -> None:
    tag(repo, 'distropack-builder-v1.2.0', 'HEAD~1')
    tag(repo, 'distro-releases-v9.0.0')  # nearer
    assert version_of(repo, 'distropack-builder').startswith('1.2.1.dev1')
    assert version_of(repo, 'distro-releases') == '9.0.0'


def test_version_ignores_the_action_tags(repo: Path) -> None:
    tag(repo, 'distro-releases-v1.2.0', 'HEAD~1')
    tag(repo, 'distropack-builder-v3.4.0', 'HEAD~1')
    tag(repo, 'v9.9.9')  # nearer, the GitHub Action's family
    assert version_of(repo, 'distro-releases').startswith('1.2.1.dev1')
    assert version_of(repo, 'distropack-builder').startswith('3.4.1.dev1')


def test_version_without_tag(repo: Path) -> None:
    assert version_of(repo, 'distropack-builder').startswith('0.1.dev')


@pytest.mark.parametrize('tagged', [True, False])
def test_archive_builds_both_packages(repo: Path, tmp_path: Path, tagged: bool) -> None:
    if tagged:
        tag(repo, 'distropack-builder-v0.3.0')
    tree = git_archive(repo, tmp_path / 'tree')
    assert version_of(tree, 'distropack-builder') == ('0.3.0' if tagged else '0.0')
    assert version_of(tree, 'distro-releases') == '0.0'  # one %(describe) per archive: design §9
