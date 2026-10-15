"""A weboldal konverter-widgetje és eszközei mögötti végpontok (böngészőből, vendégként is).

    POST /api/convert/run   {file_id, target, options?, preset_id?}        → háttérmunka (GET /api/jobs/{id})
    POST /api/tools/run     {tool, file_ids, options?, preset_id?}         → háttérmunka
    GET/POST/DELETE /api/presets                                            mentett beállítások (fiókhoz)
    GET  /api/units/convert, /api/timezones/convert                          átváltók (nyilvános)
"""
from __future__ import annotations

import functools

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

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


@router.get("/api/catalog")
async def catalog() -> dict[str, Any]:
    """Az eszközök és formátumok gépi leírása (a WebMCP-eszközök és a böngészős ügynökök ebből dolgoznak)."""
    return _catalog()


@functools.lru_cache(maxsize=1)
def _catalog() -> dict[str, Any]:
    from app.catalog import formats as F
    from app.catalog import tools as T
    from app.convert.options import OPTIONS, TOOL_OPTIONS
    from app.site import i18n
    tools = []
    for key, tool in T.TOOLS.items():
        if not T.available(key, static=True):
            continue
        opts = {}
        for k in tool.options:
            o = TOOL_OPTIONS.get(k) or OPTIONS.get(k)
            if o is not None:
                opts[k] = {**o.schema(), **({"default": o.default} if o.default is not None else {})}
        tools.append({"key": key, "name": i18n.raw("en", f"tool.{key}.name", key.replace("-", " ").capitalize()),
                      "description": i18n.raw("en", f"tool.{key}.desc", ""), "group": tool.group,
                      "accepts": list(tool.accepts), "min_files": tool.min_files, "max_files": tool.max_files,
                      "output": tool.output or "same as input", "ai": tool.ai, "options": opts})
    cats: dict[str, int] = {}
    for f in F.FORMATS.values():
        cats[f.category] = cats.get(f.category, 0) + 1
    return {"formats": len(F.FORMATS), "categories": cats, "tools": tools}


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


# ---------------------------------------------------------------- import: URL, felhő; darabolt feltöltés

def _max_upload() -> int:
    import os
    from app.auth import GUEST_MAX_UPLOAD_MB
    if tenancy.is_guest():
        return GUEST_MAX_UPLOAD_MB * 1024 * 1024
    return int(os.getenv("API_MAX_FILE_MB", "2048")) * 1024 * 1024


def _store(src, filename: str, operation: str) -> dict[str, Any]:
    import os
    from app.api_v1.operations import _save_named
    from app.storage import public_url
    size = os.path.getsize(src)
    fid = _save_named(src, filename)
    from app.storage import write_meta
    write_meta(fid, operation=operation)
    events.emit("uploaded", file_id=fid, original_name=filename, size_kb=size // 1024)
    return {"file_id": fid, "size_kb": size // 1024, "original_name": filename, "url": public_url(fid)}


@router.post("/api/import")
async def import_remote(body: dict[str, Any]) -> dict[str, Any]:
    """Fájl behozása a munkaterületre: nyilvános URL (a Dropbox Chooser és a OneDrive-választó közvetlen
    letöltési linkje is ilyen), vagy Google Drive (a Google Picker fájl-azonosítója + a böngészőben kapott
    hozzáférési token). A token csak erre a hívásra él, nem tároljuk. Minden cím SSRF-ellenőrzésen megy át."""
    import shutil
    from pathlib import Path
    from app import net
    from app.storage import temp_path
    from app.storage.files import new_file_id
    provider = str(body.get("provider") or "url")
    tmp = temp_path(new_file_id("imp"))
    tmp.mkdir(parents=True)
    try:
        if provider == "url":
            url = str(body.get("url") or "")
            if not url.startswith(("http://", "https://")):
                raise HTTPException(400, "url: http(s) link required")
            try:
                name, _, _ = await net.download(url, tmp / "dl", _max_upload())
            except net.UnsafeURL as e:
                raise HTTPException(400, f"This address isn't allowed: {e}")
            except ValueError as e:
                raise HTTPException(400, str(e))
            src = tmp / "dl"
            name = str(body.get("name") or name or "download")
        elif provider in ("google-drive", "dropbox", "onedrive"):
            from app import connectors
            spec = {k: body[k] for k in ("file_id", "path", "item_id", "access_token") if body.get(k)}
            try:
                src, name = await connectors.fetch(provider, spec, _max_upload(), tmp)
            except ValueError as e:
                raise HTTPException(400, f"{provider}: {e}")
            name = str(body.get("name") or name)
        else:
            raise HTTPException(400, "provider: url, google-drive, dropbox or onedrive")
        return _store(src, Path(name).name[:180] or "file", f"import {provider}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_CHUNK = 8 * 1024 * 1024


def _chunk_dir(upload_id: str):
    import re
    from app.storage import temp_path
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", upload_id):
        raise HTTPException(404, "No such upload.")
    d = temp_path(f"chunks-{upload_id}")
    meta = d / "meta.json"
    if not meta.exists():
        raise HTTPException(404, "No such upload.")
    import json
    m = json.loads(meta.read_text("utf-8"))
    if m["owner"] != str(tenancy.current_user_id()):
        raise HTTPException(404, "No such upload.")
    return d, m


@router.post("/api/upload/chunks")
async def chunks_start(body: dict[str, Any]) -> dict[str, Any]:
    """Nagy fájl darabokban: start → PUT /api/upload/chunks/{id}/{n} (8 MB-os darabok) → complete."""
    import json
    import secrets
    from app.storage import temp_path
    name = str(body.get("filename") or "").strip()
    size = int(body.get("size") or 0)
    if not name or size <= 0:
        raise HTTPException(400, "filename and size are required")
    if size > _max_upload():
        raise HTTPException(413, f"Files up to {_max_upload() // (1024 * 1024)} MB" + (" without an account." if tenancy.is_guest() else "."))
    uid = secrets.token_urlsafe(18)
    d = temp_path(f"chunks-{uid}")
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"owner": str(tenancy.current_user_id()), "name": name[:180], "size": size}), "utf-8")
    return {"upload_id": uid, "chunk_size": _CHUNK, "chunks": -(-size // _CHUNK)}


@router.put("/api/upload/chunks/{upload_id}/{index}")
async def chunks_put(upload_id: str, index: int, request: Request) -> dict[str, Any]:
    d, m = _chunk_dir(upload_id)
    if not 0 <= index < -(-m["size"] // _CHUNK):
        raise HTTPException(400, "chunk index out of range")
    n = 0
    with open(d / f"{index:06d}.part", "wb") as f:
        async for block in request.stream():
            n += len(block)
            if n > _CHUNK:
                raise HTTPException(413, f"a chunk is at most {_CHUNK} bytes")
            f.write(block)
    return {"index": index, "bytes": n}


@router.post("/api/upload/chunks/{upload_id}/complete")
async def chunks_complete(upload_id: str) -> dict[str, Any]:
    import shutil
    d, m = _chunk_dir(upload_id)
    parts = sorted(d.glob("*.part"))
    total = -(-m["size"] // _CHUNK)
    if len(parts) != total:
        raise HTTPException(400, f"{total - len(parts)} chunk(s) missing")
    out = d / "joined"
    with open(out, "wb") as f:
        for p in parts:
            with open(p, "rb") as src:
                shutil.copyfileobj(src, f, 1 << 20)
    if out.stat().st_size != m["size"]:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, "size mismatch — upload again")
    try:
        return _store(out, m["name"], "upload")
    finally:
        shutil.rmtree(d, ignore_errors=True)


@router.get("/api/import/config")
async def import_config() -> dict[str, Any]:
    """A böngészős fájlválasztók (Google Picker, Dropbox Chooser, OneDrive) nyilvános azonosítói az env-ből —
    amelyik nincs beállítva, annak a gombja nem jelenik meg."""
    import os
    return {"google": {"client_id": os.getenv("GOOGLE_PICKER_CLIENT_ID", ""), "api_key": os.getenv("GOOGLE_PICKER_API_KEY", ""),
                       "app_id": os.getenv("GOOGLE_PICKER_APP_ID", "")} if os.getenv("GOOGLE_PICKER_CLIENT_ID") else None,
            "dropbox": {"app_key": os.getenv("DROPBOX_APP_KEY")} if os.getenv("DROPBOX_APP_KEY") else None,
            "onedrive": {"client_id": os.getenv("ONEDRIVE_CLIENT_ID")} if os.getenv("ONEDRIVE_CLIENT_ID") else None,
            "chunk_size": _CHUNK, "max_mb": _max_upload() // (1024 * 1024)}


@router.post("/api/export")
async def export_cloud(body: dict[str, Any]) -> dict[str, Any]:
    """Eredmény mentése a felhőbe a böngészőben kapott hozzáférési tokennel (Google Drive, Dropbox, OneDrive).
    A token csak erre a hívásra él; a fájlok a hívó saját munkaterületéről jönnek."""
    from app import connectors
    from app.tools import crafts
    provider = str(body.get("provider") or "")
    if provider not in ("google-drive", "dropbox", "onedrive"):
        raise HTTPException(400, "provider: google-drive, dropbox or onedrive")
    ids = [str(i) for i in (body.get("file_ids") or [])][:50]
    if not ids:
        raise HTTPException(400, "file_ids required")
    for fid in ids:
        try:
            crafts.existing(fid)
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, f"file not found: {fid}")
    spec = {k: body[k] for k in ("access_token", "path", "folder_id") if body.get(k)}
    if provider == "dropbox" and not spec.get("path"):
        spec["path"] = "/"
    try:
        return {"saved": await connectors.push(provider, spec, ids)}
    except ValueError as e:
        raise HTTPException(400, f"{provider}: {e}")
