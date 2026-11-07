import _thread
import threading
import time
from collections.abc import Hashable
from graphlib import CycleError

import pytest

from distropack_builder.docker import Cancellation, Cancelled
from distropack_builder.scheduler import (
    Failed,
    Interrupted,
    Outcome,
    Skipped,
    Succeeded,
    schedule,
)


class Boom(Exception):
    pass


def test_dependencies_first() -> None:
    graph = {'a': set(), 'b': {'a'}, 'c': {'a', 'b'}, 'd': set(), 'e': {'d', 'c'}}
    started: list[str] = []
    lock = threading.Lock()

    def work(node: str) -> None:
        with lock:
            started.append(node)

    outcomes = schedule(graph, work, jobs=3, cancellation=Cancellation())
    assert outcomes == {node: Succeeded() for node in graph}
    for node, deps in graph.items():
        for dep in deps:
            assert started.index(dep) < started.index(node)


def test_failure_skips_only_descendants() -> None:
    graph = {'a': set(), 'b': set(), 'c': {'a'}, 'd': {'c'}}
    error = Boom()
    ran: list[str] = []
    finished: dict[Hashable, Outcome] = {}

    def work(node: str) -> None:
        ran.append(node)
        if node == 'a':
            raise error

    def on_finish(node: str, outcome: Outcome) -> None:
        assert node not in finished
        finished[node] = outcome

    outcomes = schedule(graph, work, jobs=1, cancellation=Cancellation(), on_finish=on_finish)
    assert outcomes == {
        'a': Failed(error),
        'b': Succeeded(),
        'c': Skipped(because='a'),
        'd': Skipped(because='a'),
    }
    assert sorted(ran) == ['a', 'b']
    assert finished == outcomes


def test_cycle_before_any_work() -> None:
    ran: list[str] = []
    with pytest.raises(CycleError):
        schedule(
            {'a': {'b'}, 'b': {'a'}, 'c': set()}, ran.append, jobs=1, cancellation=Cancellation()
        )
    assert ran == []


def test_jobs_bound() -> None:
    lock = threading.Lock()
    running = 0
    highest = 0

    def work(node: int) -> None:
        nonlocal running, highest
        with lock:
            running += 1
            highest = max(highest, running)
        time.sleep(0.05)
        with lock:
            running -= 1

    schedule({n: set() for n in range(6)}, work, jobs=2, cancellation=Cancellation())
    assert highest == 2


def test_cancel_stops_starting() -> None:
    cancellation = Cancellation()
    ran: list[int] = []

    def work(node: int) -> None:
        ran.append(node)
        cancellation.cancel()

    outcomes = schedule({n: set() for n in range(4)}, work, jobs=1, cancellation=cancellation)
    assert len(ran) == 1
    assert outcomes[ran[0]] == Succeeded()
    assert [o for n, o in outcomes.items() if n != ran[0]] == [Interrupted()] * 3


def test_cancelled_work_is_interrupted() -> None:
    def work(node: str) -> None:
        raise Cancelled()

    assert schedule({'a': set()}, work, jobs=1, cancellation=Cancellation()) == {'a': Interrupted()}


def test_keyboard_interrupt() -> None:
    cancellation = Cancellation()

    def work(node: str) -> None:
        _thread.interrupt_main()
        deadline = time.monotonic() + 5
        while not cancellation.cancelled and time.monotonic() < deadline:
            time.sleep(0.01)
        raise Cancelled()

    start = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        schedule({'a': set(), 'b': {'a'}}, work, jobs=1, cancellation=cancellation)
    assert time.monotonic() - start < 2
    assert cancellation.cancelled


def test_keyboard_interrupt_finishes_every_node() -> None:
    """The node queued behind the interrupted one is reported too, as interrupted."""
    cancellation = Cancellation()
    finished: dict[str, Outcome] = {}

    def work(node: str) -> None:
        time.sleep(0.2)  # the calling thread is waiting by then, b queued
        _thread.interrupt_main()
        while not cancellation.cancelled:
            time.sleep(0.01)
        raise Cancelled()

    with pytest.raises(KeyboardInterrupt):
        schedule(
            {'a': set(), 'b': set()},
            work,
            jobs=1,
            cancellation=cancellation,
            on_finish=finished.__setitem__,
        )
    assert finished == {'a': Interrupted(), 'b': Interrupted()}


def test_callback_error_cancels_and_drains() -> None:
    """A callback that raises cancels the run before waiting for the work in progress."""
    cancellation = Cancellation()

    def work(node: str) -> None:
        deadline = time.monotonic() + 5
        while not cancellation.cancelled and time.monotonic() < deadline:
            time.sleep(0.01)
        raise Cancelled()

    def on_start(node: str) -> None:
        if node == 'b':
            raise RuntimeError('broken pipe')

    start = time.monotonic()
    with pytest.raises(RuntimeError):
        schedule(
            {'a': set(), 'b': set()},
            work,
            jobs=2,
            cancellation=cancellation,
            on_start=on_start,
        )
    assert time.monotonic() - start < 2
    assert cancellation.cancelled
