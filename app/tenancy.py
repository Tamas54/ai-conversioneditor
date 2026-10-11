"""Munkaterületek: kérésenként „aktuális felhasználó” (contextvar), és ebből a tárhely gyökere.

    admin            → settings.DATA_DIR (a régi, közös gyökér — a meglévő fájlok így a helyükön maradnak)
    bárki más (uid)  → settings.DATA_DIR / "u" / uid

A storage/files.py minden útvonala (input/output/temp) ezen át megy, így egy felhasználó fizikailag sem
éri el a másik fájljait — a modulok egyenként nem ellenőriznek tulajdonjogot. Az auth-middleware állítja
be (süti vagy MCP-kulcs alapján); háttérfeladatok a létrehozó kérés kontextusát öröklik.
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from pathlib import Path

from app.config import settings

ADMIN_ID = "admin"
_UID_RE = re.compile(r"^[a-z0-9]{6,40}$")
_current: ContextVar[str] = ContextVar("aice_user", default=ADMIN_ID)
_ready: set[Path] = set()


def current_user_id() -> str:
    return _current.get()


def set_user(uid: str):
    if uid != ADMIN_ID and not _UID_RE.match(uid):
        raise ValueError(f"érvénytelen felhasználó-azonosító: {uid!r}")
    return _current.set(uid)


def reset(token) -> None:
    _current.reset(token)


def user_root(uid: str | None = None) -> Path:
    uid = uid or _current.get()
    root = settings.DATA_DIR if uid == ADMIN_ID else settings.DATA_DIR / "u" / uid
    if root not in _ready:
        for sub in ("inputs", "outputs", "temp"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        _ready.add(root)
    return root


def all_roots() -> list[Path]:
    """Minden munkaterület gyökere (takarításhoz)."""
    roots = [settings.DATA_DIR]
    base = settings.DATA_DIR / "u"
    if base.is_dir():
        roots += [p for p in base.iterdir() if p.is_dir()]
    return roots
