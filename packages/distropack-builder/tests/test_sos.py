# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

from pathlib import Path

import pysequoia

from distropack_builder import sos
from support import FIXTURE_PASSPHRASE, FIXTURE_PROJECT

ED25519_KEY = Path(__file__).parent / 'data' / 'ed25519-gpg.asc'


def binary(path: Path) -> bytes:
    return bytes(pysequoia.Tsk.from_bytes(path.read_bytes()))


def test_rsa_key_left_alone() -> None:
    assert sos.unlocked_copy(binary(FIXTURE_PROJECT / 'key.gpg'), FIXTURE_PASSPHRASE) is None


def test_ed25519_copy_is_the_same_key_unprotected() -> None:
    fixed = sos.unlocked_copy(binary(ED25519_KEY), 'test-passphrase')
    assert fixed is not None
    original = pysequoia.Tsk.from_bytes(ED25519_KEY.read_bytes())
    copy = pysequoia.Tsk.from_bytes(fixed)
    assert copy.extract_certificate().fingerprint == original.extract_certificate().fingerprint
    copy.signer(None)  # unprotected, and Sequoia reads its secret


def test_wrong_passphrase_gives_nothing() -> None:
    assert sos.unlocked_copy(binary(ED25519_KEY), 'not the passphrase') is None
