"""The errors of distro-releases."""


class SourceError(Exception):
    """A source of release data could not be read.

    Attributes:
        url: the address that failed.
    """

    def __init__(self, url: str, reason: str) -> None:
        """An error for url, failed for reason."""
        super().__init__(f'cannot read the release data at {url}: {reason}')
        self.url = url
