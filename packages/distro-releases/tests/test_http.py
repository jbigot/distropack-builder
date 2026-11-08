# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

import importlib.metadata

import pytest

from distro_releases import _http


def test_user_agent_without_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    # Run from the sources, as distropack-builder's GitHub Action does, no metadata is installed.
    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, 'version', missing)
    with _http.new_client() as client:
        assert client.headers['User-Agent'] == 'distro-releases/unknown'
