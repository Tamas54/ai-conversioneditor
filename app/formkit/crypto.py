"""Érzékeny felhasználói adatok (aláírás, adatprofil) titkosítása: AES-256-GCM, felhasználónként és
célonként külön kulccsal (HKDF-SHA256 a mesterkulcsból). Mesterkulcs: DATA_ENCRYPTION_KEY, ennek híján a
FILE_SIGNING_KEY. A titkosított adat a Postgresben van; DB nélkül (fejlesztői gép) a DATA_DIR alatt."""
from __future__ import annotations

import os
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def _master() -> bytes:
    from app.config import settings
    key = os.getenv("DATA_ENCRYPTION_KEY") or settings.FILE_SIGNING_KEY
    if not key:
        raise RuntimeError("DATA_ENCRYPTION_KEY (or FILE_SIGNING_KEY) must be set to store personal data")
    return key.encode("utf-8")


def _key(uid: str, purpose: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"aice-formkit-v1",
                info=f"{purpose}:{uid}".encode("utf-8")).derive(_master())


def seal(uid: str, purpose: str, data: bytes, aad: bytes = b"") -> tuple[bytes, bytes]:
    nonce = secrets.token_bytes(12)
    return nonce, AESGCM(_key(uid, purpose)).encrypt(nonce, data, aad or purpose.encode())


def open_(uid: str, purpose: str, nonce: bytes, data: bytes, aad: bytes = b"") -> bytes:
    return AESGCM(_key(uid, purpose)).decrypt(bytes(nonce), bytes(data), aad or purpose.encode())
