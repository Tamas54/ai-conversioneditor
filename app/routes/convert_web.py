"""A weboldal konverter-widgetje és eszközei mögötti végpontok (böngészőből, vendégként is).

    POST /api/convert/run   {file_id, target, options?, preset_id?}        → háttérmunka (GET /api/jobs/{id})
    POST /api/tools/run     {tool, file_ids, options?, preset_id?}         → háttérmunka
    GET/POST/DELETE /api/presets                                            mentett beállítások (fiókhoz)
    GET  /api/units/convert, /api/timezones/convert                          átváltók (nyilvános)
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from app import events, jobs, tenancy
from app.convert.options import OptionError

log = logging.getLogger("aice.web")
router = APIRouter()


def _bad(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))


async def _preset_options(preset_id: Optional[int], options: Optional[dict]) -> dict[str, Any]:
    opts = dict(options or {})
    if preset_id:
        from app import db
        if not db.enabled() or tenancy.is_guest():
            raise HTTPException(401, "Presets need an account.")
        row = await db.preset_get(tenancy.current_user_id(), int(preset_id))
        if not row:
            raise HTTPException(404, "No such preset.")
        opts = {**row["options"], **opts}
    return opts


@router.post("/api/convert/run")
async def convert_run(body: dict[str, Any]) -> dict[str, Any]:
    from app.convert import service
    from app.convert.options import validate
    from app.tools import crafts
    fid, target = body.get("file_id"), (body.get("target") or "").strip().lower().lstrip(".")
    if not fid or not target:
        raise HTTPException(400, "Choose a file and a format.")
    try:
        crafts.existing(fid)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "File not found.")
    opts = await _preset_options(body.get("preset_id"), body.get("options"))
    try:
        validate({k: v for k, v in opts.items() if k not in ("output", "filename")})
    except OptionError as e:
        raise _bad(e)

    async def work(progress):
        progress(0, 1, "converting")
        try:
            res = await service.convert_file(fid, target, opts)
        except (OptionError, service.ConversionError, FileNotFoundError) as e:
            raise HTTPException(400, str(e))
        progress(1, 1, "done")
        events.emit("tool_done", tool="convert_run", params={"target": target}, result={"file_id": res["file_id"]})
        return res
    return jobs.start("convert", work)


@router.post("/api/tools/run")
async def tools_run(body: dict[str, Any]) -> dict[str, Any]:
    from app import ops
    from app.catalog import tools as T
    key = body.get("tool") or ""
    tool = T.get(key)
    if not tool or not T.available(key):
        raise HTTPException(404, "No such tool.")
    if tool.ai and tenancy.is_guest():
        from app.auth import ACCOUNT_NEEDED
        raise HTTPException(401, ACCOUNT_NEEDED)
    ids = [i for i in body.get("file_ids") or ([body["file_id"]] if body.get("file_id") else []) if i]
    opts = await _preset_options(body.get("preset_id"), body.get("options"))
    try:
        ops.check_tool_call(tool, ids, opts)
    except (OptionError, ValueError) as e:
        raise _bad(e)

    async def work(progress):
        progress(0, 1, tool.key)
        try:
            res = await ops.run_tool(tool.key, ids, opts)
        except (OptionError, ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e))
        progress(1, 1, "done")
        events.emit("tool_done", tool=f"tool:{tool.key}", result={"files": [f["file_id"] for f in res.get("files", [])]})
        return res
    return jobs.start(f"tool:{tool.key}", work)


# ---------- mentett beállítások ----------

def _db():
    from app import db
    if not db.enabled():
        raise HTTPException(503, "Presets need the database.")
    if tenancy.is_guest():
        raise HTTPException(401, "Presets need an account.")
    return db


@router.get("/api/presets")
async def presets_list(scope: Optional[str] = None) -> dict[str, Any]:
    from app import db
    if not db.enabled() or tenancy.is_guest():
        return {"presets": []}
    return {"presets": await db.preset_list(tenancy.current_user_id(), scope)}


@router.post("/api/presets")
async def presets_save(body: dict[str, Any]) -> dict[str, Any]:
    from app.convert.options import validate
    db = _db()
    name = (body.get("name") or "").strip()[:80]
    scope = (body.get("scope") or "").strip().lower()[:40]
    if not name or not scope:
        raise HTTPException(400, "Give the preset a name and a scope (a target format or a tool).")
    try:
        opts = validate(body.get("options") or {}, tool="/" in scope or "-" in scope)
    except OptionError as e:
        raise _bad(e)
    return await db.preset_save(tenancy.current_user_id(), name, scope, opts)


@router.delete("/api/presets/{preset_id}")
async def presets_delete(preset_id: int) -> dict[str, Any]:
    db = _db()
    if not await db.preset_delete(tenancy.current_user_id(), preset_id):
        raise HTTPException(404, "No such preset.")
    return {"deleted": preset_id}


# ---------- átváltók ----------

@router.get("/api/units/convert")
async def units_convert(value: float, from_unit: str = Query(..., alias="from"), to_unit: str = Query(..., alias="to")) -> dict[str, Any]:
    from app.catalog import units as U
    try:
        return {"value": value, "from": from_unit, "to": to_unit, "result": U.convert(value, from_unit, to_unit)}
    except U.UnitError as e:
        raise _bad(e)


@router.get("/api/timezones/convert")
async def timezones_convert(from_zone: str = Query(..., alias="from"), to: str = "UTC", time: Optional[str] = None) -> dict[str, Any]:
    from app.catalog import units as U
    try:
        return U.tz_convert(time, from_zone, [z for z in to.split(",") if z.strip()])
    except U.UnitError as e:
        raise _bad(e)
