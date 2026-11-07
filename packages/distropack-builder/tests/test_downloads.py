import _thread
import gzip
import lzma
import time
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
import respx
import stamina

from distropack_builder.docker import Cancellation, Cancelled
from distropack_builder.downloads import DownloadFailed, fetch_all, orig_name, source_name

URL = 'https://orig.test/a/p-1.0.tar.gz'
OTHER = 'https://orig.test/a/q-1.0.tar.gz'


@pytest.fixture(autouse=True)
def no_wait() -> Iterator[None]:
    """Retries happen without waiting."""
    with stamina.set_testing(True, attempts=5):
        yield


def fetch(
    *urls: str, directory: Path, cancellation: Cancellation | None = None
) -> dict[str, Path | DownloadFailed]:
    return fetch_all(urls, directory, jobs=2, cancellation=cancellation or Cancellation())


@respx.mock
def test_each_url_once(tmp_path: Path) -> None:
    route = respx.get(URL).respond(200, content=b'data')
    files = fetch(URL, URL, directory=tmp_path)
    assert route.call_count == 1
    assert files == {URL: tmp_path / sha256(URL.encode()).hexdigest()}
    assert files[URL].read_bytes() == b'data'  # type: ignore[union-attr]


@respx.mock
def test_server_error_retried(tmp_path: Path) -> None:
    route = respx.get(URL)
    route.side_effect = [httpx.Response(503), httpx.Response(200, content=b'data')]
    files = fetch(URL, directory=tmp_path)
    assert route.call_count == 2
    assert isinstance(files[URL], Path)


@respx.mock
def test_timeout_retried(tmp_path: Path) -> None:
    route = respx.get(URL)
    route.side_effect = [httpx.ReadTimeout('slow'), httpx.Response(200, content=b'data')]
    files = fetch(URL, directory=tmp_path)
    assert route.call_count == 2
    assert isinstance(files[URL], Path)


@respx.mock
def test_not_found_not_retried(tmp_path: Path) -> None:
    route = respx.get(URL).respond(404)
    failure = fetch(URL, directory=tmp_path)[URL]
    assert route.call_count == 1
    assert isinstance(failure, DownloadFailed)
    assert failure.url == URL
    assert str(failure).startswith(f'while downloading {URL}: ')
    assert '404' in str(failure)


@respx.mock
def test_failure_spares_other_urls(tmp_path: Path) -> None:
    respx.get(URL).respond(500)
    respx.get(OTHER).respond(200, content=b'other')
    files = fetch(URL, OTHER, directory=tmp_path)
    assert isinstance(files[URL], DownloadFailed)
    assert files[OTHER].read_bytes() == b'other'  # type: ignore[union-attr]


@respx.mock
def test_cancelled_download_stops(tmp_path: Path) -> None:
    cancellation = Cancellation()
    sent = 0

    def body() -> Iterator[bytes]:
        nonlocal sent
        while sent < 1000:
            sent += 1
            if sent == 2:
                cancellation.cancel()
            yield b'x' * 1024

    respx.get(URL).mock(return_value=httpx.Response(200, content=body()))
    with pytest.raises(Cancelled):
        fetch(URL, directory=tmp_path, cancellation=cancellation)
    assert sent < 10


def test_source_name() -> None:
    assert source_name('https://x/a/b.tar.gz') == 'b.tar.gz'
    assert source_name('https://x/dl?id=3&file=dir/c.tar.xz') == 'c.tar.xz'


def test_orig_name(tmp_path: Path) -> None:
    gz, xz, text = tmp_path / 'gz', tmp_path / 'xz', tmp_path / 'text'
    gz.write_bytes(gzip.compress(b'data'))
    xz.write_bytes(lzma.compress(b'data'))
    text.write_text('not an archive\n')
    assert orig_name('p', '1.0', URL, gz) == 'p_1.0.orig.tar.gz'
    assert orig_name('p', '1.0', URL, xz) == 'p_1.0.orig.tar.xz'
    with pytest.raises(DownloadFailed) as failure:
        orig_name('p', '1.0', URL, text)
    assert failure.value.url == URL


@respx.mock
def test_keyboard_interrupt_cancels(tmp_path: Path) -> None:
    cancellation = Cancellation()

    def body() -> Iterator[bytes]:
        time.sleep(0.2)  # the calling thread is waiting by then
        _thread.interrupt_main()
        while True:
            yield b'x' * 1024
            time.sleep(0.01)

    respx.get(URL).mock(return_value=httpx.Response(200, content=body()))
    start = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        fetch(URL, directory=tmp_path, cancellation=cancellation)
    assert time.monotonic() - start < 2
    assert cancellation.cancelled
