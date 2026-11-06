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


class UnknownDistribution(LookupError):
    """No distribution has that id.

    Attributes:
        id: the id asked for.
    """

    def __init__(self, id: str, known: list[str]) -> None:
        """An error for id, which is none of known."""
        super().__init__(f'unknown distribution {id}; the distributions are {", ".join(known)}')
        self.id = id
