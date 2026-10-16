"""Webhookok: job-szintű (webhook_url) és fiók-szintű (/v1/webhooks) értesítések job.finished / job.failed eseményre.

Aláírás: X-Signature: sha256=<HMAC-SHA256(titok, f"{időbélyeg}.{törzs}")>, X-Webhook-Timestamp: <unix mp>.
A titok felhasználónként állandó (GET /v1/me → webhook_secret). Sikertelen kézbesítés: 3 újrapróba (2, 10, 60 mp).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Optional

from app.config import settings

log = logging.getLogger("aice.api.webhooks")
EVENTS = ("job.created", "job.finished", "job.failed")
_RETRIES = (2, 10, 60)


def secret(uid: str) -> str:
    return "whsec_" + hmac.new(settings.SESSION_SECRET.encode(), f"webhook|{uid}".encode(), hashlib.sha256).hexdigest()[:40]


def sign(sec: str, ts: int, body: bytes) -> str:
    return "sha256=" + hmac.new(sec.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


async def _send(url: str, payload: dict[str, Any], uid: str, hook_id: Optional[int] = None) -> None:
    from app import net
    body = json.dumps(payload, ensure_ascii=False, default=str).encode()
    for i, delay in enumerate((0,) + _RETRIES):
        if delay:
            await asyncio.sleep(delay)
        ts = int(time.time())
        status, err = None, None
        try:
            status = await net.post_json(url, body, {"X-Signature": sign(secret(uid), ts, body), "X-Webhook-Timestamp": str(ts),
                                                     "X-Webhook-Event": payload["event"], "User-Agent": f"{settings.PRODUCT_NAME}-webhooks/1"})
        except Exception as e:
            err = str(e)[:300]
        await _record(hook_id, status, err)
        if status and 200 <= status < 300:
            return
        log.info("webhook %s attempt %d failed: %s %s", url, i + 1, status, err)


async def _record(hook_id: Optional[int], status: Optional[int], err: Optional[str]) -> None:
    if hook_id is None:
        return
    from app import db
    if not db.enabled():
        return
    try:
        await db._rows("UPDATE api_webhooks SET last_status = %s, last_error = %s, last_at = now() WHERE id = %s",
                       (status, err, hook_id))
    except Exception:
        pass


async def deliver_job(job, event: str) -> None:
    payload = {"event": event, "job": job.public()}
    targets: list[tuple[str, Optional[int]]] = []
    if job.webhook_url:
        targets.append((job.webhook_url, None))
    for h in await list_hooks(job.owner):
        if event in (h.get("events") or []):
            targets.append((h["url"], h["id"]))
    await asyncio.gather(*(_send(u, payload, job.owner, hid) for u, hid in targets), return_exceptions=True)


# ---------- fiókszintű webhookok (memória-tartalék DB nélkül) ----------

_MEM: dict[str, list[dict[str, Any]]] = {}
_next = 1


async def list_hooks(uid: str) -> list[dict[str, Any]]:
    from app import db
    if db.enabled():
        try:
            return await db._rows("SELECT id, url, events, created_at, last_status, last_error, last_at FROM api_webhooks "
                                  "WHERE owner_id = %s AND disabled_at IS NULL ORDER BY id", (uid,))
        except Exception:
            return []
    return list(_MEM.get(uid, []))


async def create_hook(uid: str, url: str, events: list[str]) -> dict[str, Any]:
    global _next
    from app import db, net
    await net.check_url(url)
    evs = [e for e in events if e in EVENTS] or ["job.finished", "job.failed"]
    if db.enabled():
        from psycopg.types.json import Jsonb
        rows = await db._rows("INSERT INTO api_webhooks (owner_id, url, events) VALUES (%s, %s, %s) "
                              "RETURNING id, url, events, created_at", (uid, url, Jsonb(evs)))
        return rows[0]
    h = {"id": _next, "url": url, "events": evs}
    _next += 1
    _MEM.setdefault(uid, []).append(h)
    return h


async def delete_hook(uid: str, hook_id: int) -> bool:
    from app import db
    if db.enabled():
        return bool(await db._rows("UPDATE api_webhooks SET disabled_at = now() WHERE id = %s AND owner_id = %s "
                                   "AND disabled_at IS NULL RETURNING id", (hook_id, uid)))
    before = len(_MEM.get(uid, []))
    _MEM[uid] = [h for h in _MEM.get(uid, []) if h["id"] != hook_id]
    return len(_MEM[uid]) < before
