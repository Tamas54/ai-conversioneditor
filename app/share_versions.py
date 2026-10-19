"""Változatok megosztott projektben (OMLETT 4): optimista zárolás, ütközés → „<Név> változata”, választás, szerkesztési zár.

A „minden szerkesztés új verzió” elvre épül: semmit nem írunk felül. Ha valaki egy változatot szerkeszt (vagy az
if_version-ben megadottat), de közben MÁS már újabb változatot mentett belőle, az új fájl elágazás (fork) lesz:
„<Név> változata” címkével, a napló és az eredmény `conflict`-ja megmondja, és a felhasználó választ (choose): a
választott lesz a lánc éle (a legfrissebb), a másik régebbi változatként megmarad. Valós idejű közös szerkesztés
nincs; a szerkesztési zár tanácsadó (szívverés, LOCK_STALE_S után lejár) — csak jelzi, ki dolgozik épp rajta.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from app import tenancy
from app.sharing import _has_shares, audit, enabled, log, names

LOCK_STALE_S = 90


# a közvetlen szerkesztők (MCP) és a videó-szerkesztés is új változat (a revise._version_step mellett)
_EDIT_OPS = {"video edit", "manual edit", "find_replace", "delete_section", "insert_after", "replace_heading",
             "delete_pages", "edit_with_instruction", "insert_image"}


def _is_version(op: str, fid: str, base: str) -> bool:
    from app.tools.revise import _version_step
    same_ext = fid.rsplit(".", 1)[-1].lower() == base.rsplit(".", 1)[-1].lower()
    return same_ext and bool(_version_step(op or "") or op in _EDIT_OPS)


_fork_locks: dict[str, Any] = {}


async def check_fork(fid: str, *, owner: Optional[str] = None, actor: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Optimista zárolás: ha a változat alapját (vagy az if_version-t) közben más már továbbszerkesztette, az új
    változat „<Név> változata” lesz (fork) — semmi nem vész el, a felhasználó választ (choose). → ütközés vagy None.
    Idempotens (a kérés útja és a háttér-napló is hívja; fájlonként egyszerre egy)."""
    import asyncio
    lk = _fork_locks.setdefault(fid, asyncio.Lock())
    try:
        async with lk:
            return await _check_fork(fid, owner, actor)
    finally:
        if not lk.locked():
            _fork_locks.pop(fid, None)


async def _check_fork(fid: str, owner: Optional[str], actor: Optional[str]) -> Optional[dict[str, Any]]:
    from app import db
    from app.storage import display_label, read_meta, write_meta
    sh = tenancy.shared()
    actor = actor or (sh.actor if sh is not None else tenancy.current_user_id())
    owner = owner or tenancy.workspace_id()
    if not enabled():
        return None
    meta = read_meta(fid) or {}
    if meta.get("fork_of"):
        return await conflict_for(actor, fid) if not meta.get("fork_resolved") else None
    base = meta.get("source_file_id")
    if not base or not _is_version(meta.get("operation") or "", fid, base):
        return None
    ref = sh.if_version if sh is not None and sh.if_version and sh.if_version != fid else base
    rows = await db._rows("""SELECT file_id, operation, extra->>'edited_by' AS by, extract(epoch FROM created_at) AS t
                             FROM file_versions WHERE owner_id = %s AND source_file_id = %s AND file_id <> %s
                             AND deleted_at IS NULL ORDER BY created_at""", (owner, ref, fid))
    made = float(meta.get("created_at") or time.time())
    theirs = [r for r in rows if _is_version(r["operation"] or "", r["file_id"], ref) and (r["by"] or owner) != actor
              and float(r["t"]) <= made + 1]
    if not theirs:
        return None
    nm = await names({actor, *[(r["by"] or owner) for r in theirs]})
    who = nm.get(actor, actor)
    label = display_label(fid, meta)
    stem, ext = (label.rsplit(".", 1) + [""])[:2] if "." in label else (label, fid.rsplit(".", 1)[-1])
    write_meta(fid, label=f"{stem} – {who}'s version.{ext}",
               extra={"fork_of": ref, "fork_by": actor, "fork_theirs": [r["file_id"] for r in theirs]})
    await audit(owner, actor, fid, "conflict", {"base": ref, "theirs": [r["file_id"] for r in theirs]})
    return await conflict_for(actor, fid)


async def conflict_for(actor: str, fid: str) -> Optional[dict[str, Any]]:
    """Nyitott ütközés ehhez a fájlhoz: a te változatod és a másiké, nevekkel (a választó felülethez)."""
    from app import db
    from app.storage import display_label, read_meta
    if not enabled():
        return None
    meta = read_meta(fid) or {}
    if meta.get("fork_of") and not meta.get("fork_resolved"):
        mine, theirs = fid, (meta.get("fork_theirs") or [None])[-1]
    else:
        rows = await db._rows("""SELECT file_id FROM file_versions WHERE owner_id = %s AND extra->'fork_theirs' ? %s
                                 AND NOT COALESCE((extra->>'fork_resolved')::boolean, false) AND deleted_at IS NULL
                                 ORDER BY created_at DESC LIMIT 1""", (tenancy.workspace_id(), fid))
        if not rows:
            return None
        mine, theirs = rows[0]["file_id"], fid
    if not theirs:
        return None
    mm, tm = read_meta(mine) or {}, read_meta(theirs) or {}
    owner = tenancy.workspace_id()
    nm = await names({mm.get("fork_by") or owner, tm.get("edited_by") or owner})
    a, b = nm.get(mm.get("fork_by") or owner), nm.get(tm.get("edited_by") or owner)
    return {"conflict": True, "fork": {"file_id": mine, "label": display_label(mine, mm), "by": a},
            "other": {"file_id": theirs, "label": display_label(theirs, tm), "by": b},
            "message": f"{b} saved a newer version while {a} was working on it. Both are kept — "
                       f"{a}'s is “{display_label(mine, mm)}”. Choose which one is current "
                       "(files action=\"choose\", or the choice in the file view)."}


async def choose(actor: str, fid: str) -> dict[str, Any]:
    """Ütközés után: ez a változat legyen az aktuális (a legfrissebb a láncban); a másik megmarad régebbiként."""
    from app import db
    from app.storage import read_meta, write_meta
    from app.tools.crafts import existing
    owner = tenancy.workspace_id()
    c = await conflict_for(actor, fid)
    p = existing(fid)
    os.utime(p, None)                          # a lista a legfrissebbet mutatja a lánc élén
    write_meta(fid, extra={"chosen_by": actor, "chosen_at": time.time()})
    if c:
        fork = c["fork"]["file_id"]
        write_meta(fork, extra={"fork_resolved": True})
        await db._rows("UPDATE file_versions SET extra = extra || '{\"fork_resolved\": true}'::jsonb WHERE file_id = %s",
                       (fork,))
    await audit(owner, actor, fid, "choose", {"conflict": bool(c)})
    meta = read_meta(fid) or {}
    return {"file_id": fid, "current": True, "label": meta.get("label"), "resolved": bool(c)}


# ---------- szerkesztési zár (tanácsadó, szívveréssel) ----------

def _chain_root(fid: str) -> str:
    from app.storage import read_meta
    seen, cur = set(), fid
    while cur not in seen:
        seen.add(cur)
        m = read_meta(cur) or {}
        src = m.get("source_file_id")
        if not src or not _is_version(m.get("operation") or "", cur, src):
            return cur
        cur = src
    return cur


async def lock(actor: str, fid: str) -> dict[str, Any]:
    """Szerkesztés jelzése (30 mp-enként újra): ha más tartja és él a szívverése, megmondja, ki — nem tilt."""
    from app import db
    owner = tenancy.workspace_id()
    if not await _has_shares(owner) and owner == actor:
        return {"locked": False, "shared": False}
    key = _chain_root(fid)
    rows = await db._rows(f"""INSERT INTO edit_locks (owner_id, file_id, holder_id) VALUES (%s, %s, %s)
                              ON CONFLICT (owner_id, file_id) DO UPDATE SET holder_id = EXCLUDED.holder_id, heartbeat_at = now()
                              WHERE edit_locks.holder_id = EXCLUDED.holder_id
                                 OR edit_locks.heartbeat_at < now() - interval '{LOCK_STALE_S} seconds'
                              RETURNING holder_id""", (owner, key, actor))
    if rows:
        return {"locked": True, "yours": True}
    held = await db._rows("SELECT holder_id, heartbeat_at FROM edit_locks WHERE owner_id = %s AND file_id = %s", (owner, key))
    who = (await names({held[0]["holder_id"]})).get(held[0]["holder_id"]) if held else None
    return {"locked": True, "yours": False, "by": who, "since": held[0]["heartbeat_at"] if held else None,
            "note": f"{who} is editing this right now. You can still edit — if you both save, both versions are kept "
                    "and you choose which one is current."}


async def unlock(actor: str, fid: str) -> dict[str, Any]:
    from app import db
    await db._rows("DELETE FROM edit_locks WHERE owner_id = %s AND file_id = %s AND holder_id = %s",
                   (tenancy.workspace_id(), _chain_root(fid), actor))
    return {"locked": False}


async def annotate(res: Any) -> None:
    """MCP-eredmény: ha egy új változat ütközik (közben más mentett újabbat), conflict kerül mellé."""
    if not enabled() or not isinstance(res, dict):
        return
    sh = tenancy.shared()
    if sh is None and not await _has_shares(tenancy.workspace_id()):
        return
    files = ([res] if isinstance(res.get("file_id"), str) else []) + \
        [f for f in res.get("files") or [] if isinstance(f, dict) and isinstance(f.get("file_id"), str)]
    for f in files:
        c = await check_fork(f["file_id"])
        if c:
            res["conflict"] = c
            return
