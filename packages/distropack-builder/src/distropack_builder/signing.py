# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The signing key of a run, and every signature the builder makes with it but rpm's.

The only module that uses pysequoia: its API still moves, so it is wrapped here rather than pinned.
The secret key is unlocked once, in memory, and never written anywhere unlocked.
"""

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

import pysequoia

from distropack_builder import sos


class SigningKeyError(Exception):
    """The signing key cannot be used: unreadable, locked, or not the one asked for."""


@dataclass(frozen=True)
class Signer:
    """An unlocked signing key.

    Attributes:
        fingerprint: the fingerprint of its certificate, 40 upper-case hexadecimal digits.
        uid: the user id it signs as.
    """

    fingerprint: str
    uid: str
    _certificate: pysequoia.Cert = field(repr=False)
    _signer: pysequoia.PySigner = field(repr=False)
    _key: bytes = field(repr=False)
    _passphrase: str | None = field(repr=False)

    @classmethod
    def load(
        cls,
        keyfile: Path,
        passphrase: str | None,
        *,
        keyid: str | None = None,
        uid: str | None = None,
    ) -> Self:
        """The key of keyfile, unlocked with passphrase.

        Args:
            keyfile: one or several secret keys, binary or armored.
            passphrase: what unlocks the key, None for a key without one.
            keyid: the end of the fingerprint of the key to use, any case; the first key otherwise.
            uid: the user id to sign as, compared without surrounding spaces; the first otherwise.

        Raises:
            SigningKeyError: keyfile holds no secret key, none whose fingerprint ends with keyid,
                or the passphrase does not unlock it.
        """
        try:
            data = keyfile.read_bytes()
        except OSError as error:
            raise SigningKeyError(f'cannot read the signing key: {error}') from error
        try:
            keys = pysequoia.Tsk.split_bytes(data)
        except (ValueError, RuntimeError) as error:
            raise SigningKeyError(f'{keyfile} holds no secret key: {error}') from error
        keys = [k for k in keys if _has_secret(k)]
        if not keys:
            raise SigningKeyError(f'{keyfile} holds no secret key')
        if keyid is not None:
            keys = [k for k in keys if _fingerprint(k).endswith(keyid.upper())]
            if not keys:
                raise SigningKeyError(f'{keyfile} has no key {keyid.upper()}')
        if uid is not None:
            keys = [k for k in keys if uid.strip() in _uids(k)]
            if not keys:
                raise SigningKeyError(f'{keyfile} has no key of user id {uid.strip()}')
        key = keys[0]
        certificate = key.extract_certificate()
        uids = [uid.strip()] if uid is not None else _uids(key)
        try:
            signer = _signer(key, passphrase)
        except Exception as error:
            if passphrase is None:
                message = f'{keyfile} is protected by a passphrase, and none was given'
            else:
                message = f'the passphrase does not unlock {keyfile}'
            raise SigningKeyError(message) from error
        return cls(
            _fingerprint(key), uids[0] if uids else '', certificate, signer, data, passphrase
        )

    @property
    def keyid8(self) -> str:
        """The last 8 hexadecimal digits of the fingerprint."""
        return self.fingerprint[-8:]

    def public_key(self, *, armored: bool) -> bytes:
        """The public key, for the users of the repositories to verify them with."""
        return str(self._certificate).encode() if armored else bytes(self._certificate)

    def detached(self, data: bytes) -> bytes:
        """An armored detached signature of data (Release.gpg, repomd.xml.asc)."""
        return pysequoia.sign(self._signer, data, mode=pysequoia.SignatureMode.DETACHED)

    @contextmanager
    def gpg_secret(self, fallback: Path) -> Iterator[Path]:
        """A private directory holding the key file as given, still locked, and its passphrase.

        For gpg in the one container that signs rpms, which needs the key on disk: the directory
        is under /dev/shm, a tmpfs, when it is writable, under fallback otherwise, and it is removed
        on exit, whatever happened.
        """
        shm = Path('/dev/shm')
        base = shm if shm.is_dir() and os.access(shm, os.W_OK) else fallback
        directory = Path(tempfile.mkdtemp(prefix='distropack-sign.', dir=base))
        try:
            (directory / 'key').write_bytes(self._key)
            (directory / 'passphrase').write_text(self._passphrase or '')
            yield directory
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def clearsign(self, data: bytes) -> bytes:
        """data, clear-signed (InRelease, a .dsc).

        As RFC 9580 (7.2) requires, the trailing spaces and tabs of every line are dropped from the
        signed text: a file whose clear-signed copy must be identical to it has none.

        The line ending before the signature belongs to the signature (RFC 9580, 7.1). Signing a
        text that ends with one, sequoia keeps it as an empty line, which gpg, and apt reading an
        InRelease, would read back as an extra newline: the text is signed without its final line
        ending, which gives the form gpg itself writes, and what gpg reads back is data.
        """
        text = data.removesuffix(b'\n')
        return pysequoia.sign(self._signer, text, mode=pysequoia.SignatureMode.CLEAR)


def _signer(key: pysequoia.Tsk, passphrase: str | None) -> pysequoia.PySigner:
    """The signer of key, unlocked with passphrase.

    An EdDSA or Curve25519 key that GnuPG 2.3 or later exported may keep leading zero octets in its
    secret, which Sequoia refuses: its secrets are then decrypted here, fixed, and given to Sequoia
    unprotected, in memory only (`sos`).
    """
    try:
        return key.signer(passphrase)
    except Exception:
        fixed = None if passphrase is None else sos.unlocked_copy(bytes(key), passphrase)
        if fixed is None:
            raise
        return pysequoia.Tsk.from_bytes(fixed).signer(None)


def fingerprint(data: bytes) -> str:
    """The fingerprint of the OpenPGP certificate in data, 40 upper-case hexadecimal digits."""
    return str(pysequoia.Cert.from_bytes(data).fingerprint).upper()


def check_public_key(data: bytes) -> None:
    """Check that data, binary or armored, is an OpenPGP certificate (a public key).

    Raises:
        ValueError: it is not one; the message says why.
    """
    try:
        pysequoia.Cert.from_bytes(data)
    except (ValueError, RuntimeError) as error:
        raise ValueError(str(error)) from error


def _fingerprint(key: pysequoia.Tsk) -> str:
    return key.extract_certificate().fingerprint.upper()


def _uids(key: pysequoia.Tsk) -> list[str]:
    return [str(u).strip() for u in key.extract_certificate().user_ids]


def _has_secret(key: pysequoia.Tsk) -> bool:
    """Whether key holds secret key material: a public certificate parses as a Tsk too."""
    secret = [pysequoia.packet.Tag.SecretKey, pysequoia.packet.Tag.SecretSubkey]  # not hashable
    return any(p.tag in secret for p in pysequoia.packet.PacketPile.from_bytes(bytes(key)))
