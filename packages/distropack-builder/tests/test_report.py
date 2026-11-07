import re
from io import StringIO
from pathlib import Path

from rich.console import Console

from distropack_builder.report import Reporter, build_logger, github_mode
from distropack_builder.scheduler import Failed, Interrupted, Skipped, Succeeded
from support import FORKY, TRIXIE


def console() -> tuple[Console, StringIO]:
    out = StringIO()
    return Console(file=out, force_terminal=False, width=200), out


def test_logger_name() -> None:
    assert build_logger('hello', TRIXIE).name == 'distropack_builder.build.debian-13-trixie.hello'


def test_github_mode() -> None:
    assert github_mode({'GITHUB_ACTIONS': 'true'})
    assert not github_mode({})


def test_groups_do_not_interleave() -> None:
    con, out = console()
    with Reporter(con, verbose=False, github=True) as reporter:
        reporter.started('lib', TRIXIE)
        reporter.started('app', FORKY)
        for i in range(3):
            build_logger('lib', TRIXIE).debug('lib line %d', i)
            build_logger('app', FORKY).debug('app line %d', i)
        reporter.finished('app', FORKY, Failed(RuntimeError('boom')))
        reporter.finished('lib', TRIXIE, Succeeded())
    lines = out.getvalue().splitlines()
    assert lines.count('::endgroup::') == 2
    groups = re.findall(r'^::group::(.*)\n((?:(?!::).*\n)*)::endgroup::$', out.getvalue(), re.M)
    assert [g[0] for g in groups] == [
        f'failed app on {FORKY}: boom',
        f'built lib on {TRIXIE}',
    ]
    assert groups[0][1].splitlines() == ['app line 0', 'app line 1', 'app line 2']
    assert groups[1][1].splitlines() == ['lib line 0', 'lib line 1', 'lib line 2']
    # every group closes before the next opens
    depth = 0
    for line in lines:
        if line.startswith('::group::'):
            depth += 1
            assert depth == 1
        elif line == '::endgroup::':
            depth -= 1
    assert depth == 0


def test_interrupted_build_closes_its_group() -> None:
    con, out = console()
    with Reporter(con, verbose=False, github=True) as reporter:
        reporter.started('lib', TRIXIE)
        build_logger('lib', TRIXIE).debug('half way')
        reporter.finished('lib', TRIXIE, Interrupted())
    assert out.getvalue().splitlines() == [
        f'::group::interrupted lib on {TRIXIE}',
        'half way',
        '::endgroup::',
    ]


def test_summary_table(tmp_path: Path) -> None:
    con, out = console()
    summary = tmp_path / 'summary.md'
    summary.write_text('before\n')
    with Reporter(con, verbose=False, github=True, summary=summary) as reporter:
        reporter.summarize(
            {
                ('lib', TRIXIE): Failed(RuntimeError('boom')),
                ('app', TRIXIE): Skipped('lib'),
                ('lib', FORKY): Succeeded(),
            }
        )
    text = summary.read_text()
    assert text.startswith('before\n')
    assert '| Package | Release | Outcome |' in text
    rows = [
        line
        for line in text.splitlines()
        if line.startswith('| ') and line.split()[1] not in ('Package', '---')
    ]
    assert len(rows) == 3
    assert f'| lib | {TRIXIE} | failed: boom |' in rows
    assert f'| app | {TRIXIE} | skipped: lib failed |' in rows
    # the console summary lists the builds that did not succeed, and only those
    assert out.getvalue().splitlines() == [
        '2 of 3 builds did not succeed:',
        f'  skipped app on {TRIXIE}: lib failed',
        f'  failed lib on {TRIXIE}: boom',
    ]


def test_failure_output_shown() -> None:
    con, out = console()
    with Reporter(con, verbose=False, github=False) as reporter:
        reporter.started('lib', TRIXIE)
        reporter.started('app', FORKY)
        build_logger('lib', TRIXIE).debug('lib output')
        build_logger('app', FORKY).debug('app output')
        reporter.finished('lib', TRIXIE, Failed(RuntimeError('boom')))
        reporter.finished('app', FORKY, Succeeded())
    lines = out.getvalue().splitlines()
    assert lines[lines.index(f'failed lib on {TRIXIE}: boom') + 1].strip() == 'lib output'
    assert f'built app on {FORKY}' in lines
    assert 'app output' not in out.getvalue()


def test_verbose_streams_with_prefix() -> None:
    con, out = console()
    with Reporter(con, verbose=True, github=False) as reporter:
        reporter.started('lib', TRIXIE)
        build_logger('lib', TRIXIE).debug('lib output')
        assert out.getvalue().splitlines() == ['[lib debian-13-trixie] lib output']
        reporter.finished('lib', TRIXIE, Failed(RuntimeError('boom')))
    # already shown: not repeated after the status line
    assert out.getvalue().count('lib output') == 1


def test_no_live_display_off_terminal() -> None:
    con, out = console()
    with Reporter(con, verbose=False, github=False) as reporter:
        reporter.expect(2)
        reporter.started('lib', TRIXIE)
        reporter.finished('lib', TRIXIE, Succeeded())
        reporter.finished('app', TRIXIE, Skipped('lib'))
    assert '\x1b' not in out.getvalue()


def test_progress_on_a_terminal() -> None:
    out = StringIO()
    con = Console(file=out, force_terminal=True, width=100)
    with Reporter(con, verbose=False, github=False) as reporter:
        reporter.expect(2)
        reporter.started('lib', TRIXIE)
        reporter.finished('lib', TRIXIE, Succeeded())
        reporter.finished('app', TRIXIE, Skipped('lib'))
    text = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', out.getvalue())
    assert 'built lib' in text and 'skipped app' in text
