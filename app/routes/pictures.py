"""Képválasztó-végpontok: stock-jelöltek (szűrés nélkül), a fájl képei, egy kép cseréje (új verzió).

    GET  /api/images/search?q=&n=          → {candidates: [{thumb_url, url, credit, title, width, height, source}]}
    GET  /api/pictures?file_id=            → {pictures: [{slot, where, query?, prompt?, credit?}]}
    GET  /api/pictures/img?file_id=&slot=  → a kép kicsiben (JPEG)
    POST /api/make/picture {file_id, slot, pick: {url, credit, query?} | {ai: prompt} | {file_id} | {stock}}
                                           → új verzió {file_id, url, label, where, slot}
Vendégnek is megy (stock, saját kép); AI-kép csak fiókkal (kredit).
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from app import events, tenancy

log = logging.getLogger("aice.pictures")
router = APIRouter()


@router.get("/api/images/search")
async def images_search(q: str = "", n: int = 12) -> dict[str, Any]:
    from app.tools import images
    q = q.strip()
    if not q:
        return {"candidates": []}
    return {"candidates": await images.search(q, max(1, min(30, n)))}


@router.get("/api/pictures")
async def pictures_list(file_id: str) -> dict[str, Any]:
    from app.tools import pictures
    try:
        return {"pictures": await pictures.list_pictures(file_id)}
    except FileNotFoundError:
        raise HTTPException(404, "This file is gone.")


@router.get("/api/pictures/img", include_in_schema=False)
async def pictures_img(file_id: str, slot: str) -> Response:
    from app.tools import pictures
    try:
        data = await pictures.picture_image(file_id, slot)
    except (FileNotFoundError, KeyError):
        raise HTTPException(404)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=600"})


@router.post("/api/make/picture")
async def make_picture(body: dict[str, Any]):
    from app.routes.make import _fail
    from app.tools import pictures
    fid, slot, pick = body.get("file_id"), body.get("slot"), body.get("pick") or {}
    if not fid or not slot:
        raise HTTPException(400, "Choose the picture to change.")
    if pick.get("ai") and tenancy.is_guest():
        from app.auth import ACCOUNT_NEEDED
        return JSONResponse({"detail": ACCOUNT_NEEDED, "account_required": True}, status_code=401)
    try:
        res = await pictures.replace(fid, slot, pick)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise _fail(e)
    events.emit("tool_done", tool="make_picture", params={"slot": slot}, result={"file_id": res["file_id"]})
    return res
