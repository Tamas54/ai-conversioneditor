"""Postgres (házszabály: tárolás, napló, sor → PG): verziótörténet és lektorálási kérések.

    file_versions    minden fájl: forrása (source_file_id), művelete, címkéje, mérete — ebből a verziófa
    review_requests  „vissza Claude-nak": egy fájl + megjegyzés, nyitott/lezárt, a válasz-fájllal

A fájlok maguk a köteten maradnak (/data); itt csak a történetük. A metadata-sidecar (storage/metadata.py)
minden írása ide is bekerül (háttérben, hibatűrően: a PG kiesése nem állítja meg a szerkesztést).
A séma CREATE … IF NOT EXISTS — friss, csak ennek az appnak a saját adatbázisa; ALTER nincs benne.
Adatgyűjtés ex nunc: a bevezetés előtti fájlokról nincs sor.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from app.config import settings

log = logging.getLogger("aice.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS file_versions (
    file_id         text PRIMARY KEY,
    source_file_id  text,
    operation       text,
    label           text,
    ext             text,
    size_bytes      bigint,
    extra           jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);
CREATE INDEX IF NOT EXISTS file_versions_source_idx ON file_versions (source_file_id);
CREATE TABLE IF NOT EXISTS review_requests (
    id                  bigserial PRIMARY KEY,
    file_id             text NOT NULL,
    note                text,
    status              text NOT NULL DEFAULT 'open',
    requested_by        text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    resolved_at         timestamptz,
    resolution_file_id  text,
    resolution_note     text
);
CREATE INDEX IF NOT EXISTS review_requests_status_idx ON review_requests (status, created_at DESC);
"""

_pool = None
_KNOWN = {"label", "source_file_id", "operation", "original_filename", "created_at", "updated_at"}


def enabled() -> bool:
    return _pool is not None


async def start() -> None:
    global _pool
    if not settings.DATABASE_URL:
        log.warning("DATABASE_URL üres — verziótörténet és lektorálási kérések nélkül fut")
        return
    from psycopg_pool import AsyncConnectionPool
    pool = AsyncConnectionPool(settings.DATABASE_URL, min_size=1, max_size=5, open=False,
                               kwargs={"autocommit": True, "connect_timeout": 10})
    try:
        await pool.open(wait=True, timeout=20)
        async with pool.connection() as conn:
            await conn.execute(SCHEMA)
    except Exception:
        log.exception("Postgres nem érhető el — verziótörténet nélkül fut")
        await pool.close()
        return
    _pool = pool
    log.info("Postgres kész (verziótörténet, lektorálási kérések)")


async def stop() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    from psycopg.rows import dict_row
    async with _pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return [_jsonable(r) for r in await cur.fetchall()] if cur.description else []


def _jsonable(r: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}


# ---------- verziótörténet ----------

async def record_version(file_id: str, meta: dict[str, Any]) -> None:
    if not enabled():
        return
    from app.storage.files import input_path, output_path
    p = output_path(file_id) if output_path(file_id).exists() else input_path(file_id)
    size = p.stat().st_size if p.exists() else None
    extra = {k: v for k, v in meta.items() if k not in _KNOWN}
    await _rows(
        """INSERT INTO file_versions (file_id, source_file_id, operation, label, ext, size_bytes, extra)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (file_id) DO UPDATE SET
             source_file_id = COALESCE(EXCLUDED.source_file_id, file_versions.source_file_id),
             operation = COALESCE(EXCLUDED.operation, file_versions.operation),
             label = COALESCE(EXCLUDED.label, file_versions.label),
             size_bytes = COALESCE(EXCLUDED.size_bytes, file_versions.size_bytes),
             extra = file_versions.extra || EXCLUDED.extra,
             updated_at = now()""",
        (file_id, meta.get("source_file_id"), meta.get("operation"), meta.get("label") or meta.get("original_filename"),
         file_id.rsplit(".", 1)[-1].lower() if "." in file_id else None, size, json.dumps(extra, ensure_ascii=False, default=str)),
    )


def record_version_bg(file_id: str, meta: dict[str, Any]) -> None:
    """Szinkron kódból (write_meta): háttérfeladatként, hibatűrően."""
    if not enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(record_version(file_id, dict(meta)))
    task.add_done_callback(lambda t: t.exception() and log.warning("verzió-napló hiba (%s): %s", file_id, t.exception()))


def mark_deleted_bg(file_id: str) -> None:
    if not enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_rows("UPDATE file_versions SET deleted_at = now() WHERE file_id = %s", (file_id,)))


async def lineage(file_id: str) -> dict[str, Any]:
    """Az ősök lánca (forrásról forrásra a gyökérig) és a közvetlen/távoli leszármazottak."""
    if not enabled():
        raise RuntimeError("nincs Postgres (DATABASE_URL)")
    ancestors = await _rows(
        """WITH RECURSIVE up AS (
             SELECT *, 0 AS depth FROM file_versions WHERE file_id = %s
             UNION ALL
             SELECT f.*, up.depth + 1 FROM file_versions f JOIN up ON f.file_id = up.source_file_id WHERE up.depth < 50)
           SELECT file_id, source_file_id, operation, label, size_bytes, created_at, deleted_at, depth FROM up ORDER BY depth DESC""",
        (file_id,))
    descendants = await _rows(
        """WITH RECURSIVE down AS (
             SELECT *, 1 AS depth FROM file_versions WHERE source_file_id = %s
             UNION ALL
             SELECT f.*, down.depth + 1 FROM file_versions f JOIN down ON f.source_file_id = down.file_id WHERE down.depth < 50)
           SELECT file_id, source_file_id, operation, label, size_bytes, created_at, deleted_at, depth FROM down
           ORDER BY created_at LIMIT 200""",
        (file_id,))
    return {"file_id": file_id, "ancestors": ancestors, "descendants": descendants}


# ---------- lektorálási kérések ----------

async def review_create(file_id: str, note: Optional[str], requested_by: Optional[str]) -> dict[str, Any]:
    if not enabled():
        raise RuntimeError("nincs Postgres (DATABASE_URL)")
    rows = await _rows("""INSERT INTO review_requests (file_id, note, requested_by) VALUES (%s, %s, %s)
                          RETURNING id, file_id, note, status, requested_by, created_at""", (file_id, note, requested_by))
    return rows[0]


async def review_list(status: Optional[str] = "open", file_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    if not enabled():
        raise RuntimeError("nincs Postgres (DATABASE_URL)")
    where, params = [], []
    if status:
        where.append("r.status = %s"); params.append(status)
    if file_id:
        where.append("r.file_id = %s"); params.append(file_id)
    sql = ("SELECT r.*, v.label AS file_label FROM review_requests r LEFT JOIN file_versions v ON v.file_id = r.file_id"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY r.created_at DESC LIMIT %s")
    return await _rows(sql, (*params, limit))


async def review_resolve(review_id: int, resolution_file_id: Optional[str], note: Optional[str]) -> dict[str, Any]:
    if not enabled():
        raise RuntimeError("nincs Postgres (DATABASE_URL)")
    rows = await _rows("""UPDATE review_requests SET status = 'done', resolved_at = now(),
                            resolution_file_id = %s, resolution_note = %s
                          WHERE id = %s RETURNING *""", (resolution_file_id, note, review_id))
    if not rows:
        raise ValueError(f"nincs ilyen kérés: {review_id}")
    return rows[0]
