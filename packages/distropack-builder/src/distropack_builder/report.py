"""What a run tells: one line per build, each build's output kept together.

Every build logs through its own logger (build_logger), whose records the Reporter buffers per
build: they are printed with the build's outcome, so that the output of parallel builds never
interleaves. On a terminal a progress bar counts the builds done; under GitHub Actions each build
becomes one collapsed group of the job's log, and the summary a table of the job's summary page.
"""

import logging
import threading
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Self

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TaskID, TextColumn
from rich.text import Text

from distro_releases import Release
from distropack_builder.scheduler import Failed, Interrupted, Outcome, Skipped, Succeeded

# The logger every logger of the builder descends from.
ROOT = 'distropack_builder'

type Build = tuple[str, Release]


def build_logger(package: str, release: Release) -> logging.Logger:
    """The logger of the build of package for release."""
    return logging.getLogger(f'{ROOT}.build.{release.uid}.{package}')


def github_mode(environ: Mapping[str, str]) -> bool:
    """Whether the run is a GitHub Actions job, by its environment."""
    return environ.get('GITHUB_ACTIONS') == 'true'


def status(outcome: Outcome) -> str:
    """How an outcome reads, before the package and the release."""
    match outcome:
        case Succeeded():
            return 'built'
        case Failed(error):
            return f'failed: {str(error) or type(error).__name__}'
        case Skipped(because):
            return f'skipped: {because} failed'
        case Interrupted():
            return 'interrupted'


_STYLES = {Succeeded: 'green', Failed: 'red', Skipped: 'yellow', Interrupted: 'yellow'}


class _Handler(logging.Handler):
    """Hands every record to the reporter."""

    def __init__(self, reporter: 'Reporter') -> None:
        super().__init__(logging.DEBUG)
        self._reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        self._reporter._record(record)


class Reporter:
    """The report of a run, attached to the builder's loggers while used as a context manager."""

    def __init__(
        self,
        console: Console,
        *,
        verbose: bool,
        github: bool,
        summary: Path | None = None,
        total: int = 0,
    ) -> None:
        """A report to console.

        Args:
            console: where the report goes.
            verbose: whether each build's output is printed as it comes, prefixed with the build,
                rather than only for the builds that fail.
            github: whether to report as a GitHub Actions job: one group per build, all printed.
            summary: the file a GitHub Actions summary table is appended to, if any.
            total: the number of builds, for the progress bar.
        """
        self._console = console
        self._verbose = verbose
        self._github = github
        self._summary = summary
        self._total = total
        self._lock = threading.Lock()
        self._builds: dict[str, Build] = {}
        self._buffers: dict[Build, list[str]] = {}
        self._handler = _Handler(self)
        self._level = logging.NOTSET
        self._progress: Progress | None = None
        self._task: TaskID | None = None

    def __enter__(self) -> Self:
        """Attach to the builder's loggers, and show the progress bar on a terminal."""
        root = logging.getLogger(ROOT)
        self._level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self._handler)
        if self._console.is_terminal and not self._github:
            self._progress = Progress(
                TextColumn('builds'),
                BarColumn(),
                MofNCompleteColumn(),
                console=self._console,
                transient=True,
            )
            self._task = self._progress.add_task('builds', total=self._total)
            self._progress.start()
        return self

    def __exit__(
        self,
        type: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Detach from the loggers, and remove the progress bar."""
        if self._progress is not None:
            self._progress.stop()
        root = logging.getLogger(ROOT)
        root.removeHandler(self._handler)
        root.setLevel(self._level)

    def _print(self, line: str | Text) -> None:
        self._console.print(line, markup=False, highlight=False, soft_wrap=True)

    def _record(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        with self._lock:
            build = self._builds.get(record.name)
            if build is None:
                if self._verbose or record.levelno >= logging.WARNING:
                    self._print(message)
            elif self._verbose and not self._github:
                package, release = build
                self._print(f'[{package} {release.uid}] {message}')
            else:
                self._buffers[build].append(message)

    def started(self, package: str, release: Release) -> None:
        """The build of package for release starts: its logger's records are its own from now."""
        with self._lock:
            self._builds[build_logger(package, release).name] = (package, release)
            self._buffers[(package, release)] = []

    def finished(self, package: str, release: Release, outcome: Outcome) -> None:
        """The build of package for release ended with outcome, or will not run."""
        line = Text.assemble((status(outcome), _STYLES[type(outcome)]), f' {package} on {release}')
        with self._lock:
            self._builds.pop(build_logger(package, release).name, None)
            output = self._buffers.pop((package, release), [])
            if self._github:
                self._print(f'::group::{line.plain}')
                for message in output:
                    self._print(message)
                self._print('::endgroup::')
            else:
                self._print(line)
                if isinstance(outcome, Failed):
                    for message in output:
                        self._print(Text('  ' + message))
            if self._progress is not None and self._task is not None:
                self._progress.advance(self._task)

    def summarize(self, outcomes: Mapping[Build, Outcome]) -> None:
        """List the builds that did not succeed; in GitHub mode, append a table of them all."""
        builds = sorted(outcomes, key=lambda build: (build[1].uid, build[0]))
        if self._summary is not None:
            rows = ['', '| Package | Release | Outcome |', '| --- | --- | --- |']
            for package, release in builds:
                cell = status(outcomes[(package, release)]).replace('|', r'\|')
                rows.append(f'| {package} | {release} | {cell} |')
            with self._summary.open('a') as summary:
                summary.write('\n'.join(rows) + '\n')
        for package, release in builds:
            outcome = outcomes[(package, release)]
            if not isinstance(outcome, Succeeded):
                self._print(
                    Text.assemble(
                        (status(outcome), _STYLES[type(outcome)]), f' {package} on {release}'
                    )
                )
