"""The runner, on the engine at hand: a container's status, output, mounts and cancellation."""

import logging
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import docker
import pytest

from distropack_builder.docker import Cancellation, Cancelled, ContainerFailed, Mount, run
from distropack_builder.docker.engine import Engine
from distropack_builder.docker.runner import LABEL

pytestmark = [pytest.mark.docker, pytest.mark.enable_socket]

ALPINE = 'docker.io/library/alpine:latest'


@pytest.fixture(scope='module')
def engine() -> Engine:
    try:
        return Engine.connect()
    except Exception as error:  # no engine is a reason to skip, whatever docker-py raises
        if os.environ.get('DISTROPACK_TEST_ENGINE'):
            pytest.fail(f'no container engine, although DISTROPACK_TEST_ENGINE names one: {error}')
        pytest.skip(f'no container engine: {error}')


def ours(engine: Engine) -> list[str]:
    return [str(c.id) for c in engine.client.containers.list(all=True, filters={'label': LABEL})]


@pytest.fixture(autouse=True)
def no_container_left(engine: Engine) -> Iterator[None]:
    """After each test, no container the runner created remains, running or stopped."""
    assert ours(engine) == []
    yield
    assert ours(engine) == []


def test_exit_status_and_output(engine: Engine) -> None:
    result = run(engine, ALPINE, ['sh', '-c', 'echo out; echo err >&2; exit 3'], check=False)
    assert result.status == 3
    # The engine interleaves the two streams as it receives them.
    assert sorted(result.output.splitlines()) == ['err', 'out']
    assert result.stdout == 'out\n'


def test_failure_raises(engine: Engine) -> None:
    with pytest.raises(ContainerFailed) as error:
        run(engine, ALPINE, ['sh', '-c', 'echo bad; exit 2'])
    assert error.value.returncode == 2
    assert error.value.cmd == [ALPINE, 'sh', '-c', 'echo bad; exit 2']
    assert 'bad' in error.value.output


def test_output_streamed(engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger='distropack_builder.docker'):
        run(engine, ALPINE, ['sh', '-c', 'echo one; echo two'])
    messages = [r.getMessage() for r in caplog.records if r.name == 'distropack_builder.docker']
    assert 'one' in messages and 'two' in messages


def test_mount_written_belongs_to_invoker(engine: Engine, tmp_path: Path) -> None:
    run(engine, ALPINE, ['touch', '/data/file'], mounts=[Mount('/data', tmp_path)])
    written = tmp_path / 'file'
    assert written.stat().st_uid == os.getuid()
    written.unlink()


def test_read_only_mount(engine: Engine, tmp_path: Path) -> None:
    result = run(
        engine,
        ALPINE,
        ['touch', '/data/file'],
        mounts=[Mount('/data', tmp_path, read_only=True)],
        check=False,
    )
    assert result.status != 0
    assert not (tmp_path / 'file').exists()


def test_env_workdir_shm(engine: Engine) -> None:
    result = run(
        engine,
        ALPINE,
        ['sh', '-c', 'echo "$GREETING"; pwd; df -k /dev/shm | tail -1'],
        env={'GREETING': 'hello'},
        workdir='/tmp',
        shm_size='128m',
    )
    greeting, workdir, shm = result.stdout.splitlines()
    assert (greeting, workdir) == ('hello', '/tmp')
    assert shm.split()[1] == str(128 * 1024)


def test_cancel_within_a_second(engine: Engine) -> None:
    cancellation = Cancellation()
    outcome: list[BaseException] = []

    def target() -> None:
        try:
            run(engine, ALPINE, ['sleep', '60'], cancellation=cancellation)
        except BaseException as error:  # the thread reports whatever ends the run
            outcome.append(error)

    thread = threading.Thread(target=target)
    thread.start()
    deadline = time.monotonic() + 30
    while not engine.client.containers.list(filters={'label': LABEL, 'status': 'running'}):
        assert time.monotonic() < deadline, 'the container never started'
        time.sleep(0.1)
    start = time.monotonic()
    cancellation.cancel()
    thread.join(5)
    assert time.monotonic() - start < 1
    assert len(outcome) == 1 and isinstance(outcome[0], Cancelled)


def test_cancelled_before_start(engine: Engine) -> None:
    cancellation = Cancellation()
    cancellation.cancel()
    with pytest.raises(Cancelled):
        run(engine, ALPINE, ['true'], cancellation=cancellation)


def test_image_pulled_when_missing(engine: Engine) -> None:
    image = 'docker.io/library/busybox:1.37'
    try:
        engine.client.images.get(image)
        pytest.skip(f"{image} is already there, and is not the test's to remove")
    except docker.errors.ImageNotFound:
        pass
    try:
        assert run(engine, image, ['echo', 'pulled']).stdout == 'pulled\n'
    finally:
        engine.client.images.remove(image)
