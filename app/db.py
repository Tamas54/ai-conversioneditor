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
-- a felhasználó arculatai (mintákból tanulva vagy kézzel): színek, betűk, logó, elrendezési szabályok (spec, JSON);
-- az alapértelmezett (is_default) minden dizájnra rákerül, ha auto_apply igaz — a felületen kikapcsolható
CREATE TABLE IF NOT EXISTS brand_kits (
    id              bigserial PRIMARY KEY,
    owner_id        text NOT NULL,
    name            text NOT NULL,
    spec            jsonb NOT NULL,
    is_default      boolean NOT NULL DEFAULT false,
    auto_apply      boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS brand_kits_owner_idx ON brand_kits (owner_id, created_at DESC);
-- tartalmi ellenőrzés (10-14): egy szöveg (hash) ellentmondás-/stílus-/állítás-eredménye — ugyanarra a szövegre nem
-- fizetünk újra; az állítás-ítéletnél a kora számít (a tény elavulhat, ld. check_get max_age_s)
CREATE TABLE IF NOT EXISTS content_checks (
    kind            text NOT NULL,
    key             text NOT NULL,
    prompt_ver      text NOT NULL,
    result          jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, key, prompt_ver)
);
-- élő adatok (10-14): a dokumentumban egy könyvjelző (aice_…) a horgony — a Word és a WordCraft is megtartja —,
-- itt a forrása (spec), a számformátum és az utolsó érték
CREATE TABLE IF NOT EXISTS data_bindings (
    id              text PRIMARY KEY,
    owner_id        text,
    label           text,
    spec            jsonb NOT NULL,
    fmt             jsonb NOT NULL,
    value           double precision,
    display         text,
    period          text,
    source_name     text,
    source_url      text,
    file_id         text,
    checked_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS data_bindings_owner_idx ON data_bindings (owner_id, created_at DESC);
CREATE TABLE IF NOT EXISTS ocr_cache (
    sha256          text NOT NULL,
    prompt_ver      text NOT NULL,
    model           text NOT NULL,
    text            text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sha256, prompt_ver, model)
);
-- ---- bővítés (10-15), csak additív: mentett konverziós beállítások, nyilvános REST API (jobs/tasks), webhookok,
-- aláírások és űrlap-adatprofil (titkosítva, felhasználónként) ----
CREATE TABLE IF NOT EXISTS convert_presets (
    id              bigserial PRIMARY KEY,
    owner_id        text NOT NULL,
    name            text NOT NULL,
    scope           text NOT NULL,
    options         jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_id, scope, name)
);
CREATE TABLE IF NOT EXISTS api_jobs (
    id              text PRIMARY KEY,
    owner_id        text NOT NULL,
    tag             text,
    status          text NOT NULL,
    sandbox         boolean NOT NULL DEFAULT false,
    webhook_url     text,
    credits         integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    ended_at        timestamptz
);
CREATE INDEX IF NOT EXISTS api_jobs_owner_idx ON api_jobs (owner_id, created_at DESC);
CREATE TABLE IF NOT EXISTS api_tasks (
    id              text PRIMARY KEY,
    job_id          text NOT NULL,
    owner_id        text NOT NULL,
    name            text NOT NULL,
    operation       text NOT NULL,
    status          text NOT NULL,
    depends_on      jsonb NOT NULL DEFAULT '[]',
    payload         jsonb NOT NULL DEFAULT '{}',
    result          jsonb,
    message         text,
    code            text,
    percent         integer NOT NULL DEFAULT 0,
    credits         integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    ended_at        timestamptz
);
CREATE INDEX IF NOT EXISTS api_tasks_job_idx ON api_tasks (job_id);
CREATE TABLE IF NOT EXISTS api_webhooks (
    id              bigserial PRIMARY KEY,
    owner_id        text NOT NULL,
    url             text NOT NULL,
    events          jsonb NOT NULL DEFAULT '["job.finished","job.failed"]',
    created_at      timestamptz NOT NULL DEFAULT now(),
    disabled_at     timestamptz,
    last_status     integer,
    last_error      text,
    last_at         timestamptz
);
CREATE INDEX IF NOT EXISTS api_webhooks_owner_idx ON api_webhooks (owner_id);
CREATE TABLE IF NOT EXISTS user_signatures (
    id              text PRIMARY KEY,
    owner_id        text NOT NULL,
    name            text NOT NULL,
    kind            text NOT NULL DEFAULT 'signature',
    nonce           bytea NOT NULL,
    data            bytea NOT NULL,
    width           integer,
    height          integer,
    created_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);
CREATE INDEX IF NOT EXISTS user_signatures_owner_idx ON user_signatures (owner_id, created_at DESC);
CREATE TABLE IF NOT EXISTS sheet_templates (
    id              text PRIMARY KEY,
    owner_id        text NOT NULL,
    name            text NOT NULL,
    nonce           bytea NOT NULL,
    data            bytea NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sheet_templates_owner_idx ON sheet_templates (owner_id, created_at DESC);
CREATE TABLE IF NOT EXISTS user_profiles (
    owner_id        text PRIMARY KEY,
    nonce           bytea NOT NULL,
    data            bytea NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
-- ---- megosztás (OMLETT 4, 10-19), csak additív: ki mit lát / szerkeszt más munkaterületén (app/sharing.py) ----
-- shares: egy fájl/projekt (és a családja: ősei, változatai, médiafájljai) egy meghívottnak; grantee_id NULL = függő
-- meghívó e-mailre (regisztráláskor / belépéskor oldódik fel); share_links: szerepkörös link (csak belépve ér valamit)
CREATE TABLE IF NOT EXISTS shares (
    id              bigserial PRIMARY KEY,
    owner_id        text NOT NULL,
    file_id         text NOT NULL,
    grantee_id      text,
    grantee_email   text,
    role            text NOT NULL,
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    accepted_at     timestamptz,
    revoked_at      timestamptz
);
CREATE INDEX IF NOT EXISTS shares_grantee_idx ON shares (grantee_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS shares_owner_idx ON shares (owner_id, file_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS shares_email_idx ON shares (lower(grantee_email)) WHERE grantee_id IS NULL AND revoked_at IS NULL;
CREATE TABLE IF NOT EXISTS share_links (
    id              bigserial PRIMARY KEY,
    token_hash      text NOT NULL UNIQUE,
    owner_id        text NOT NULL,
    file_id         text NOT NULL,
    role            text NOT NULL,
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    revoked_at      timestamptz
);
-- napló: ki, mit, mikor (megosztás, változat, ütközés, választás, törlés) a tulajdonos munkaterületén
CREATE TABLE IF NOT EXISTS share_audit (
    id              bigserial PRIMARY KEY,
    owner_id        text NOT NULL,
    actor_id        text NOT NULL,
    file_id         text,
    action          text NOT NULL,
    detail          jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS share_audit_owner_idx ON share_audit (owner_id, created_at DESC);
-- szerkesztési zár (tanácsadó): a változatlánc gyökerén, szívveréssel; lejárt szívverés = szabad
CREATE TABLE IF NOT EXISTS edit_locks (
    owner_id        text NOT NULL,
    file_id         text NOT NULL,
    holder_id       text NOT NULL,
    heartbeat_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, file_id)
);
-- ---- kreditkönyv (10-19, OMLETT 2.3): a meglévő credit_ledger bővül (fajta, művelet, szolgáltató, idempotencia-kulcs);
-- az egyenleg továbbra is users.credits (gyorsítótár), ugyanabban a tranzakcióban írva — app/credits.py ----
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'credit_ledger' AND column_name = 'kind') THEN
    ALTER TABLE credit_ledger ADD COLUMN kind text, ADD COLUMN workspace_id text, ADD COLUMN operation text,
                              ADD COLUMN provider text, ADD COLUMN provider_task_id text, ADD COLUMN provider_cost numeric,
                              ADD COLUMN idempotency_key text, ADD COLUMN meta jsonb;
    UPDATE credit_ledger SET kind = CASE WHEN reason LIKE 'bought %' THEN 'purchase' WHEN reason LIKE 'refund%' THEN 'refund'
                                         WHEN reason = 'admin grant' THEN 'adjust' WHEN delta < 0 THEN 'capture' ELSE 'grant' END;
  END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS credit_ledger_idem_idx ON credit_ledger (idempotency_key);
-- nyitó egyenleg: ahol a users.credits nem egyezik a napló összegével (kézi jóváírás SQL-ből, a régi 0-s „signup” sor),
-- egy „adjust” sor a különbséggel — felhasználónként egyszer (idempotency_key), az egyenleg maga nem változik
INSERT INTO credit_ledger (user_id, delta, balance_after, reason, kind, idempotency_key)
SELECT u.id, u.credits - COALESCE(s.total, 0), u.credits, 'opening balance', 'adjust', 'opening:' || u.id
FROM users u LEFT JOIN (SELECT user_id, sum(delta) AS total FROM credit_ledger GROUP BY user_id) s ON s.user_id = u.id
WHERE u.id <> 'admin' AND u.credits <> COALESCE(s.total, 0)
ON CONFLICT (idempotency_key) DO NOTHING;
-- árazás: ár = ceil(provider_cost × egység × multiplier), legalább min_charge (provider_cost a szolgáltató saját egységében,
-- pl. kie-kredit/mp; a multiplier ebből Studio-kreditet csinál, haszonnal). Adminból szerkeszthető; a helyi CPU-művelet 0.
CREATE TABLE IF NOT EXISTS pricing_rules (
    id              bigserial PRIMARY KEY,
    operation       text NOT NULL,
    model           text NOT NULL DEFAULT '',
    tier            text NOT NULL DEFAULT '',
    unit            text NOT NULL DEFAULT 'job',
    provider        text,
    provider_cost   numeric NOT NULL DEFAULT 0,
    multiplier      numeric NOT NULL DEFAULT 1,
    min_charge      integer NOT NULL DEFAULT 0,
    note            text,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (operation, model, tier)
);
-- külső AI-feladatok (kie.ai…): a mi azonosítónk (a hold kulcsa is), a szolgáltatóé, állapot, a tárolt eredményfájlok —
-- újraindítás után is innen folytatódik (lekérdezés), a webhook a provider_task_id-vel talál ide
CREATE TABLE IF NOT EXISTS provider_tasks (
    id                text PRIMARY KEY,
    user_id           text NOT NULL,
    provider          text NOT NULL,
    model             text NOT NULL,
    operation         text NOT NULL,
    provider_task_id  text UNIQUE,
    status            text NOT NULL DEFAULT 'created',
    settled           boolean NOT NULL DEFAULT false,
    input             jsonb NOT NULL DEFAULT '{}',
    hold              integer NOT NULL DEFAULT 0,
    charged           integer,
    provider_cost     numeric,
    result            jsonb,
    error             text,
    meta              jsonb NOT NULL DEFAULT '{}',
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz
);
CREATE INDEX IF NOT EXISTS provider_tasks_open_idx ON provider_tasks (created_at) WHERE NOT settled;
CREATE INDEX IF NOT EXISTS provider_tasks_user_idx ON provider_tasks (user_id, created_at DESC);
-- napi egyeztetés (szolgáltatói egyenleg ↔ a könyv provider-költségei) és az admin-felület figyelmeztetései
CREATE TABLE IF NOT EXISTS provider_balance (
    id              bigserial PRIMARY KEY,
    provider        text NOT NULL,
    balance         numeric NOT NULL,
    ledger_cost     numeric,
    expected        numeric,
    drift           numeric,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS admin_notices (
    id              bigserial PRIMARY KEY,
    kind            text NOT NULL,
    message         text NOT NULL,
    meta            jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz
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
    from app.storage.files import file_workspace
    rows = await _rows(
        """INSERT INTO file_versions (file_id, source_file_id, operation, label, ext, size_bytes, extra, owner_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (file_id) DO UPDATE SET
             source_file_id = COALESCE(EXCLUDED.source_file_id, file_versions.source_file_id),
             operation = COALESCE(EXCLUDED.operation, file_versions.operation),
             label = COALESCE(EXCLUDED.label, file_versions.label),
             size_bytes = COALESCE(EXCLUDED.size_bytes, file_versions.size_bytes),
             extra = file_versions.extra || EXCLUDED.extra,
             updated_at = now()
           RETURNING (xmax = 0) AS inserted, owner_id""",
        (file_id, meta.get("source_file_id"), meta.get("operation"), meta.get("label") or meta.get("original_filename"),
         file_id.rsplit(".", 1)[-1].lower() if "." in file_id else None, size, json.dumps(extra, ensure_ascii=False, default=str),
         file_workspace(file_id)),     # a fájl munkaterülete (megosztott projektben a tulajdonosé, nem a szerkesztőé)
    )
    if rows and rows[0]["inserted"]:
        from app import sharing
        await sharing.on_new_version(file_id, rows[0]["owner_id"], meta)    # megosztott családban: napló + ütközés


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
        raise RuntimeError("No database configured (DATABASE_URL).")
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
    vis = _visible()                     # megosztott fájlnál csak a megosztott család (a nyersanyag más ágai nem)
    if vis is not None:
        ancestors = [r for r in ancestors if r["file_id"] in vis]
        descendants = [r for r in descendants if r["file_id"] in vis]
    return {"file_id": file_id, "ancestors": ancestors, "descendants": descendants}


# ---------- lektorálási kérések ----------

async def review_create(file_id: str, note: Optional[str], requested_by: Optional[str]) -> dict[str, Any]:
    if not enabled():
        raise RuntimeError("No database configured (DATABASE_URL).")
    if not await _owns(file_id):
        raise ValueError(f"No such file in this workspace: {file_id}")
    rows = await _rows("""INSERT INTO review_requests (file_id, note, requested_by) VALUES (%s, %s, %s)
                          RETURNING id, file_id, note, status, requested_by, created_at""", (file_id, note, requested_by))
    return rows[0]


async def review_list(status: Optional[str] = "open", file_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    if not enabled():
        raise RuntimeError("No database configured (DATABASE_URL).")
    where, params = ["v.owner_id IS NOT DISTINCT FROM %s"], [_actor()]    # a SAJÁT bejövő kérései (megosztott kérésben is)
    if status:
        where.append("r.status = %s"); params.append(status)
    if file_id:
        where.append("r.file_id = %s"); params.append(file_id)
    sql = ("SELECT r.*, v.label AS file_label FROM review_requests r JOIN file_versions v ON v.file_id = r.file_id"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY r.created_at DESC LIMIT %s")
    return await _rows(sql, (*params, limit))


async def review_resolve(review_id: int, resolution_file_id: Optional[str], note: Optional[str]) -> dict[str, Any]:
    if not enabled():
        raise RuntimeError("No database configured (DATABASE_URL).")
    rows = await _rows("""UPDATE review_requests r SET status = 'done', resolved_at = now(),
                            resolution_file_id = %s, resolution_note = %s
                          FROM file_versions v
                          WHERE r.id = %s AND v.file_id = r.file_id AND v.owner_id IS NOT DISTINCT FROM %s
                          RETURNING r.*""", (resolution_file_id, note, review_id, _actor()))
    if not rows:
        raise ValueError(f"No such review request: {review_id}")
    return rows[0]


def _owner() -> str:
    from app import tenancy
    return tenancy.workspace_id()         # megosztott fájlnál a tulajdonos munkaterülete (fájlonkénti műveletekhez)


def _actor() -> str:
    from app import tenancy
    return tenancy.current_user_id()      # a belépett felhasználó saját munkaterülete (listákhoz, azonosító szerinti eléréshez)


def _visible() -> Optional[set]:
    """Megosztott kérésben a tulajdonos munkaterületéből látható fájlok (a család) — különben None (minden a sajátja)."""
    from app import tenancy
    return tenancy.visible_root()[1]


async def _owns(file_id: str) -> bool:
    rows = await _rows("SELECT 1 FROM file_versions WHERE file_id = %s AND owner_id IS NOT DISTINCT FROM %s",
                       (file_id, _owner()))
    return bool(rows)


# ---------- fiókok, MCP-kulcsok, kreditek ----------

class InsufficientCredits(Exception):
    """Nincs fedezet. needed / available: mennyi kellene és mennyi van (MCP: insufficient_credits)."""

    def __init__(self, msg: str = "", *, needed: Optional[int] = None, available: Optional[int] = None):
        self.needed, self.available = needed, available
        super().__init__(msg or f"insufficient_credits: this needs {needed} credits, you have {available}")


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
    if credits:                     # a kezdő kredit is a könyvben (különben a nyitó egyenleg-egyeztetés pótolná)
        await _rows("INSERT INTO credit_ledger (user_id, delta, balance_after, reason, kind, idempotency_key) "
                    "VALUES (%s, %s, %s, 'signup', 'grant', %s) ON CONFLICT (idempotency_key) DO NOTHING",
                    (uid, credits, credits, f"signup:{uid}"))
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


async def api_key_create(uid: str, name: Optional[str], sandbox: bool = False) -> dict[str, Any]:
    import secrets
    key = ("sk_test_" if sandbox else "sk_") + secrets.token_urlsafe(30)
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


async def api_key_active(key_id: int, uid: str) -> bool:
    """OAuth-engedély (a kapcsolódott AI api_keys-sora): él-e még — a meglévő táblán, DDL nélkül (app/oauth.py)."""
    rows = await _rows("""UPDATE api_keys SET last_used_at = now() WHERE id = %s AND user_id = %s AND revoked_at IS NULL
                          RETURNING id""", (key_id, uid))
    return bool(rows)


async def api_key_user(key: str) -> Optional[str]:
    rows = await _rows("""UPDATE api_keys SET last_used_at = now() WHERE key_hash = %s AND revoked_at IS NULL
                          RETURNING user_id""", (_key_hash(key),))
    return rows[0]["user_id"] if rows else None


async def credits_spend(uid: str, amount: int, reason: str, ref: Optional[str] = None, **kw: Any) -> Optional[int]:
    """Atomi levonás (a könyvben „capture”: azonnal lefoglalt és elszámolt); admin korlátlan (None). Nincs fedezet →
    InsufficientCredits. kw: operation, idempotency_key, meta… (app/credits.py apply)."""
    if uid == "admin" or amount <= 0:
        return None
    from app import credits
    return (await credits.apply(uid, -amount, kw.pop("kind", "capture"), reason, ref=ref, **kw))["balance"]


async def credits_add(uid: str, amount: int, reason: str, ref: Optional[str] = None, **kw: Any) -> int:
    """Jóváírás a könyvben (kind: grant | purchase | refund | adjust — a reason-ből, ha nincs megadva)."""
    from app import credits
    kind = kw.pop("kind", None) or ("refund" if reason.startswith("refund") else "purchase" if reason.startswith("bought")
                                    else "adjust" if reason.startswith("admin") else "grant")
    return (await credits.apply(uid, amount, kind, reason, ref=ref, **kw))["balance"]


# ---------- OCR-gyorsítótár ----------

async def ocr_cache_get(sha256: str, prompt_ver: str, model: str) -> Optional[str]:
    if not enabled():
        return None
    rows = await _rows("SELECT text FROM ocr_cache WHERE sha256 = %s AND prompt_ver = %s AND model = %s",
                       (sha256, prompt_ver, model))
    return rows[0]["text"] if rows else None


async def ocr_cache_put(sha256: str, prompt_ver: str, model: str, text: str, replace: bool = False) -> None:
    """replace: a meglévő bejegyzés felülírása (pl. az átirat force-szal újrahallgatva)."""
    if not enabled():
        return
    await _rows("INSERT INTO ocr_cache (sha256, prompt_ver, model, text) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (sha256, prompt_ver, model) DO "
                + ("UPDATE SET text = EXCLUDED.text, created_at = now()" if replace else "NOTHING"),
                (sha256, prompt_ver, model, text))


# ---------- tartalmi ellenőrzés (gyorsítótár) és élő adatok ----------

async def check_get(kind: str, key: str, prompt_ver: str, max_age_s: Optional[int] = None) -> Optional[Any]:
    if not enabled():
        return None
    rows = await _rows("SELECT result, extract(epoch FROM now() - created_at) AS age FROM content_checks "
                       "WHERE kind = %s AND key = %s AND prompt_ver = %s", (kind, key, prompt_ver))
    if not rows or (max_age_s is not None and float(rows[0]["age"]) > max_age_s):
        return None
    return rows[0]["result"]


async def check_put(kind: str, key: str, prompt_ver: str, result: Any) -> None:
    if not enabled():
        return
    await _rows("INSERT INTO content_checks (kind, key, prompt_ver, result) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (kind, key, prompt_ver) DO UPDATE SET result = EXCLUDED.result, created_at = now()",
                (kind, key, prompt_ver, json.dumps(result, ensure_ascii=False, default=str)))


async def binding_put(b: dict[str, Any]) -> None:
    if not enabled():
        raise RuntimeError("Live data needs the database (DATABASE_URL).")
    await _rows(
        """INSERT INTO data_bindings (id, owner_id, label, spec, fmt, value, display, period, source_name, source_url,
                                      file_id, checked_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
           ON CONFLICT (id) DO UPDATE SET value = EXCLUDED.value, display = EXCLUDED.display, period = EXCLUDED.period,
             source_name = EXCLUDED.source_name, source_url = EXCLUDED.source_url, file_id = EXCLUDED.file_id,
             checked_at = now()""",
        (b["id"], _owner(), b.get("label"), json.dumps(b["spec"], ensure_ascii=False), json.dumps(b["fmt"], ensure_ascii=False),
         b.get("value"), b.get("display"), b.get("period"), b.get("source_name"), b.get("source_url"), b.get("file_id")))


async def bindings_get(ids: list[str]) -> dict[str, dict[str, Any]]:
    if not enabled() or not ids:
        return {}
    rows = await _rows("SELECT * FROM data_bindings WHERE id = ANY(%s) AND owner_id IS NOT DISTINCT FROM %s",
                       (list(ids), _owner()))
    vis = _visible()                     # megosztott dokumentum: csak a családhoz tartozó kötések
    return {r["id"]: r for r in rows if vis is None or r.get("file_id") in vis}


async def reassign_owner(src_uid: str, dst_uid: str) -> None:
    """Vendég → fiók: a verziótörténet sorai és az arculatok az új tulajdonoshoz."""
    if enabled():
        await _rows("UPDATE file_versions SET owner_id = %s WHERE owner_id = %s", (dst_uid, src_uid))
        await _rows("UPDATE brand_kits SET owner_id = %s, is_default = is_default AND NOT EXISTS "
                    "(SELECT 1 FROM brand_kits b WHERE b.owner_id = %s AND b.is_default) WHERE owner_id = %s",
                    (dst_uid, dst_uid, src_uid))
        await _rows("UPDATE data_bindings SET owner_id = %s WHERE owner_id = %s", (dst_uid, src_uid))


# ---------- arculatok (brand kits) ----------

_BRAND_COLS = "id, name, spec, is_default, auto_apply, created_at, updated_at"


async def brand_list(uid: str) -> list[dict[str, Any]]:
    return await _rows(f"SELECT {_BRAND_COLS} FROM brand_kits WHERE owner_id = %s ORDER BY is_default DESC, updated_at DESC", (uid,))


async def brand_get(uid: str, brand_id: int) -> Optional[dict[str, Any]]:
    rows = await _rows(f"SELECT {_BRAND_COLS} FROM brand_kits WHERE owner_id = %s AND id = %s", (uid, brand_id))
    return rows[0] if rows else None


async def brand_by_name(uid: str, name: str) -> Optional[dict[str, Any]]:
    rows = await _rows(f"SELECT {_BRAND_COLS} FROM brand_kits WHERE owner_id = %s AND lower(name) = lower(%s) "
                       "ORDER BY updated_at DESC LIMIT 1", (uid, name))
    return rows[0] if rows else None


async def brand_default(uid: str) -> Optional[dict[str, Any]]:
    rows = await _rows(f"SELECT {_BRAND_COLS} FROM brand_kits WHERE owner_id = %s AND is_default LIMIT 1", (uid,))
    return rows[0] if rows else None


async def brand_create(uid: str, name: str, spec: dict[str, Any], make_default: Optional[bool] = None) -> dict[str, Any]:
    """Új arculat; az első mindig alapértelmezett (make_default=None), különben csak kérésre."""
    has = await _rows("SELECT 1 FROM brand_kits WHERE owner_id = %s AND is_default LIMIT 1", (uid,))
    default = (not has) if make_default is None else make_default
    if default:
        await _rows("UPDATE brand_kits SET is_default = false WHERE owner_id = %s", (uid,))
    rows = await _rows(f"INSERT INTO brand_kits (owner_id, name, spec, is_default) VALUES (%s, %s, %s, %s) RETURNING {_BRAND_COLS}",
                       (uid, name[:80], json.dumps(spec, ensure_ascii=False), default))
    return rows[0]


async def brand_update(uid: str, brand_id: int, *, name: Optional[str] = None, spec: Optional[dict[str, Any]] = None,
                       auto_apply: Optional[bool] = None, is_default: Optional[bool] = None) -> Optional[dict[str, Any]]:
    if is_default:
        await _rows("UPDATE brand_kits SET is_default = false WHERE owner_id = %s AND id <> %s", (uid, brand_id))
    rows = await _rows(
        f"""UPDATE brand_kits SET name = COALESCE(%s, name), spec = COALESCE(%s::jsonb, spec),
               auto_apply = COALESCE(%s, auto_apply), is_default = COALESCE(%s, is_default), updated_at = now()
            WHERE owner_id = %s AND id = %s RETURNING {_BRAND_COLS}""",
        (name[:80] if name else None, json.dumps(spec, ensure_ascii=False) if spec is not None else None, auto_apply, is_default,
         uid, brand_id))
    return rows[0] if rows else None


async def brand_delete(uid: str, brand_id: int) -> bool:
    rows = await _rows("DELETE FROM brand_kits WHERE owner_id = %s AND id = %s RETURNING is_default", (uid, brand_id))
    if rows and rows[0]["is_default"]:            # a következő legyen az alapértelmezett (ha van)
        await _rows("UPDATE brand_kits SET is_default = true WHERE id = (SELECT id FROM brand_kits WHERE owner_id = %s "
                    "ORDER BY updated_at DESC LIMIT 1)", (uid,))
    return bool(rows)


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
    rows = await _rows("""UPDATE users SET topup_month = %s WHERE id = %s AND plan = 'free' AND topup_month IS DISTINCT FROM %s
                          RETURNING id""", (month, uid, month))
    if not rows:
        return 0
    from app import credits                      # a hiány a sorzár alatt számolva (közben levonás is jöhet)
    res = await credits.apply(uid, 0, "grant", "free monthly credits", ref=month, idempotency_key=f"topup:{uid}:{month}",
                              top_up_to=amount)
    return res["delta"]


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



# ---------- mentett konverziós / eszköz-beállítások ----------

async def preset_list(uid: str, scope: Optional[str] = None) -> list[dict[str, Any]]:
    if scope:
        return await _rows("SELECT id, name, scope, options, created_at FROM convert_presets WHERE owner_id = %s AND scope = %s "
                           "ORDER BY name", (uid, scope))
    return await _rows("SELECT id, name, scope, options, created_at FROM convert_presets WHERE owner_id = %s ORDER BY scope, name", (uid,))


async def preset_get(uid: str, preset_id: int) -> Optional[dict[str, Any]]:
    rows = await _rows("SELECT id, name, scope, options FROM convert_presets WHERE owner_id = %s AND id = %s", (uid, preset_id))
    return rows[0] if rows else None


async def preset_save(uid: str, name: str, scope: str, options: dict[str, Any]) -> dict[str, Any]:
    from psycopg.types.json import Jsonb
    rows = await _rows("""INSERT INTO convert_presets (owner_id, name, scope, options) VALUES (%s, %s, %s, %s)
                          ON CONFLICT (owner_id, scope, name) DO UPDATE SET options = EXCLUDED.options
                          RETURNING id, name, scope, options, created_at""", (uid, name, scope, Jsonb(options)))
    return rows[0]


async def preset_delete(uid: str, preset_id: int) -> bool:
    return bool(await _rows("DELETE FROM convert_presets WHERE owner_id = %s AND id = %s RETURNING id", (uid, preset_id)))


# ---------- aláírások, adatprofil (titkosítva: app/formkit/crypto.py) ----------

async def signature_put(uid: str, sid: str, name: str, kind: str, nonce: bytes, data: bytes, width: int, height: int) -> None:
    await _rows("""INSERT INTO user_signatures (id, owner_id, name, kind, nonce, data, width, height)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""", (sid, uid, name, kind, nonce, data, width, height))


async def signature_list(uid: str) -> list[dict[str, Any]]:
    return await _rows("SELECT id, name, kind, width, height, created_at FROM user_signatures WHERE owner_id = %s "
                       "AND deleted_at IS NULL ORDER BY created_at DESC", (uid,))


async def signature_get(uid: str, sid: str) -> Optional[dict[str, Any]]:
    rows = await _rows("SELECT id, name, kind, nonce, data, width, height FROM user_signatures WHERE owner_id = %s AND id = %s "
                       "AND deleted_at IS NULL", (uid, sid))
    return rows[0] if rows else None


async def signature_delete(uid: str, sid: str) -> bool:
    """Végleges törlés (a titkosított adat is) — az aláírás érzékeny adat, nem tartjuk meg."""
    return bool(await _rows("DELETE FROM user_signatures WHERE owner_id = %s AND id = %s RETURNING id", (uid, sid)))


# ---------- ismétlődő táblázat-sablonok (jelenléti ív…), titkosítva: app/formkit/sheet_template.py ----------

async def sheet_template_put(uid: str, tid: str, name: str, nonce: bytes, data: bytes) -> None:
    await _rows("INSERT INTO sheet_templates (id, owner_id, name, nonce, data) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (tid, uid, name, nonce, data))


async def sheet_template_list(uid: str) -> list[dict[str, Any]]:
    return await _rows("SELECT id, name, created_at FROM sheet_templates WHERE owner_id = %s ORDER BY created_at DESC", (uid,))


async def sheet_template_get(uid: str, tid: str) -> Optional[dict[str, Any]]:
    rows = await _rows("SELECT id, name, nonce, data FROM sheet_templates WHERE owner_id = %s AND id = %s", (uid, tid))
    return rows[0] if rows else None


async def sheet_template_delete(uid: str, tid: str) -> bool:
    return bool(await _rows("DELETE FROM sheet_templates WHERE owner_id = %s AND id = %s RETURNING id", (uid, tid)))


async def profile_get(uid: str) -> Optional[dict[str, Any]]:
    rows = await _rows("SELECT nonce, data, updated_at FROM user_profiles WHERE owner_id = %s", (uid,))
    return rows[0] if rows else None


async def profile_put(uid: str, nonce: bytes, data: bytes) -> None:
    await _rows("""INSERT INTO user_profiles (owner_id, nonce, data) VALUES (%s, %s, %s)
                   ON CONFLICT (owner_id) DO UPDATE SET nonce = EXCLUDED.nonce, data = EXCLUDED.data, updated_at = now()
                   RETURNING owner_id""", (uid, nonce, data))


async def profile_delete(uid: str) -> bool:
    return bool(await _rows("DELETE FROM user_profiles WHERE owner_id = %s RETURNING owner_id", (uid,)))
