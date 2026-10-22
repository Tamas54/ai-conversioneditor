"""Egyszer használatos feltöltési link az MCP-ügynöknek (10-22, robot-tanulmány v5: a 4,4 MB-os docx a Claude Code
gépén volt; MCP-n csak a base64-darabos feltöltés maradt, az ésszerűtlenül drága — a v5 nem került fel a Studióba).

files(action="upload_link") → aláírt link (30 perc, egyszer, a hívó munkaterületére) → az ügynök `curl -T fájl link`
→ a fájl a munkaterületen, a válasz a file_id + edit_url. version_of: a fájl egy meglévő fájl ÚJ VÁLTOZATA (forrása az
a fájl, a művelet „upload version”) — a verziótörténet, a files(action="compare") és a history(action="branches") így
összeköti őket.

A link állapot nélküli (HMAC a FILE_SIGNING_KEY-jel); az „egyszer” a Postgresben (upload_links_used), adatbázis nélkül
(helyi fejlesztés) a folyamat memóriájában.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Optional

from app.config import settings

TTL_S = 30 * 60
_used_local: set[str] = set()


def _sig(body: str) -> str:
    return hmac.new(settings.FILE_SIGNING_KEY.encode(), f"uplink|{body}".encode(), hashlib.sha256).hexdigest()[:40]


def make(uid: str, filename: Optional[str], version_of: Optional[str], ttl_s: int = TTL_S) -> tuple[str, int]:
    """→ (token, lejárat unix-időben)."""
    exp = int(time.time()) + ttl_s
    body = base64.urlsafe_b64encode(json.dumps(
        {"u": uid, "e": exp, "n": secrets.token_urlsafe(12), "f": filename or "", "v": version_of or ""},
        separators=(",", ":"), ensure_ascii=False).encode()).decode().rstrip("=")
    return f"{body}.{_sig(body)}", exp


def read(token: str) -> Optional[dict[str, Any]]:
    """A link adatai, ha az aláírás jó és nem járt le — különben None."""
    try:
        body, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(_sig(body), sig):
            return None
        d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or int(d.get("e") or 0) <= time.time():
        return None
    return d


async def claim(nonce: str, uid: str) -> bool:
    """Az „egyszer”: True, ha ez a link még nem volt használva (és most lefoglaltuk)."""
    from app import db
    if not db.enabled():
        if nonce in _used_local:
            return False
        _used_local.add(nonce)
        return True
    rows = await db._rows("INSERT INTO upload_links_used (nonce, owner_id) VALUES (%s, %s) ON CONFLICT DO NOTHING "
                          "RETURNING nonce", (nonce, uid))
    return bool(rows)


async def release(nonce: str) -> None:
    """Sikertelen feltöltés (túl nagy, sérült fájl): a link újra használható, amíg le nem jár."""
    from app import db
    if not db.enabled():
        _used_local.discard(nonce)
        return
    await db._rows("DELETE FROM upload_links_used WHERE nonce = %s AND file_id IS NULL", (nonce,))


async def done(nonce: str, file_id: str) -> None:
    from app import db
    if db.enabled():
        await db._rows("UPDATE upload_links_used SET file_id = %s WHERE nonce = %s", (file_id, nonce))
