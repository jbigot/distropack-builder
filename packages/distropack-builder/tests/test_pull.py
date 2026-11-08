# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""Pulling a missing image, as the runner does it: cancellable while the layers download."""

import itertools
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import docker
import pytest

from distropack_builder.docker import Cancellation, Cancelled, run
from distropack_builder.docker.engine import Engine


class MissingImage:
    """A client without the image, whose pull reports progress until cancelled, then forever."""

    def __init__(self, cancellation: Cancellation) -> None:
        self.created = 0
        self.pulled: list[tuple[str, str | None]] = []
        self._cancellation = cancellation
        self.containers = SimpleNamespace(create=self._create)
        self.api = SimpleNamespace(pull=self._pull)

    def _create(self, image: str, **options: Any) -> None:
        self.created += 1
        raise docker.errors.ImageNotFound(image)

    def _pull(self, repository: str, tag: str | None = None, **options: Any) -> Iterator[Any]:
        self.pulled.append((repository, tag))
        for n in itertools.count():
            if n == 3:  # Ctrl-C, while the layers download
                self._cancellation.cancel()
            yield {'status': 'Downloading', 'progressDetail': {'current': n}}


def test_pull_stops_when_cancelled() -> None:
    cancellation = Cancellation()
    client = MissingImage(cancellation)
    engine = Engine(cast(Any, client), rootless=True)
    with pytest.raises(Cancelled):
        run(engine, 'example.org/big', ['true'], cancellation=cancellation)
    assert client.pulled == [('example.org/big', 'latest')]
    assert client.created == 1  # no container after a cancelled pull
