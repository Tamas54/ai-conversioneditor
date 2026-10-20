"""AI-videó végpontok (a logika: app/tools/ai_video.py; a szolgáltató: app/providers).

    POST /webhooks/kie?token=…     a kie.ai callbackje — idempotens; a token (KIE_WEBHOOK_SECRET) és/vagy a kie HMAC-aláírása
                                   (KIE_WEBHOOK_HMAC_KEY) kell; a taskId a saját nyilvántartásunkkal egyeztetve
    GET  /api/ai/task/{id}         egy AI-feladat állapota (a hívó saját feladata)
    DELETE /api/ai/task/{id}       megszakítás (a foglalt kredit visszajár)
    GET  /api/ai/config            be van-e kapcsolva, választható értékek
    POST /api/ai/estimate          ár (Studio-kredit) — a gombon
    POST /api/ai/enhance           Topaz minőségjavítás → job (új változat)
    POST /api/ai/clip              Seedance klip → job (draft, véglegesítés, a timeline-ra: at / after_scene)
    GET  /api/ai/draft-of          egy fájl AI-feladata (a draft véglegesítéséhez)
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


class _NoWebhookToken(logging.Filter):
    """A /webhooks/kie?token=… titka ne kerüljön a uvicorn hozzáférési naplójába (a kérés sora: args[2])."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "token=" in args[2]:
            import re
            record.args = (*args[:2], re.sub(r"token=[^&\s]*", "token=***", args[2]), *args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(_NoWebhookToken())


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


@router.get("/api/ai/config")
async def ai_config() -> dict[str, Any]:
    """Be van-e kapcsolva (a felület ennek alapján mutatja az AI-videó gombokat), és a választható értékek."""
    from app import providers
    from app.tools import ai_video
    return {"enabled": providers.enabled_for() and not tenancy.is_guest(), "resolutions": list(ai_video.RESOLUTIONS),
            "aspects": list(ai_video.ASPECTS), "duration": [ai_video.CLIP_MIN_S, ai_video.CLIP_MAX_S],
            "factors": list(ai_video.FACTORS)}


@router.post("/api/ai/estimate")
async def ai_estimate(body: dict[str, Any]) -> dict[str, Any]:
    """{action: enhance, file_id, factor} | {action: ai_clip, prompt, resolution, duration, …} → {credits, what, balance}."""
    from app import credits
    from app.tools import ai_video
    try:
        ai_video.require_enabled()
        if (body.get("action") or "") == "enhance":
            est = await ai_video.estimate_enhance(await ai_video._source_video(body.get("file_id") or ""), body.get("factor"))
        else:
            est = await ai_video.estimate_clip(body)
    except Exception as e:
        raise _fail(e)
    return {**credits.public_estimate(est), "balance": await credits.balance(tenancy.current_user_id())}


@router.post("/api/ai/enhance")
async def ai_enhance(body: dict[str, Any]) -> dict[str, Any]:
    """{file_id: videó vagy videóprojekt, factor: 1 | 2 | 4} → job (az eredmény a fájl új változata)."""
    from app.tools import ai_video
    try:
        return await ai_video.start_enhance(body.get("file_id") or "", body.get("factor") or 2)
    except Exception as e:
        raise _fail(e)


@router.post("/api/ai/clip")
async def ai_clip(body: dict[str, Any]) -> dict[str, Any]:
    """{prompt, resolution?, duration?, audio?, aspect?, draft?, image? | first_frame? (+ last_frame?) — file_id vagy
    {frame: mp} a file_id videójából —, reference_*?, finalize?: draft task_id, file_id?: a projekt, at? | after_scene?} → job."""
    from app.tools import ai_video
    try:
        return await ai_video.start_clip({k: v for k, v in body.items() if k != "file_id"}, project=body.get("file_id"))
    except Exception as e:
        raise _fail(e)


@router.get("/api/ai/draft-of")
async def ai_task_of_file(file_id: str) -> dict[str, Any]:
    from app.tools import ai_video
    try:
        return await ai_video.task_of_file(file_id)
    except Exception as e:
        raise _fail(e)
