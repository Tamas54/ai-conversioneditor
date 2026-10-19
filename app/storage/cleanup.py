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


async def _shared_keep(root: Path) -> set[str]:
    """Megosztott projekt (és családja) nem jár le a megőrzéssel (app/sharing.py) — a munkaterület ezen fájljai maradnak."""
    from app import sharing
    from app.config import settings
    if not sharing.enabled():
        return set()
    uid = "admin" if root == settings.DATA_DIR else root.name
    try:
        return await sharing.shared_family(uid)
    except Exception:
        log.warning("megosztott fájlok listája nem olvasható (%s) — ebben a körben semmit nem törlünk itt", uid)
        return {"*"}


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
