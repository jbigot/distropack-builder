"""Running work over a dependency graph, where a failure stops only what depends on it."""

from collections.abc import Callable, Hashable, Mapping, Set
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import suppress
from dataclasses import dataclass
from graphlib import TopologicalSorter

from distropack_builder.docker import Cancellation, Cancelled


@dataclass(frozen=True)
class Succeeded:
    """The work ran and returned."""


@dataclass(frozen=True)
class Failed:
    """The work ran and raised error."""

    error: BaseException


@dataclass(frozen=True)
class Skipped:
    """The work did not run: because, one of the nodes it depends on, failed."""

    because: Hashable


@dataclass(frozen=True)
class Interrupted:
    """The run was cancelled before the work ended, or before it started."""


type Outcome = Succeeded | Failed | Skipped | Interrupted

# How often the waiting thread wakes up, to see a KeyboardInterrupt that no signal delivers.
_POLL = 0.1


def _ignore(*args: object) -> None:
    pass


def _descendants[T: Hashable](dependents: Mapping[T, Set[T]], node: T) -> set[T]:
    found: set[T] = set()
    pending = list(dependents.get(node, ()))
    while pending:
        dependent = pending.pop()
        if dependent not in found:
            found.add(dependent)
            pending.extend(dependents.get(dependent, ()))
    return found


def schedule[T: Hashable](
    graph: Mapping[T, Set[T]],
    work: Callable[[T], None],
    *,
    jobs: int,
    cancellation: Cancellation,
    on_start: Callable[[T], None] = _ignore,
    on_finish: Callable[[T, Outcome], None] = _ignore,
) -> dict[T, Outcome]:
    """Run work on every node of graph, each after the nodes it maps to, at most jobs at once.

    A node whose work raises fails, and the nodes that depend on it, directly or not, are skipped.
    Once cancellation is set, no work starts any more. The callbacks run in the calling thread:
    on_start before a node's work starts, on_finish once for every node, whatever its outcome.

    Returns:
        The outcome of every node.

    Raises:
        graphlib.CycleError: the graph has a cycle; no work ran.
        KeyboardInterrupt: it came while waiting; the cancellation was set, and the work in
            progress ended, before it propagated. So does any exception a callback raises.
    """
    sorter = TopologicalSorter(graph)
    sorter.prepare()
    dependents: dict[T, set[T]] = {}
    for node, dependencies in graph.items():
        for dependency in dependencies:
            dependents.setdefault(dependency, set()).add(node)
    outcomes: dict[T, Outcome] = {}

    def finish(node: T, outcome: Outcome) -> None:
        outcomes[node] = outcome
        on_finish(node, outcome)
        if isinstance(outcome, Failed | Interrupted):
            stopped = Skipped(node) if isinstance(outcome, Failed) else outcome
            for descendant in _descendants(dependents, node) - outcomes.keys():
                finish(descendant, stopped)

    with ThreadPoolExecutor(jobs) as pool:
        running: dict[Future[None], T] = {}
        try:
            while sorter.is_active():
                for node in sorter.get_ready():
                    if node in outcomes:
                        sorter.done(node)
                    elif cancellation.cancelled:
                        finish(node, Interrupted())
                        sorter.done(node)
                    else:
                        on_start(node)
                        running[pool.submit(work, node)] = node
                if not running:
                    continue
                done, _ = wait(running, timeout=_POLL, return_when=FIRST_COMPLETED)
                for future in done:
                    node = running.pop(future)
                    finish(node, _outcome(future))
                    sorter.done(node)
        except BaseException:
            # Ctrl-C, or a callback that raised: the work in progress is cancelled, not awaited
            cancellation.cancel()
            pool.shutdown(cancel_futures=True)
            # a callback that raised may raise again: the original exception is the one to tell
            with suppress(Exception):
                for future, node in running.items():
                    finish(node, _outcome(future))
                for node in graph.keys() - outcomes.keys():
                    finish(node, Interrupted())
            raise
    return outcomes


def _outcome(future: Future[None]) -> Outcome:
    """The outcome of a work that ended, or never started."""
    if future.cancelled():
        return Interrupted()
    error = future.exception()
    if error is None:
        return Succeeded()
    if isinstance(error, Cancelled):
        return Interrupted()
    return Failed(error)
