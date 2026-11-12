# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The release tool: pending changes in a channel's directory, and the release cut from them."""

import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from debian.changelog import Changelog

from distropack_builder import config, release
from distropack_builder.release import ReleaseError, UnportedCommits
from git_history import git

FIXTURE = Path(__file__).parents[3] / 'tests' / 'e2e' / 'project'
TODAY = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
LATER = datetime(2026, 12, 1, 9, 30, tzinfo=UTC)
AUTHOR = 'Rel Ease <release@example.invalid>'
APP_SOURCE = 'https://github.com/example/hello-app/archive/%{version}/hello-app-%{version}.tar.gz'


def write_conf(root: Path, *, snapshots: tuple[str, ...] = ('weekly',), **extra: object) -> None:
    conf = {
        'channels': {'stable': {'kind': 'release'}}
        | {name: {'kind': 'snapshot'} for name in snapshots},
        'release': {'author': AUTHOR, 'packages': ['hello-*']},
        'distribs': {},
        'packages': {
            'hello-lib': {},
            'hello-app': {
                'channels': {
                    name: {'git': 'file:///nowhere.git', 'ref': 'main', 'verify': 'none'}
                    for name in snapshots
                }
            },
        },
    } | extra
    (root / 'build.conf').write_text(yaml.safe_dump(conf))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A packaging repository: hello-lib, and hello-app as a quilt source in channel weekly."""
    root = tmp_path / 'packaging'
    root.mkdir()
    shutil.copytree(FIXTURE / 'hello-lib', root / 'hello-lib')
    spec_in = root / 'hello-lib' / 'hello-lib.spec.in'
    spec_in.with_suffix('').write_text(spec_in.read_text().replace('@ORIG_BASE@', 'https://o.test'))
    spec_in.unlink()
    app = root / 'hello-app'
    shutil.copytree(FIXTURE / 'hello-app', app)
    (app / 'debian' / 'source' / 'format').write_text('3.0 (quilt)\n')
    shutil.copy(root / 'hello-lib' / 'debian' / 'watch', app / 'debian' / 'watch')
    changelog = app / 'debian' / 'changelog'
    changelog.write_text(changelog.read_text().replace('hello-app (1.0)', 'hello-app (1.0-1)', 1))
    spec = app / 'hello-app.spec'
    spec.write_text(
        spec.read_text().replace('BuildArch:', f'Source0:        {APP_SOURCE}\nBuildArch:')
    )
    write_conf(root)
    git(root, 'init', '-q', '-b', 'main')
    commit_all(root, 'packaging')
    return root


def commit_all(root: Path, message: str) -> None:
    git(root, 'add', '-A')
    git(root, 'commit', '-q', '-m', message)


def status(root: Path) -> str:
    return git(root, 'status', '--porcelain')


def load(root: Path) -> config.BuildConfig:
    return config.load(root / 'build.conf')


def test_pending_creates_directory_and_entry(repo: Path) -> None:
    release.pending(load(repo), 'weekly', ['hello-app'], TODAY)
    weekly = repo / 'hello-app-weekly'
    text = (weekly / 'debian' / 'changelog').read_text()
    assert text.splitlines()[0] == 'hello-app (1.0-1) UNRELEASED; urgency=medium'
    parsed = Changelog(text)
    assert [line.strip() for line in parsed[0].changes() if line.strip()] == [
        '* Pending changes for the next release'
    ]
    assert parsed[0].author == AUTHOR
    assert parsed[0].date == 'Thu, 12 Nov 2026 10:00:00 +0000'
    spec = (weekly / 'hello-app.spec').read_text()
    assert (
        '%changelog\n* Thu Nov 12 2026 Rel Ease <release@example.invalid> - 1.0-1%{?dist}\n'
        '- Pending changes for the next release\n\n* Thu Nov 05 2026'
    ) in spec
    assert (repo / 'hello-app' / 'debian' / 'changelog').read_text() == (
        (FIXTURE / 'hello-app' / 'debian' / 'changelog')
        .read_text()
        .replace('hello-app (1.0)', 'hello-app (1.0-1)', 1)
    )


@pytest.mark.parametrize(
    ('channel', 'names', 'message'),
    [
        pytest.param('nope', ['hello-app'], 'unknown channel nope', id='unknown-channel'),
        pytest.param('stable', ['hello-app'], 'release channel', id='release-channel'),
        pytest.param('weekly', ['hello-lib'], 'hello-lib', id='undeclared'),
    ],
)
def test_pending_refuses(repo: Path, channel: str, names: list[str], message: str) -> None:
    with pytest.raises(ReleaseError, match=message):
        release.pending(load(repo), channel, names, TODAY)
    assert status(repo) == ''


def test_pending_refuses_twice(repo: Path) -> None:
    release.pending(load(repo), 'weekly', ['hello-app'], TODAY)
    commit_all(repo, 'pending')
    with pytest.raises(ReleaseError, match='hello-app-weekly'):
        release.pending(load(repo), 'weekly', ['hello-app'], TODAY)


def test_pending_refuses_a_dirty_tree(repo: Path) -> None:
    (repo / 'stray').write_text('x')
    with pytest.raises(ReleaseError, match='uncommitted'):
        release.pending(load(repo), 'weekly', ['hello-app'], TODAY)
    assert not (repo / 'hello-app-weekly').exists()


def test_pending_needs_an_author(repo: Path) -> None:
    write_conf(repo, release={'packages': ['hello-*']})
    commit_all(repo, 'no author')
    with pytest.raises(ReleaseError, match='release.author is not set'):
        release.pending(load(repo), 'weekly', ['hello-app'], TODAY)


def pending_with_change(repo: Path) -> None:
    release.pending(load(repo), 'weekly', ['hello-app'], TODAY)
    control = repo / 'hello-app-weekly' / 'debian' / 'control'
    control.write_text(control.read_text().replace('hello-lib', 'hello-lib, libnew-dev', 1))
    commit_all(repo, 'pending changes')


def test_release_ports_pending(repo: Path) -> None:
    pending_with_change(repo)
    release.release(load(repo), '1.2.0', from_channel=None, ported=False, now=LATER)
    assert not (repo / 'hello-app-weekly').exists()
    assert 'libnew-dev' in (repo / 'hello-app' / 'debian' / 'control').read_text()
    text = (repo / 'hello-app' / 'debian' / 'changelog').read_text()
    lines = text.splitlines()
    assert lines[0] == 'hello-app (1.2.0-1) unstable; urgency=medium'
    assert lines[2] == '  * Upstream release 1.2.0'
    assert lines[3] == '  * Pending changes for the next release'
    parsed = Changelog(text)
    assert str(parsed.version) == '1.2.0-1'
    assert parsed[0].author == AUTHOR
    assert parsed[0].date == 'Tue, 01 Dec 2026 09:30:00 +0000'
    assert len(parsed) == 2  # the pending entry became the release's
    spec = (repo / 'hello-app' / 'hello-app.spec').read_text()
    assert re.search(r'^Version:\s+1\.2\.0$', spec, re.M)
    assert re.search(r'^Release:\s+1%\{\?dist\}$', spec, re.M)
    assert (
        '%changelog\n* Tue Dec 01 2026 Rel Ease <release@example.invalid> - 1.2.0-1%{?dist}\n'
        '- Upstream release 1.2.0\n- Pending changes for the next release\n\n* Thu Nov 05 2026'
    ) in spec
    # hello-lib had no pending changes: a new entry
    lib = Changelog((repo / 'hello-lib' / 'debian' / 'changelog').read_text())
    assert str(lib.version) == '1.2.0-1'
    assert [line.strip() for line in lib[0].changes() if line.strip()] == [
        '* Upstream release 1.2.0'
    ]
    assert len(lib) == 2
    assert yaml.safe_load((repo / 'build.conf').read_text())['packages']['hello-lib'] == {}


def test_release_refuses_unported(repo: Path) -> None:
    pending_with_change(repo)
    rules = repo / 'hello-app' / 'debian' / 'rules'
    rules.write_text(rules.read_text() + '\n# a fix on the release branch\n')
    commit_all(repo, 'fix the rules')
    with pytest.raises(UnportedCommits) as caught:
        release.release(load(repo), '1.2.0', from_channel=None, ported=False, now=LATER)
    message = str(caught.value)
    assert 'fix the rules' in message
    assert 'port these into hello-app-weekly/ and rerun with --ported' in message
    assert status(repo) == ''
    assert (repo / 'hello-app-weekly').is_dir()
    release.release(load(repo), '1.2.0', from_channel=None, ported=True, now=LATER)
    assert not (repo / 'hello-app-weekly').exists()


def test_release_candidate_versions(repo: Path) -> None:
    release.release(load(repo), '1.3.0-rc1', from_channel=None, ported=False, now=LATER)
    lib = Changelog((repo / 'hello-lib' / 'debian' / 'changelog').read_text())
    assert str(lib.version) == '1.3.0~rc1-1'
    spec = (repo / 'hello-app' / 'hello-app.spec').read_text()
    assert re.search(r'^Version:\s+1\.3\.0~rc1$', spec, re.M)
    (source0,) = re.findall(r'^Source0:\s*(\S+)$', spec, re.M)
    assert source0 == (
        'https://github.com/example/hello-app/archive/1.3.0-rc1/hello-app-1.3.0-rc1.tar.gz'
    )


def test_release_final_version_keeps_the_macro(repo: Path) -> None:
    release.release(load(repo), '1.2.0', from_channel=None, ported=False, now=LATER)
    spec = (repo / 'hello-app' / 'hello-app.spec').read_text()
    (source0,) = re.findall(r'^Source0:\s*(\S+)$', spec, re.M)
    assert source0 == APP_SOURCE


def test_release_two_channels_need_from(repo: Path) -> None:
    write_conf(repo, snapshots=('weekly', 'nightly'))
    commit_all(repo, 'two channels')
    release.pending(load(repo), 'weekly', ['hello-app'], TODAY)
    commit_all(repo, 'pending in weekly')
    release.pending(load(repo), 'nightly', ['hello-app'], TODAY)
    commit_all(repo, 'pending in nightly')
    with pytest.raises(ReleaseError) as caught:
        release.release(load(repo), '1.2.0', from_channel=None, ported=False, now=LATER)
    assert 'weekly' in str(caught.value) and 'nightly' in str(caught.value)
    assert status(repo) == ''
    release.release(load(repo), '1.2.0', from_channel='weekly', ported=False, now=LATER)
    assert not (repo / 'hello-app-weekly').exists()


def test_release_refuses_an_unreleased_channel_directory(repo: Path) -> None:
    pending_with_change(repo)
    write_conf(repo, release={'author': AUTHOR, 'packages': ['hello-lib']})
    commit_all(repo, 'release hello-lib only')
    with pytest.raises(ReleaseError, match='hello-app-weekly'):
        release.release(load(repo), '1.2.0', from_channel=None, ported=False, now=LATER)
    assert status(repo) == ''


def test_autorelease_spec_untouched(repo: Path) -> None:
    spec = repo / 'hello-lib' / 'hello-lib.spec'
    text = spec.read_text()
    text = re.sub(r'^Release:.*$', 'Release:        %autorelease', text, flags=re.M)
    text = text[: text.index('%changelog')] + '%changelog\n%autochangelog\n'
    spec.write_text(text)
    commit_all(repo, 'autorelease')
    release.release(load(repo), '1.2.0', from_channel=None, ported=False, now=LATER)
    written = spec.read_text()
    assert re.search(r'^Release:\s+%autorelease$', written, re.M)
    assert written.endswith('%changelog\n%autochangelog\n')
    assert re.search(r'^Version:\s+1\.2\.0$', written, re.M)


@pytest.mark.docker
def test_released_spec_parses(repo: Path) -> None:
    pending_with_change(repo)
    release.release(load(repo), '1.3.0-rc1', from_channel=None, ported=False, now=LATER)
    image = 'ghcr.io/jbigot/distropack-builder/fedora_builder:44-p4'
    result = subprocess.run(
        [
            'docker',
            'run',
            '--rm',
            '-v',
            f'{repo / "hello-app"}:/spec:ro',
            '--entrypoint',
            'rpmspec',
            image,
            '-P',
            '/spec/hello-app.spec',
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert 'Version:        1.3.0~rc1' in result.stdout


def test_release_native_package_has_no_revision(repo: Path) -> None:
    shutil.copytree(FIXTURE / 'hello-app', repo / 'hello-native')
    for path in (repo / 'hello-native').rglob('*'):
        if path.is_file():
            path.write_text(path.read_text().replace('hello-app', 'hello-native'))
    (repo / 'hello-native' / 'hello-app.spec').unlink()
    conf = yaml.safe_load((repo / 'build.conf').read_text())
    conf['packages']['hello-native'] = {}
    (repo / 'build.conf').write_text(yaml.safe_dump(conf))
    commit_all(repo, 'a native package')
    release.release(load(repo), '1.2.0', from_channel=None, ported=False, now=LATER)
    native = Changelog((repo / 'hello-native' / 'debian' / 'changelog').read_text())
    assert str(native.version) == '1.2.0'


def test_second_release_does_not_list_the_first(repo: Path) -> None:
    pending_with_change(repo)
    release.release(load(repo), '1.2.0', from_channel=None, ported=False, now=LATER)
    commit_all(repo, 'release 1.2.0')
    pending_with_change(repo)
    release.release(load(repo), '1.3.0', from_channel=None, ported=False, now=LATER)
    assert not (repo / 'hello-app-weekly').exists()
