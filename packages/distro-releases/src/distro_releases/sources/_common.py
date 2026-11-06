"""What the sources share."""


def cpe(prefix: str | None, version: str | None) -> str | None:
    """The CPE of a release, from its distribution's prefix, if both exist."""
    return None if prefix is None or version is None else f'{prefix}:{version}'
