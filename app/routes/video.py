"""Videó végpontok (a logika: app/tools/video.py).

    POST /api/make/video           automata videó (cikk / URL / dokumentum / saját szöveg) → job; mode=new → üres projekt
    POST /api/video/render         egy videóprojekt (.ecproj) renderelése MP4-re → job
    GET  /api/video/assets         a projekt médiafájljai (név + letöltési link) — a böngészős szerkesztőnek (bridge)
    POST /api/video/asset          a böngészőben a projekthez adott médiafájl feltöltése (mentés után a bridge küldi)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from app import events, tenancy
from app.config import settings
from app.storage import output_path, public_url, temp_path

router = APIRouter()
log = logging.getLogger("aice.video.routes")


def _fail(e: Exception) -> HTTPException:
    if isinstance(e, HTTPException):
        return e
    if isinstance(e, (ValueError, FileNotFoundError)):
        return HTTPException(400, str(e))
    from app.routes.make import _fail as make_fail
    return make_fail(e)


@router.post("/api/make/video")
async def make_video(body: dict[str, Any]) -> dict[str, Any]:
    """{mode: article|text|new, text?, url?, source_file_id?, format: vertical|landscape|square, duration?, images?,
    pictures?: stock|mine|none, music_file_id?, voice_file_id?, accent?, language?}"""
    from app import jobs
    from app.tools import video
    mode = (body.get("mode") or "article").lower()
    if mode == "new":
        try:
            out = await video.blank(body.get("format"), body.get("duration"))
        except Exception as e:
            raise _fail(e)
        events.emit("tool_done", tool="make_video", params={"mode": "new"}, result={"file_id": out["file_id"]})
        return out
    if tenancy.is_guest():
        from app.auth import ACCOUNT_NEEDED
        raise HTTPException(401, ACCOUNT_NEEDED)
    if not video.available():
        raise HTTPException(503, "The video engine isn't installed on this server.")
    try:
        video.fmt_name(body.get("format"))
    except ValueError as e:
        raise HTTPException(400, str(e))

    async def work(progress):
        try:
            res = await video.make(body, progress)
        except Exception as e:
            raise _fail(e)
        events.emit("tool_done", tool="make_video", params={"mode": mode},
                    result={"files": [f["file_id"] for f in res.get("files") or []]})
        return res
    return jobs.start("video", work)


@router.post("/api/video/render")
async def render_video(body: dict[str, Any]) -> dict[str, Any]:
    """{file_id: <.ecproj>, format?: mp4|webm|gif, draft?: bool} → job (result: a renderelt fájl)."""
    from app import jobs
    from app.tools import video
    fid = body.get("file_id") or ""
    if not fid.lower().endswith(".ecproj"):
        raise HTTPException(400, "Pick a video project (.ecproj) to render.")

    async def work(progress):
        progress(0, 1, "rendering the video")
        try:
            res = await video.render(fid, (body.get("format") or "mp4").lower(), bool(body.get("draft")))
        except Exception as e:
            raise _fail(e)
        events.emit("tool_done", tool="render_video", params={"file_id": fid}, result={"file_id": res["file_id"]})
        return res
    return jobs.start("render", work)


@router.get("/api/video/assets")
async def video_assets(file_id: str) -> dict[str, Any]:
    from app.tools import video
    if not file_id.lower().endswith(".ecproj"):
        raise HTTPException(400, "not a video project")
    out = []
    for name, fid in video.assets_of(file_id).items():
        if output_path(fid).exists():
            u = public_url(fid)
            out.append({"name": name, "url": u[u.index("/files/"):] if "/files/" in u else u})
    return {"assets": out}


@router.post("/api/video/asset")
async def video_asset(request: Request, project: str = Query(...), name: str = Query(...)) -> dict[str, Any]:
    from app.tools import video
    if not project.lower().endswith(".ecproj") or not output_path(project).exists():
        raise HTTPException(404, "no such video project")
    data = await request.body()
    if not data:
        raise HTTPException(400, "Empty file.")
    if len(data) > settings.EDITOR_MAX_SAVE_MB * 1024 * 1024:
        raise HTTPException(413, f"larger than {settings.EDITOR_MAX_SAVE_MB} MB")
    tmp = temp_path()
    tmp.write_bytes(data)
    try:
        fid = video.add_asset(project, name, Path(tmp))
    finally:
        tmp.unlink(missing_ok=True)
    return {"file_id": fid, "name": video._asset_name(name)}
