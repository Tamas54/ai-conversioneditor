"""Cleanup loop — runs every CLEANUP_INTERVAL_MIN, deletes files older than TTL."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from app.config import settings

log = logging.getLogger("aice.cleanup")


async def cleanup_loop():
    interval = settings.CLEANUP_INTERVAL_MIN * 60
    ttl = settings.FILE_RETENTION_DAYS * 86400  # megőrzés (a link-TTL külön: FILE_TTL_HOURS)
    while True:
        try:
            from app import tenancy
            removed = 0
            for root in tenancy.all_roots():
                guest = root.name.startswith(tenancy.GUEST_PREFIX) and root.parent.name == "u"
                removed += _sweep(root, 86400 if guest else ttl, await _shared_keep(root))   # vendég: 1 nap
            if removed:
                log.info("Cleanup: removed %d files", removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Cleanup pass failed")
        await asyncio.sleep(interval)


_KEEP_FILE = ".shared-keep.json"           # a munkaterület gyökerében (a takarítás nem nézi): az utolsó ismert állapot
_KEEP_MAX_AGE_S = 7 * 86400


async def _shared_keep(root: Path) -> set[str]:
    """Megosztott projekt (és családja) nem jár le a megőrzéssel (app/sharing.py) — a munkaterület ezen fájljai
    maradnak. Minden sikeres kör elmenti a listát a munkaterület gyökerébe; ha a Postgres épp nem válaszol, az
    utolsó ismert (legfeljebb 7 napos) lista védi a megosztottakat, a többi fájl lejárhat. Ha ilyen sincs: ebben a
    körben ezt a munkaterületet kihagyjuk (hangos hibanapló) — inkább késik egy kört a takarítás, mint hogy egy
    élő megosztás fájljai eltűnjenek."""
    import json
    from app import sharing
    from app.config import settings
    if not sharing.enabled():
        return set()
    uid = "admin" if root == settings.DATA_DIR else root.name
    snap = root / _KEEP_FILE
    try:
        keep = await sharing.shared_family(uid)
    except Exception as e:
        try:
            age = time.time() - snap.stat().st_mtime
            if age <= _KEEP_MAX_AGE_S:
                keep = set(json.loads(snap.read_text("utf-8")))
                log.error("megosztott fájlok listája nem olvasható (%s: %s) — az utolsó ismert lista véd (%d fájl, %.0f órás)",
                          uid, e, len(keep), age / 3600)
                return keep
        except (OSError, ValueError):
            pass
        log.error("megosztott fájlok listája nem olvasható (%s: %s), és nincs friss mentett lista — ebben a körben "
                  "ennek a munkaterületnek a takarítása kimarad", uid, e)
        return {"*"}
    if not keep and not snap.exists():
        return keep                           # nincs megosztás és nem is volt: nincs mit menteni
    try:
        tmp = snap.with_name(snap.name + ".tmp")
        tmp.write_text(json.dumps(sorted(keep)), "utf-8")
        tmp.replace(snap)
    except OSError as e:
        log.warning("a megosztott fájlok listája nem menthető (%s): %s", uid, e)
    return keep


def _sweep(root: Path, ttl_seconds: int, keep: set[str] | None = None) -> int:
    now = time.time()
    removed = 0
    keep = keep or set()
    if "*" in keep:
        return 0
    for sub in ("inputs", "outputs", "temp"):
        d = root / sub
        if not d.exists():
            continue
        limit = min(ttl_seconds, 86400) if sub == "temp" else ttl_seconds  # temp: legfeljebb 1 nap
        for p in d.iterdir():
            if sub != "temp" and keep and p.name.removesuffix(".meta.json") in keep:
                continue                              # megosztva: nem jár le
            try:
                if p.is_file() and (now - p.stat().st_mtime) > limit:
                    p.unlink()
                    removed += 1
                    if sub != "temp" and not p.name.endswith(".meta.json"):
                        from app import db
                        db.mark_deleted_bg(p.name)
                elif p.is_dir() and (now - p.stat().st_mtime) > limit:
                    import shutil

                    shutil.rmtree(p, ignore_errors=True)
                    removed += 1
            except FileNotFoundError:
                pass
    return removed
