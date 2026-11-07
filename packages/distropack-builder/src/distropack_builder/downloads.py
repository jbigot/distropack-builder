"""The download phase: every source file of the run fetched once, before any build starts.

A failure is known before the first build: it fails the builds that need the file, and spares the
others. Transient failures (transport errors, timeouts, server errors) are retried.
"""

from collections.abc import Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, urlparse

import filetype
import httpx
import stamina

from distropack_builder.docker import Cancellation, Cancelled

ATTEMPTS = 5
# How often the waiting thread wakes up, to see a KeyboardInterrupt that no signal delivers.
_POLL = 0.1


class DownloadFailed(Exception):
    """A source file that could not be downloaded, or that is not what it should be."""

    def __init__(self, url: str, reason: str) -> None:
        """The failure of url, for reason."""
        super().__init__(f'while downloading {url}: {reason}')
        self.url = url


def new_client() -> httpx.Client:
    """The client of the downloads: 30 s timeout, redirects followed, a User-Agent of ours."""
    return httpx.Client(
        timeout=30,
        follow_redirects=True,
        headers={'User-Agent': f'distropack-builder/{version("distropack-builder")}'},
    )


def _transient(error: Exception) -> bool:
    """Whether a new attempt may succeed: a server error, or a transport error but a bad URL."""
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.is_server_error
    return isinstance(error, httpx.TransportError) and not isinstance(
        error, httpx.UnsupportedProtocol
    )


@stamina.retry(on=_transient, attempts=ATTEMPTS)
def _attempt(client: httpx.Client, url: str, path: Path, cancellation: Cancellation) -> None:
    with client.stream('GET', url) as response:
        response.raise_for_status()
        with path.open('wb') as file:
            for chunk in response.iter_bytes():
                if cancellation.cancelled:
                    raise Cancelled()
                file.write(chunk)


def _fetch(client: httpx.Client, url: str, path: Path, cancellation: Cancellation) -> Path:
    if cancellation.cancelled:
        raise Cancelled()
    try:
        _attempt(client, url, path, cancellation)
    except httpx.HTTPError as error:
        raise DownloadFailed(url, str(error)) from error
    return path


def fetch_all(
    urls: Iterable[str],
    directory: Path,
    *,
    jobs: int,
    cancellation: Cancellation,
    client: httpx.Client | None = None,
) -> dict[str, Path | DownloadFailed]:
    """Download each distinct URL once, to directory/<sha256 of the URL>, at most jobs at once.

    Returns:
        The file of each URL, or why it could not be downloaded.

    Raises:
        Cancelled: the cancellation came during the downloads.
        KeyboardInterrupt: it came while waiting; the cancellation was set before it propagated.
    """
    unique = list(dict.fromkeys(urls))
    owned = client is None
    http = client or new_client()
    try:
        with ThreadPoolExecutor(jobs) as pool:
            futures: dict[Future[Path], str] = {
                pool.submit(
                    _fetch, http, url, directory / sha256(url.encode()).hexdigest(), cancellation
                ): url
                for url in unique
            }
            try:
                pending = set(futures)
                while pending:
                    _, pending = wait(pending, timeout=_POLL, return_when=FIRST_COMPLETED)
            except KeyboardInterrupt:
                cancellation.cancel()
                raise
    finally:
        if owned:
            http.close()
    if cancellation.cancelled:
        raise Cancelled()
    results: dict[str, Path | DownloadFailed] = {}
    for future, url in futures.items():
        try:
            results[url] = future.result()
        except DownloadFailed as failure:
            results[url] = failure
    return results


def source_name(url: str) -> str:
    """The name of the file url downloads, for rpmbuild.

    That is the basename of the value of its last query parameter when it has a query, of its path
    otherwise.
    """
    parsed = urlparse(url)
    query = parse_qsl(parsed.query)
    return PurePosixPath(query[-1][1] if query else parsed.path).name


def orig_name(package: str, upstream: str, url: str, file: Path) -> str:
    """The name of the upstream tarball file, downloaded from url, in a deb source package.

    That is `<package>_<upstream>.orig.tar.<compression>`, the compression read from the file.

    Raises:
        DownloadFailed: the file is not of a type that names a compression.
    """
    kind = filetype.guess(str(file))
    if kind is None:
        raise DownloadFailed(url, 'not a compressed tarball')
    return f'{package}_{upstream}.orig.tar.{kind.extension}'
