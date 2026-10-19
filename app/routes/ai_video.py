"""AI-videó végpontok (a logika: app/tools/ai_video.py; a szolgáltató: app/providers).

    POST /webhooks/kie?token=…     a kie.ai callbackje — idempotens; a token (KIE_WEBHOOK_SECRET) és/vagy a kie HMAC-aláírása
                                   (KIE_WEBHOOK_HMAC_KEY) kell; a taskId a saját nyilvántartásunkkal egyeztetve
    GET  /api/ai/task/{id}         egy AI-feladat állapota (a hívó saját feladata)
    DELETE /api/ai/task/{id}       megszakítás (a foglalt kredit visszajár)
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app import tenancy

router = APIRouter()
log = logging.getLogger("aice.ai_video.routes")


def _fail(e: Exception) -> HTTPException:
    from app import db
    from app.tools import ai_video
    if isinstance(e, HTTPException):
        return e
    if isinstance(e, db.InsufficientCredits):
        return HTTPException(402, {"error": "insufficient_credits", "needed": e.needed, "available": e.available,
                                   "message": f"This needs {e.needed} credits — you have {e.available}. Add credits in Plans."})
    if isinstance(e, PermissionError):
        return HTTPException(401, str(e))
    if isinstance(e, (ai_video.AIVideoError, ValueError, FileNotFoundError)):
        return HTTPException(400, str(e))
    log.exception("AI video failed")
    return HTTPException(500, "Something went wrong with the AI video job.")


@router.post("/webhooks/kie", include_in_schema=False)
async def kie_webhook(request: Request, token: str = "") -> dict[str, Any]:
    from app.providers import kie
    from app.tools import ai_video
    secret, hkey = os.getenv("KIE_WEBHOOK_SECRET", ""), os.getenv("KIE_WEBHOOK_HMAC_KEY", "")
    if not secret and not hkey:
        raise HTTPException(404, "not found")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "bad body")
    data = (body or {}).get("data") or {}
    tid = str(data.get("taskId") or data.get("task_id") or "")
    if secret and not hmac.compare_digest(token.encode(), secret.encode()):
        raise HTTPException(403, "forbidden")
    if hkey and not kie.verify_signature(tid, request.headers.get("x-webhook-timestamp", ""),
                                         request.headers.get("x-webhook-signature", ""), hkey):
        raise HTTPException(403, "bad signature")
    if not tid:
        raise HTTPException(400, "no taskId")
    try:
        result = await ai_video.on_webhook(tid)
    except Exception as e:                 # a kie újrapróbálkozhat; a lekérdezés úgyis befejezi
        log.warning("kie webhook %s: %s", tid, e)
        raise HTTPException(503, "try again")
    log.info("kie webhook %s → %s", tid, result)
    return {"ok": True, "result": result}


@router.get("/api/ai/task/{op}")
async def task_status(op: str) -> dict[str, Any]:
    from app.tools import ai_video
    try:
        return await ai_video.get(op)
    except Exception as e:
        raise _fail(e)


@router.delete("/api/ai/task/{op}")
async def task_cancel(op: str) -> dict[str, Any]:
    from app.tools import ai_video
    try:
        return await ai_video.cancel(op)
    except Exception as e:
        raise _fail(e)


def _uid() -> str:
    return tenancy.current_user_id()
