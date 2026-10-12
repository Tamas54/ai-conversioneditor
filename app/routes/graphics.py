"""Grafikai készítők a felületnek (a tördelés/újraépítés az app/tools-ban):

    POST /api/make/rebuild      {file_id, page?, ai_clean?, hint?}              kép/PDF-plakát → szerkeszthető grafika
    POST /api/make/infographic  {data_file_id | table_image_file_id, title?, brief?, source?, orientation?, accent?}
    POST /api/make/album        {photo_ids, manuscript_file_id?, title?, subtitle?, author?, size?, auto_captions?, cover?}

Az újraépítés és az infografika AI-t használ (látómodell / nyelvi modell) → fiók kell; az album AI nélkül vendégnek
is megy (csak az AI-képaláírás kér fiókot). Az eredmény mindig a LÁTHATÓ szerkesztőbe visz: .vectorcraft a
design editorba, .designcraft a layout editorba.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from app import events, tenancy

log = logging.getLogger("aice.graphics")
router = APIRouter()


def _need_account() -> None:
    if tenancy.is_guest():
        from app.auth import ACCOUNT_NEEDED
        raise HTTPException(401, ACCOUNT_NEEDED)


@router.post("/api/make/rebuild")
async def make_rebuild(body: dict[str, Any]) -> dict[str, Any]:
    from app.routes.make import _fail
    from app.tools import rebuild
    _need_account()
    if not body.get("file_id"):
        raise HTTPException(400, "Add the poster: a photo, screenshot or PDF of it.")
    try:
        out = await rebuild.rebuild(body["file_id"], page=int(body.get("page") or 1), ai_clean=bool(body.get("ai_clean")),
                                    hint=(body.get("hint") or None))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise _fail(e)
    events.emit("tool_done", tool="make_rebuild", result={"file_id": out["file_id"]})
    return out


@router.post("/api/make/infographic")
async def make_infographic(body: dict[str, Any]) -> dict[str, Any]:
    from app.routes.make import _fail
    from app.tools import infographic
    _need_account()
    try:
        out = await infographic.make(data_file_id=body.get("data_file_id"), table_image_file_id=body.get("table_image_file_id"),
                                     title=body.get("title"), brief=body.get("brief"), source=body.get("source"),
                                     orientation=body.get("orientation") or "portrait",
                                     theme={"accent": body["accent"]} if body.get("accent") else None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise _fail(e)
    events.emit("tool_done", tool="make_infographic", result={"file_id": out["file_id"]})
    return out


@router.post("/api/make/album")
async def make_album(body: dict[str, Any]) -> dict[str, Any]:
    from app.routes.make import _fail, _image_spec
    from app.tools import album
    if body.get("auto_captions"):
        _need_account()
    try:
        out = await album.make(list(body.get("photo_ids") or []), manuscript_file_id=body.get("manuscript_file_id"),
                               title=body.get("title"), subtitle=body.get("subtitle"), author=body.get("author"),
                               size=body.get("size"), captions=body.get("captions"), auto_captions=bool(body.get("auto_captions")),
                               cover=_image_spec(body.get("cover")),
                               theme={"accent": body["accent"]} if body.get("accent") else None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise _fail(e)
    events.emit("tool_done", tool="make_album", result={"file_id": out["file_id"]})
    return out
