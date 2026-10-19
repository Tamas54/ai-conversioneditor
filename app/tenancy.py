"""Munkaterületek: kérésenként „aktuális felhasználó” (contextvar), és ebből a tárhely gyökere.

    admin            → settings.DATA_DIR (a régi, közös gyökér — a meglévő fájlok így a helyükön maradnak)
    bárki más (uid)  → settings.DATA_DIR / "u" / uid

A storage/files.py minden útvonala (input/output/temp) ezen át megy, így egy felhasználó fizikailag sem
éri el a másik fájljait — a modulok egyenként nem ellenőriznek tulajdonjogot. Az auth-middleware állítja
be (süti vagy MCP-kulcs alapján); háttérfeladatok a létrehozó kérés kontextusát öröklik.

Megosztás (OMLETT 4, app/sharing.py): egy kérés idejére a munkaterület egy MÁSIK felhasználóé lehet (aki megosztotta
a fájlt) — a fájlok ott élnek (workspace_id, user_root), a művelet gazdája (kredit, napló, jobok) viszont a belépett
felhasználó marad (current_user_id). Ilyenkor a fájl-útvonalakat a megosztás-objektum adja (shared().root_for).
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from pathlib import Path

from app.config import settings

ADMIN_ID = "admin"
_UID_RE = re.compile(r"^[a-z0-9]{6,40}$")
_current: ContextVar[str] = ContextVar("aice_user", default=ADMIN_ID)
# MCP-n a kliens (ChatGPT, Claude…) maga generál képet a SAJÁT képeszközével: a szerver FLUX-a ilyenkor nem fut,
# a rendszer tokenjeit nem égetjük (Kommandant, 10-12). Az MCP-burkoló állítja be (app/mcp_server.py: linked).
client_makes_images: ContextVar[bool] = ContextVar("aice_client_images", default=False)
CLIENT_IMAGE_MSG = ("This server doesn't generate AI pictures for connected AIs. Make the picture with your own image "
                    "tool, then pass its public URL (image_url, or {\"url\": …} in a spec) or upload it (upload_from_url / "
                    "upload_file) and use the file_id. Free stock photos: {\"stock\": \"words\"} or generate_image(stock_search=…).")
_ready: set[Path] = set()
# a kérés idejére megnyitott megosztott munkaterület (app/sharing.Shared) — None: a saját
_shared: ContextVar[object] = ContextVar("aice_shared", default=None)


def current_user_id() -> str:
    """A művelet gazdája (aki belépett): kredit, keret, jobok, saját beállítások."""
    return _current.get()


def workspace_id() -> str:
    """Akinek a tárhelyén a fájlok élnek: megosztott fájlnál a tulajdonos, különben a belépett felhasználó."""
    s = _shared.get()
    return s.owner if s is not None else _current.get()


def shared():
    return _shared.get()


def enter_shared(s):
    return _shared.set(s)


def leave_shared(token) -> None:
    _shared.reset(token)


def set_user(uid: str):
    if uid != ADMIN_ID and not _UID_RE.match(uid):
        raise ValueError(f"Invalid user id: {uid!r}")
    return _current.set(uid)


def reset(token) -> None:
    _current.reset(token)


def user_root(uid: str | None = None) -> Path:
    uid = uid or workspace_id()
    root = settings.DATA_DIR if uid == ADMIN_ID else settings.DATA_DIR / "u" / uid
    if root not in _ready:
        for sub in ("inputs", "outputs", "temp"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        _ready.add(root)
    return root


GUEST_PREFIX = "g"          # a regisztrált azonosító hex (0-9a-f), így a „g…” sosem ütközik


def new_guest_id() -> str:
    import secrets
    return GUEST_PREFIX + secrets.token_hex(8)


def is_guest(uid: str | None = None) -> bool:
    uid = uid or _current.get()
    return uid.startswith(GUEST_PREFIX) and uid != ADMIN_ID


def migrate(src_uid: str, dst_uid: str) -> int:
    """Vendég → fiók: a vendég fájljai (inputs/outputs, meta-fájlokkal) átkerülnek a fiók tárhelyére."""
    import shutil
    if not is_guest(src_uid) or src_uid == dst_uid:
        return 0
    src, dst = settings.DATA_DIR / "u" / src_uid, user_root(dst_uid)
    moved = 0
    for sub in ("inputs", "outputs"):
        for f in (src / sub).glob("*") if (src / sub).is_dir() else []:
            target = dst / sub / f.name
            if f.is_file() and not target.exists():
                shutil.move(str(f), str(target))
                moved += 1
    shutil.rmtree(src, ignore_errors=True)
    _ready.discard(src)
    return moved


def all_roots() -> list[Path]:
    """Minden munkaterület gyökere (takarításhoz)."""
    roots = [settings.DATA_DIR]
    base = settings.DATA_DIR / "u"
    if base.is_dir():
        roots += [p for p in base.iterdir() if p.is_dir()]
    return roots
