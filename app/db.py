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
CREATE TABLE IF NOT EXISTS users (
    id              text PRIMARY KEY,
    email           text UNIQUE,
    name            text,
    pw_hash         text,
    is_admin        boolean NOT NULL DEFAULT false,
    credits         integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_login_at   timestamptz,
    disabled        boolean NOT NULL DEFAULT false
);
INSERT INTO users (id, name, is_admin) VALUES ('admin', 'Admin', true) ON CONFLICT (id) DO NOTHING;
CREATE TABLE IF NOT EXISTS api_keys (
    id              bigserial PRIMARY KEY,
    user_id         text NOT NULL REFERENCES users(id),
    name            text,
    prefix          text NOT NULL,
    key_hash        text NOT NULL UNIQUE,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_used_at    timestamptz,
    revoked_at      timestamptz
);
CREATE TABLE IF NOT EXISTS credit_ledger (
    id              bigserial PRIMARY KEY,
    user_id         text NOT NULL REFERENCES users(id),
    delta           integer NOT NULL,
    balance_after   integer,
    reason          text NOT NULL,
    ref             text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS credit_ledger_user_idx ON credit_ledger (user_id, created_at DESC);
-- egyszeri, kis tábla (10-11-én jött létre): a fájl tulajdonosa (munkaterület)
ALTER TABLE file_versions ADD COLUMN IF NOT EXISTS owner_id text;
-- számlázás (10-11): csomag + Stripe-azonosítók a felhasználón — csak ha hiányzik (ADD COLUMN = ACCESS EXCLUSIVE zár)
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'users' AND column_name = 'plan') THEN
    ALTER TABLE users ADD COLUMN plan text NOT NULL DEFAULT 'free', ADD COLUMN stripe_customer_id text,
                      ADD COLUMN stripe_subscription_id text, ADD COLUMN topup_month text;
  END IF;
END $$;
CREATE TABLE IF NOT EXISTS usage_monthly (
    user_id         text NOT NULL,
    month           text NOT NULL,
    kind            text NOT NULL,
    n               integer NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, month, kind)
);
CREATE TABLE IF NOT EXISTS stripe_events (
    id              text PRIMARY KEY,
    type            text,
    received_at     timestamptz NOT NULL DEFAULT now()
);
-- OCR-eredmény oldalanként (a kép bájtjainak sha256-ja + prompt-verzió + modell): amit egyszer
-- kiolvastunk, nem fizetjük meg újra (ugyanaz a szkennelt oldal másodszor = 0 LLM-hívás)
CREATE TABLE IF NOT EXISTS ocr_cache (
    sha256          text NOT NULL,
    prompt_ver      text NOT NULL,
    model           text NOT NULL,
    text            text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sha256, prompt_ver, model)
);
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
    from app import tenancy
    await _rows(
        """INSERT INTO file_versions (file_id, source_file_id, operation, label, ext, size_bytes, extra, owner_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (file_id) DO UPDATE SET
             source_file_id = COALESCE(EXCLUDED.source_file_id, file_versions.source_file_id),
             operation = COALESCE(EXCLUDED.operation, file_versions.operation),
             label = COALESCE(EXCLUDED.label, file_versions.label),
             size_bytes = COALESCE(EXCLUDED.size_bytes, file_versions.size_bytes),
             extra = file_versions.extra || EXCLUDED.extra,
             updated_at = now()""",
        (file_id, meta.get("source_file_id"), meta.get("operation"), meta.get("label") or meta.get("original_filename"),
         file_id.rsplit(".", 1)[-1].lower() if "." in file_id else None, size, json.dumps(extra, ensure_ascii=False, default=str),
         tenancy.current_user_id()),
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
             SELECT *, 0 AS depth FROM file_versions WHERE file_id = %s AND owner_id IS NOT DISTINCT FROM %s
             UNION ALL
             SELECT f.*, up.depth + 1 FROM file_versions f JOIN up ON f.file_id = up.source_file_id WHERE up.depth < 50)
           SELECT file_id, source_file_id, operation, label, size_bytes, created_at, deleted_at, depth FROM up ORDER BY depth DESC""",
        (file_id, _owner()))
    descendants = await _rows(
        """WITH RECURSIVE down AS (
             SELECT *, 1 AS depth FROM file_versions WHERE source_file_id = %s AND owner_id IS NOT DISTINCT FROM %s
             UNION ALL
             SELECT f.*, down.depth + 1 FROM file_versions f JOIN down ON f.source_file_id = down.file_id WHERE down.depth < 50)
           SELECT file_id, source_file_id, operation, label, size_bytes, created_at, deleted_at, depth FROM down
           ORDER BY created_at LIMIT 200""",
        (file_id, _owner()))
    return {"file_id": file_id, "ancestors": ancestors, "descendants": descendants}


# ---------- lektorálási kérések ----------

async def review_create(file_id: str, note: Optional[str], requested_by: Optional[str]) -> dict[str, Any]:
    if not enabled():
        raise RuntimeError("nincs Postgres (DATABASE_URL)")
    if not await _owns(file_id):
        raise ValueError(f"nincs ilyen fájl ebben a munkaterületben: {file_id}")
    rows = await _rows("""INSERT INTO review_requests (file_id, note, requested_by) VALUES (%s, %s, %s)
                          RETURNING id, file_id, note, status, requested_by, created_at""", (file_id, note, requested_by))
    return rows[0]


async def review_list(status: Optional[str] = "open", file_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    if not enabled():
        raise RuntimeError("nincs Postgres (DATABASE_URL)")
    where, params = ["v.owner_id IS NOT DISTINCT FROM %s"], [_owner()]
    if status:
        where.append("r.status = %s"); params.append(status)
    if file_id:
        where.append("r.file_id = %s"); params.append(file_id)
    sql = ("SELECT r.*, v.label AS file_label FROM review_requests r JOIN file_versions v ON v.file_id = r.file_id"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY r.created_at DESC LIMIT %s")
    return await _rows(sql, (*params, limit))


async def review_resolve(review_id: int, resolution_file_id: Optional[str], note: Optional[str]) -> dict[str, Any]:
    if not enabled():
        raise RuntimeError("nincs Postgres (DATABASE_URL)")
    rows = await _rows("""UPDATE review_requests r SET status = 'done', resolved_at = now(),
                            resolution_file_id = %s, resolution_note = %s
                          FROM file_versions v
                          WHERE r.id = %s AND v.file_id = r.file_id AND v.owner_id IS NOT DISTINCT FROM %s
                          RETURNING r.*""", (resolution_file_id, note, review_id, _owner()))
    if not rows:
        raise ValueError(f"nincs ilyen kérés: {review_id}")
    return rows[0]


def _owner() -> str:
    from app import tenancy
    return tenancy.current_user_id()


async def _owns(file_id: str) -> bool:
    rows = await _rows("SELECT 1 FROM file_versions WHERE file_id = %s AND owner_id IS NOT DISTINCT FROM %s",
                       (file_id, _owner()))
    return bool(rows)


# ---------- fiókok, MCP-kulcsok, kreditek ----------

class InsufficientCredits(Exception):
    pass


def _hash_password(pw: str) -> str:
    import base64
    import hashlib
    import os
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def _check_password(pw: str, stored: Optional[str]) -> bool:
    import base64
    import hashlib
    import hmac as _hmac
    try:
        _, n, r, p, salt, h = (stored or "").split("$")
        got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return _hmac.compare_digest(got, base64.b64decode(h))
    except Exception:
        return False


async def user_create(email: str, password: str, name: Optional[str], credits: int = 0) -> dict[str, Any]:
    import secrets
    uid = secrets.token_hex(8)
    rows = await _rows("""INSERT INTO users (id, email, name, pw_hash, credits) VALUES (%s, lower(%s), %s, %s, %s)
                          ON CONFLICT (email) DO NOTHING RETURNING id, email, name, credits""",
                       (uid, email.strip(), name, _hash_password(password), credits))
    if not rows:
        raise ValueError("this email is already registered")
    return rows[0]


async def user_login(email: str, password: str) -> Optional[dict[str, Any]]:
    rows = await _rows("SELECT id, email, name, pw_hash, disabled FROM users WHERE email = lower(%s)", (email.strip(),))
    if not rows or rows[0]["disabled"] or not _check_password(password, rows[0]["pw_hash"]):
        return None
    await _rows("UPDATE users SET last_login_at = now() WHERE id = %s", (rows[0]["id"],))
    return {k: rows[0][k] for k in ("id", "email", "name")}


async def user_get(uid: str) -> Optional[dict[str, Any]]:
    rows = await _rows("SELECT id, email, name, is_admin, credits, created_at, disabled, plan, stripe_customer_id, "
                       "stripe_subscription_id FROM users WHERE id = %s", (uid,))
    return rows[0] if rows else None


def _key_hash(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()


async def api_key_create(uid: str, name: Optional[str]) -> dict[str, Any]:
    import secrets
    key = "sk_" + secrets.token_urlsafe(30)
    rows = await _rows("""INSERT INTO api_keys (user_id, name, prefix, key_hash) VALUES (%s, %s, %s, %s)
                          RETURNING id, name, prefix, created_at""", (uid, name, key[:10], _key_hash(key)))
    return {**rows[0], "key": key}  # a teljes kulcs csak itt, egyszer látszik


async def api_key_list(uid: str) -> list[dict[str, Any]]:
    return await _rows("""SELECT id, name, prefix, created_at, last_used_at FROM api_keys
                          WHERE user_id = %s AND revoked_at IS NULL ORDER BY created_at DESC""", (uid,))


async def api_key_revoke(uid: str, key_id: int) -> bool:
    rows = await _rows("UPDATE api_keys SET revoked_at = now() WHERE id = %s AND user_id = %s AND revoked_at IS NULL RETURNING id",
                       (key_id, uid))
    return bool(rows)


async def api_key_user(key: str) -> Optional[str]:
    rows = await _rows("""UPDATE api_keys SET last_used_at = now() WHERE key_hash = %s AND revoked_at IS NULL
                          RETURNING user_id""", (_key_hash(key),))
    return rows[0]["user_id"] if rows else None


async def credits_spend(uid: str, amount: int, reason: str, ref: Optional[str] = None) -> Optional[int]:
    """Atomi levonás; admin korlátlan (None). Nincs fedezet → InsufficientCredits."""
    if uid == "admin" or amount <= 0:
        return None
    rows = await _rows("""UPDATE users SET credits = credits - %s WHERE id = %s AND credits >= %s RETURNING credits""",
                       (amount, uid, amount))
    if not rows:
        raise InsufficientCredits(f"not enough credits ({amount} needed for {reason})")
    await _rows("INSERT INTO credit_ledger (user_id, delta, balance_after, reason, ref) VALUES (%s, %s, %s, %s, %s)",
                (uid, -amount, rows[0]["credits"], reason, ref))
    return rows[0]["credits"]


async def credits_add(uid: str, amount: int, reason: str, ref: Optional[str] = None) -> int:
    rows = await _rows("UPDATE users SET credits = credits + %s WHERE id = %s RETURNING credits", (amount, uid))
    if not rows:
        raise ValueError(f"no such user: {uid}")
    await _rows("INSERT INTO credit_ledger (user_id, delta, balance_after, reason, ref) VALUES (%s, %s, %s, %s, %s)",
                (uid, amount, rows[0]["credits"], reason, ref))
    return rows[0]["credits"]


# ---------- OCR-gyorsítótár ----------

async def ocr_cache_get(sha256: str, prompt_ver: str, model: str) -> Optional[str]:
    if not enabled():
        return None
    rows = await _rows("SELECT text FROM ocr_cache WHERE sha256 = %s AND prompt_ver = %s AND model = %s",
                       (sha256, prompt_ver, model))
    return rows[0]["text"] if rows else None


async def ocr_cache_put(sha256: str, prompt_ver: str, model: str, text: str) -> None:
    if not enabled():
        return
    await _rows("INSERT INTO ocr_cache (sha256, prompt_ver, model, text) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (sha256, prompt_ver, model) DO NOTHING", (sha256, prompt_ver, model, text))


async def reassign_owner(src_uid: str, dst_uid: str) -> None:
    """Vendég → fiók: a verziótörténet sorai az új tulajdonoshoz."""
    if enabled():
        await _rows("UPDATE file_versions SET owner_id = %s WHERE owner_id = %s", (dst_uid, src_uid))


# ---------- számlázás: havi keretek, ingyenes feltöltés, Stripe ----------

async def usage_get(uid: str, month: str, kind: str) -> int:
    rows = await _rows("SELECT n FROM usage_monthly WHERE user_id = %s AND month = %s AND kind = %s", (uid, month, kind))
    return rows[0]["n"] if rows else 0


async def usage_add(uid: str, month: str, kind: str, n: int, limit: Optional[int]) -> Optional[int]:
    """Atomi növelés; ha a keret (limit) túllépne, nem növel és None-t ad."""
    if limit is None:
        rows = await _rows("""INSERT INTO usage_monthly (user_id, month, kind, n) VALUES (%s, %s, %s, %s)
                              ON CONFLICT (user_id, month, kind) DO UPDATE SET n = usage_monthly.n + EXCLUDED.n RETURNING n""",
                           (uid, month, kind, n))
        return rows[0]["n"]
    rows = await _rows("""INSERT INTO usage_monthly AS u (user_id, month, kind, n) SELECT %s, %s, %s, %s WHERE %s <= %s
                          ON CONFLICT (user_id, month, kind) DO UPDATE SET n = u.n + EXCLUDED.n WHERE u.n + EXCLUDED.n <= %s
                          RETURNING n""", (uid, month, kind, n, n, limit, limit))
    return rows[0]["n"] if rows else None


async def free_topup(uid: str, month: str, amount: int) -> int:
    """Ingyenes csomag: hónaponként egyszer feltöltés legalább `amount` kreditre (nem halmozódik). Vissza: jóváírt."""
    rows = await _rows("""WITH old AS (SELECT credits FROM users WHERE id = %s AND plan = 'free'
                                         AND topup_month IS DISTINCT FROM %s FOR UPDATE)
                          UPDATE users u SET credits = GREATEST(u.credits, %s), topup_month = %s FROM old
                          WHERE u.id = %s RETURNING u.credits AS now, old.credits AS before""", (uid, month, amount, month, uid))
    if not rows:
        return 0
    delta = rows[0]["now"] - rows[0]["before"]
    if delta > 0:
        await _rows("INSERT INTO credit_ledger (user_id, delta, balance_after, reason, ref) VALUES (%s, %s, %s, %s, %s)",
                    (uid, delta, rows[0]["now"], "free monthly credits", month))
    return delta


async def user_set_billing(uid: str, *, plan: Optional[str] = None, customer: Optional[str] = None,
                           subscription: Optional[str] = None) -> None:
    sets, vals = [], []
    for col, v in (("plan", plan), ("stripe_customer_id", customer), ("stripe_subscription_id", subscription)):
        if v is not None:
            sets.append(f"{col} = %s"); vals.append(v or None)
    if sets:
        await _rows(f"UPDATE users SET {', '.join(sets)} WHERE id = %s", (*vals, uid))


async def user_by_subscription(sub: Optional[str], customer: Optional[str]) -> Optional[dict[str, Any]]:
    rows = await _rows("SELECT * FROM users WHERE (stripe_subscription_id = %s::text AND %s::text IS NOT NULL) OR "
                       "(stripe_customer_id = %s::text AND %s::text IS NOT NULL) LIMIT 1", (sub, sub, customer, customer))
    return rows[0] if rows else None


async def stripe_event_seen(event_id: str) -> bool:
    return bool(await _rows("SELECT 1 FROM stripe_events WHERE id = %s", (event_id,)))


async def stripe_event_done(event_id: str, kind: str) -> None:
    """Csak SIKERES feldolgozás után: ha közben hiba van, a Stripe újrapróbálkozása újra feldolgozza."""
    await _rows("INSERT INTO stripe_events (id, type) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", (event_id, kind))

