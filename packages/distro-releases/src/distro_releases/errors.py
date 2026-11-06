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


class SelectorError(ValueError):
    """A list of release tokens that cannot be read."""


class UnknownRelease(SelectorError):
    """A release token that names no release and no suite of its distribution.

    Attributes:
        distribution: the id of the distribution the token was given for.
        token: the token.
    """

    def __init__(self, distribution: str, token: str) -> None:
        """An error for token, given for distribution."""
        super().__init__(f'{distribution} has no release, nor suite, named {token}')
        self.distribution = distribution
        self.token = token
