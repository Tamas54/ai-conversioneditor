"""File storage helpers — file_id ↔ path mapping, signed URL tokens."""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from pathlib import Path

import aiofiles

from app import tenancy
from app.config import settings

log = logging.getLogger("aice.storage")


def new_file_id(ext: str) -> str:
    """Generate a unique file_id with extension preserved."""
    return f"{uuid.uuid4().hex}.{ext.lstrip('.')}"


# Minden útvonal az aktuális felhasználó munkaterületén belül (app/tenancy.py). Megosztott fájlnál (app/sharing.py)
# a tulajdonos munkaterületén — a megosztás dönti el fájlonként, hol él (és új fájlt csak szerkesztő hozhat létre).

def _root(file_id: str) -> Path:
    s = tenancy.shared()
    return s.root_for(file_id) if s is not None else tenancy.user_root()


def input_path(file_id: str) -> Path:
    return _root(_safe(file_id)) / "inputs" / file_id


def output_path(file_id: str) -> Path:
    return _root(_safe(file_id)) / "outputs" / file_id


def file_workspace(file_id: str) -> str:
    """Kinek a munkaterületén van a fájl (az aláírt link ezt a felhasználót írja alá)."""
    s = tenancy.shared()
    return s.workspace_of(file_id) if s is not None else tenancy.current_user_id()


def temp_path(name: str | None = None) -> Path:
    return tenancy.user_root() / "temp" / (name or uuid.uuid4().hex)


def _safe(file_id: str) -> str:
    # file_id sosem tartalmazhat útvonalat (más munkaterület / ../ elérése ellen)
    if not file_id or "/" in file_id or "\\" in file_id or file_id.startswith("."):
        raise FileNotFoundError(f"Invalid file_id: {file_id!r}")
    return file_id


async def save_bytes(path: Path, data: bytes) -> None:
    async with aiofiles.open(path, "wb") as f:
        await f.write(data)


# --- Signed URL tokens (HMAC, expiry-bound) ---


def _msg(file_id: str, exp: int, uid: str) -> bytes:
    # admin: a régi formátum (a korábbi linkek érvényesek maradnak); mások: a felhasználó is aláírt
    return (f"{file_id}|{exp}" if uid == tenancy.ADMIN_ID else f"{uid}|{file_id}|{exp}").encode()


def sign_file_id(file_id: str, ttl_seconds: int | None = None, uid: str | None = None) -> tuple[str, int]:
    """Returns (token, exp_unix). HMAC-SHA256 over (uid) + file_id + exp."""
    if ttl_seconds is None:
        ttl_seconds = settings.FILE_TTL_HOURS * 3600
    exp = int(time.time()) + ttl_seconds
    msg = _msg(file_id, exp, uid or file_workspace(file_id))
    sig = hmac.new(
        settings.FILE_SIGNING_KEY.encode(), msg, hashlib.sha256
    ).hexdigest()[:32]
    return f"{exp}.{sig}", exp


def verify_file_token(file_id: str, token: str, uid: str = tenancy.ADMIN_ID) -> bool:
    try:
        exp_str, sig = token.split(".", 1)
        exp = int(exp_str)
    except (ValueError, AttributeError):
        return False
    if exp < int(time.time()):
        return False
    msg = _msg(file_id, exp, uid)
    expected = hmac.new(
        settings.FILE_SIGNING_KEY.encode(), msg, hashlib.sha256
    ).hexdigest()[:32]
    return hmac.compare_digest(expected, sig)


def public_url(file_id: str) -> str:
    """Return the full public URL for a file_id (with signed token)."""
    uid = file_workspace(file_id)
    token, _ = sign_file_id(file_id, uid=uid)
    user = "" if uid == tenancy.ADMIN_ID else f"&u={uid}"
    return f"{settings.PUBLIC_BASE_URL.rstrip('/')}/files/{file_id}?t={token}{user}"
