"""
Encrypting a credential at rest.

Someone's Anthropic key is not their memoir, but losing it is worse than
losing a paragraph: it is spendable, it is theirs, and it grants access to an
account this software has nothing to do with. So it is encrypted with a master
key that lives in the deployment's environment and never in the database --
a stolen database file alone decrypts nothing.

AES-GCM, with the account id as associated data. That binding is the point:
a ciphertext lifted from one row and pasted into another fails to decrypt
rather than silently handing one account's credential to another.

The master key is required. There is no default and no generated fallback: a
key that changes on restart would lock everyone out of their own credential
at the worst possible moment, and a shared default is not a key at all.
"""

from __future__ import annotations

import base64
import binascii
import os

KEY_BYTES = 32
NONCE_BYTES = 12
ENV = "SARGAM_KEY_SECRET"


class VaultError(RuntimeError):
    pass


def generate() -> str:
    """A fresh master key, printable. Put it in the deployment's secrets."""
    return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode()


def _decode(raw: str) -> bytes:
    for decoder in (base64.urlsafe_b64decode, base64.b64decode,
                    binascii.unhexlify):
        try:
            key = decoder(raw)
        except Exception:
            continue
        if len(key) == KEY_BYTES:
            return key
    raise VaultError(
        f"{ENV} must be {KEY_BYTES} bytes, base64 or hex. "
        f"Generate one with: python -c "
        f"'from sargam import vault; print(vault.generate())'"
    )


def master_key() -> bytes:
    raw = os.environ.get(ENV, "")
    if not raw:
        raise VaultError(
            f"{ENV} is not set, so credentials cannot be stored. Generate one "
            f"with: python -c 'from sargam import vault; print(vault.generate())'"
        )
    return _decode(raw)


def available() -> bool:
    try:
        master_key()
    except VaultError:
        return False
    return True


def _aesgcm():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(master_key())


def seal(plaintext: str, owner: str) -> tuple[bytes, bytes]:
    """Returns (ciphertext, nonce). `owner` is bound in, not stored in the
    ciphertext, so the row cannot be replayed under a different account."""
    if not plaintext:
        raise VaultError("refusing to store an empty credential")
    nonce = os.urandom(NONCE_BYTES)
    ct = _aesgcm().encrypt(nonce, plaintext.encode(), owner.encode())
    return ct, nonce


def open_(ciphertext: bytes, nonce: bytes, owner: str) -> str:
    """Raises VaultError on any failure: a wrong master key, a tampered row,
    or a ciphertext belonging to someone else. All three are the same answer
    to the caller -- there is no credential here."""
    try:
        return _aesgcm().decrypt(bytes(nonce), bytes(ciphertext),
                                 owner.encode()).decode()
    except VaultError:
        raise
    except Exception:
        raise VaultError("could not decrypt the stored credential")


def hint(plaintext: str) -> str:
    """What may safely be shown back: enough to recognise which key it is,
    not enough to be one."""
    tail = plaintext[-4:] if len(plaintext) >= 4 else ""
    return f"…{tail}" if tail else "…"
