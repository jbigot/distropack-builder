from pathlib import Path

import pysequoia
import pytest

from distropack_builder.signing import Signer, SigningKeyError
from support import FIXTURE_PASSPHRASE, FIXTURE_PROJECT

# pysequoia's API moves (Cert.secrets is deprecated already): a deprecation is a failure here.
pytestmark = pytest.mark.filterwarnings('error::DeprecationWarning')

KEY = FIXTURE_PROJECT / 'key.gpg'
FINGERPRINT = 'B1C5D1374F33A60728C275E347F88E74A1846964'
UID = 'distropack test <test@example.invalid>'
# A Release file with what clear signatures must escape or keep: a line starting with a dash, a
# line ending in spaces.
TEXT = b'Origin: x\n- dashed line\n-- x\ntrailing   \nlast\n'


@pytest.fixture(scope='module')
def signer() -> Signer:
    return Signer.load(KEY, FIXTURE_PASSPHRASE)


def certificate(signer: Signer) -> pysequoia.Cert:
    return pysequoia.Cert.from_bytes(signer.public_key(armored=True))


def test_identity(signer: Signer) -> None:
    assert signer.fingerprint == FINGERPRINT
    assert signer.keyid8 == FINGERPRINT[-8:]
    assert signer.uid == UID


def test_detached_verifies(signer: Signer) -> None:
    signature = signer.detached(TEXT)
    assert signature.startswith(b'-----BEGIN PGP SIGNATURE-----')
    cert = certificate(signer)
    result = pysequoia.verify(
        bytes=TEXT, store=lambda ids: [cert], signature=pysequoia.Sig.from_bytes(signature)
    )
    assert result.valid_sigs


def test_clearsign_verifies(signer: Signer) -> None:
    """The payload is the text, without the trailing spaces RFC 9580 drops from cleartext."""
    signed = signer.clearsign(TEXT)
    assert signed.startswith(b'-----BEGIN PGP SIGNED MESSAGE-----')
    cert = certificate(signer)
    result = pysequoia.verify(bytes=signed, store=lambda ids: [cert])
    assert result.valid_sigs
    assert result.bytes == TEXT.replace(b'trailing   \n', b'trailing\n')


def test_wrong_passphrase() -> None:
    with pytest.raises(SigningKeyError, match=f'passphrase does not unlock {KEY}'):
        Signer.load(KEY, 'not the passphrase')


def test_missing_passphrase() -> None:
    with pytest.raises(SigningKeyError, match=str(KEY)):
        Signer.load(KEY, None)


def two_keys(tmp_path: Path) -> Path:
    """The fixture key, then an unprotected one of another user."""
    other = pysequoia.Tsk.generate('Other <other@example.invalid>')
    keyring = tmp_path / 'keys.gpg'
    keyring.write_bytes(KEY.read_bytes() + str(other).encode())
    return keyring


def test_keyid_selects(tmp_path: Path) -> None:
    keyring = two_keys(tmp_path)
    assert Signer.load(keyring, FIXTURE_PASSPHRASE, keyid=FINGERPRINT[-8:].lower()).uid == UID
    other = Signer.load(keyring, None, uid='Other <other@example.invalid>')
    assert other.fingerprint != FINGERPRINT


def test_unknown_keyid(tmp_path: Path) -> None:
    with pytest.raises(SigningKeyError, match='DEADBEEF'):
        Signer.load(two_keys(tmp_path), FIXTURE_PASSPHRASE, keyid='deadbeef')


def test_uid_selects(signer: Signer) -> None:
    assert Signer.load(KEY, FIXTURE_PASSPHRASE, uid=f' {UID} ').uid == UID
    with pytest.raises(SigningKeyError, match='nobody'):
        Signer.load(KEY, FIXTURE_PASSPHRASE, uid='nobody')


@pytest.mark.parametrize('armored', [True, False])
def test_public_key_exports(signer: Signer, armored: bool) -> None:
    exported = signer.public_key(armored=armored)
    assert exported.startswith(b'-----BEGIN PGP PUBLIC KEY BLOCK-----') == armored
    assert pysequoia.Cert.from_bytes(exported).fingerprint.upper() == FINGERPRINT
    tags = [p.tag for p in pysequoia.packet.PacketPile.from_bytes(exported)]
    assert pysequoia.packet.Tag.PublicKey in tags
    assert pysequoia.packet.Tag.SecretKey not in tags
    assert pysequoia.packet.Tag.SecretSubkey not in tags


def test_public_keyfile_is_no_signing_key(signer: Signer, tmp_path: Path) -> None:
    public = tmp_path / 'public.asc'
    public.write_bytes(signer.public_key(armored=True))
    with pytest.raises(SigningKeyError, match='holds no secret key'):
        Signer.load(public, FIXTURE_PASSPHRASE)
