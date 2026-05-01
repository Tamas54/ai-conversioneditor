"""File storage helpers — file_id ↔ path mapping, signed URL tokens."""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from pathlib import Path

import aiofiles

from app.config import settings

log = logging.getLogger("aice.storage")


def new_file_id(ext: str) -> str:
    """Generate a unique file_id with extension preserved."""
    return f"{uuid.uuid4().hex}.{ext.lstrip('.')}"


def input_path(file_id: str) -> Path:
    return settings.DATA_DIR / "inputs" / file_id


def output_path(file_id: str) -> Path:
    return settings.DATA_DIR / "outputs" / file_id


def temp_path(name: str | None = None) -> Path:
    return settings.DATA_DIR / "temp" / (name or uuid.uuid4().hex)


async def save_bytes(path: Path, data: bytes) -> None:
    async with aiofiles.open(path, "wb") as f:
        await f.write(data)


# --- Signed URL tokens (HMAC, expiry-bound) ---


def sign_file_id(file_id: str, ttl_seconds: int | None = None) -> tuple[str, int]:
    """Returns (token, exp_unix). HMAC-SHA256 over file_id + exp."""
    if ttl_seconds is None:
        ttl_seconds = settings.FILE_TTL_HOURS * 3600
    exp = int(time.time()) + ttl_seconds
    msg = f"{file_id}|{exp}".encode()
    sig = hmac.new(
        settings.FILE_SIGNING_KEY.encode(), msg, hashlib.sha256
    ).hexdigest()[:32]
    return f"{exp}.{sig}", exp


def verify_file_token(file_id: str, token: str) -> bool:
    try:
        exp_str, sig = token.split(".", 1)
        exp = int(exp_str)
    except (ValueError, AttributeError):
        return False
    if exp < int(time.time()):
        return False
    msg = f"{file_id}|{exp}".encode()
    expected = hmac.new(
        settings.FILE_SIGNING_KEY.encode(), msg, hashlib.sha256
    ).hexdigest()[:32]
    return hmac.compare_digest(expected, sig)


def public_url(file_id: str) -> str:
    """Return the full public URL for a file_id (with signed token)."""
    token, _ = sign_file_id(file_id)
    return f"{settings.PUBLIC_BASE_URL.rstrip('/')}/files/{file_id}?t={token}"
