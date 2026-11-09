# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

"""The secret ECC keys GnuPG 2.3 and later export, made readable by Sequoia.

GnuPG writes the secret scalar of an EdDSA or Curve25519 ECDH key as a fixed-size SOS, keeping
its leading zero octets; Sequoia reads it as an MPI and refuses one with leading zeros as
malformed (sequoia-pgp/sequoia#1194, #1024). The zeros are inside the passphrase-protected part,
so this module decrypts it with the passphrase (OpenPGP v4 secret key packets, RFC 9580 5.5.3 and
3.7), strips the zeros, and gives back the key with that secret unprotected, for Sequoia to read
in memory: nothing it returns is written anywhere.
"""

import hashlib
from collections.abc import Iterator

from cryptography.hazmat.decrepit.ciphers.modes import CFB
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

_SECRET_TAGS = (5, 7)  # secret key, secret subkey
_ECDH, _EDDSA = 18, 22
# symmetric algorithm id: key size in octets (AES only; the IV is its 16-octet block)
_AES = {7: 16, 8: 24, 9: 32}
_HASHES = {
    1: 'md5',
    2: 'sha1',
    3: 'ripemd160',
    8: 'sha256',
    9: 'sha384',
    10: 'sha512',
    11: 'sha224',
}


class _Unsupported(Exception):
    """A packet this module does not rewrite: left as it is."""


class _WrongPassphrase(Exception):
    """The passphrase does not decrypt a secret: its SHA-1 check fails."""


def unlocked_copy(data: bytes, passphrase: str) -> bytes | None:
    """data, binary OpenPGP packets, with each protected ECC secret decrypted and its SOS fixed.

    Returns:
        The rewritten packets, or None when no packet needed it, or the passphrase did not decrypt
        one (its SHA-1 check failed), so that the caller reports the original error.
    """
    out = bytearray()
    changed = False
    for tag, body in _packets(data):
        if tag in _SECRET_TAGS:
            try:
                fixed = _unlock(body, passphrase.encode())
            except _Unsupported:
                fixed = None
            except _WrongPassphrase:
                return None
            if fixed is not None:
                body = fixed
                changed = True
        out += _header(tag, len(body)) + body
    return bytes(out) if changed else None


def _packets(data: bytes) -> Iterator[tuple[int, bytes]]:
    """The (tag, body) of each packet, in old or new format, without partial lengths."""
    position = 0
    while position < len(data):
        ctb = data[position]
        if not ctb & 0x80:
            raise ValueError(f'not an OpenPGP packet at offset {position}')
        if ctb & 0x40:  # new format
            tag = ctb & 0x3F
            first = data[position + 1]
            if first < 192:
                length, header = first, 2
            elif first < 224:
                length, header = ((first - 192) << 8) + data[position + 2] + 192, 3
            elif first == 255:
                length, header = int.from_bytes(data[position + 2 : position + 6]), 6
            else:
                raise ValueError('partial body lengths do not occur in keys')
        else:
            tag = (ctb >> 2) & 0x0F
            size = (1, 2, 4)[ctb & 3] if ctb & 3 != 3 else 0
            if not size:
                raise ValueError('indeterminate lengths do not occur in keys')
            length, header = int.from_bytes(data[position + 1 : position + 1 + size]), 1 + size
        body = data[position + header : position + header + length]
        yield tag, body
        position += header + length


def _header(tag: int, length: int) -> bytes:
    """A new-format packet header."""
    if length < 192:
        return bytes((0xC0 | tag, length))
    if length < 8384:
        length -= 192
        return bytes((0xC0 | tag, (length >> 8) + 192, length & 0xFF))
    return bytes((0xC0 | tag, 255)) + length.to_bytes(4)


def _mpi(data: bytes, position: int) -> int:
    """The position after the MPI or SOS at position."""
    bits = int.from_bytes(data[position : position + 2])
    return position + 2 + (bits + 7) // 8


def _unlock(body: bytes, passphrase: bytes) -> bytes | None:
    """The secret key packet body with its ECC secret decrypted and fixed, None if not needed.

    Raises:
        _Unsupported: not a v4 ECC key protected in a way this module knows.
        _WrongPassphrase: the passphrase does not decrypt it.
    """
    if body[0] != 4 or body[5] not in (_ECDH, _EDDSA):
        return None
    position = 6
    position += 1 + body[position]  # the curve's OID
    position = _mpi(body, position)  # the public point
    if body[5] == _ECDH:
        position += 1 + body[position]  # the KDF parameters
    public, usage = body[:position], body[position]
    position += 1
    if usage == 0:
        secret, check = body[position:-2], None
        key = iv = b''
    elif usage == 254:
        cipher = body[position]
        if cipher not in _AES:
            raise _Unsupported(f'cipher {cipher}')
        key, position = _s2k(body, position + 1, passphrase, _AES[cipher])
        iv, encrypted = body[position : position + 16], body[position + 16 :]
        decryptor = Cipher(algorithms.AES(key), CFB(iv)).decryptor()
        plain = decryptor.update(encrypted) + decryptor.finalize()
        secret, check = plain[:-20], plain[-20:]
        if hashlib.sha1(secret).digest() != check:  # noqa: S324 (the format's own check)
            raise _WrongPassphrase()
    else:
        raise _Unsupported(f'S2K usage {usage}')
    bits = int.from_bytes(secret[:2])
    value = secret[2 : 2 + (bits + 7) // 8]
    stripped = value.lstrip(b'\0')
    if usage == 0 and stripped == value:
        return None
    bits = (len(stripped) - 1) * 8 + stripped[0].bit_length() if stripped else 0
    mpi = bits.to_bytes(2) + stripped
    checksum = (sum(mpi) & 0xFFFF).to_bytes(2)
    return public + b'\0' + mpi + checksum


def _s2k(body: bytes, position: int, passphrase: bytes, size: int) -> tuple[bytes, int]:
    """The key an S2K specifier at position derives from passphrase, and the position after it."""
    kind, hash_id = body[position], body[position + 1]
    if hash_id not in _HASHES:
        raise _Unsupported(f'hash {hash_id}')
    position += 2
    salt = b''
    count = 0
    if kind in (1, 3):
        salt, position = body[position : position + 8], position + 8
    if kind == 3:
        coded = body[position]
        count, position = (16 + (coded & 15)) << ((coded >> 4) + 6), position + 1
    elif kind != 0 and kind != 1:
        raise _Unsupported(f'S2K type {kind}')
    material = salt + passphrase
    total = max(count, len(material))
    key = b''
    preload = 0
    while len(key) < size:
        digest = hashlib.new(_HASHES[hash_id])
        digest.update(b'\0' * preload)
        full, rest = divmod(total, len(material))
        digest.update(material * full + material[:rest])
        key += digest.digest()
        preload += 1
    return key[:size], position
