"""Kézi szerkesztő — a WordCraft WASM-build (változatlan upstream release) kiszolgálása.

    /editor/                  — a WordCraft index.html, beszúrt aice_bridge.js-sel
    /editor/{asset}           — wasm/js (előtömörített .gz, ha a kliens kéri)
    POST /api/editor/save     — a szerkesztő Mentés/Exportálás bájtjai → új output file_id

A dashboard iframe-ben nyitja: /editor/?src=<signed url>&name=<fájlnév>&save=/api/editor/save?source=<file_id>
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app import events
from app.config import settings
from app.storage import input_path, output_path, public_url, save_bytes, write_meta
from app.storage.files import new_file_id
from app.tools.wordcraft import SAVE_FORMATS

router = APIRouter()

BRIDGE_JS = Path(__file__).parent.parent / "static" / "editor" / "aice_bridge.js"
_ASSET_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MEDIA = {".wasm": "application/wasm", ".js": "text/javascript", ".html": "text/html"}
_IMMUTABLE = "public, max-age=31536000, immutable"


def _web_dir() -> Path:
    d = settings.WORDCRAFT_WEB_DIR
    if not (d / "index.html").is_file():
        raise HTTPException(503, "a WordCraft web-build nincs telepítve (WORDCRAFT_WEB_DIR)")
    return d


def _now_hu() -> datetime:
    try:
        return datetime.now(ZoneInfo("Europe/Budapest"))
    except Exception:  # nincs tz-adatbázis a képben
        return datetime.now()


def editor_available() -> bool:
    return (settings.WORDCRAFT_WEB_DIR / "index.html").is_file()


@router.get("/editor", include_in_schema=False)
async def editor_redirect(request: Request):
    qs = request.url.query
    return RedirectResponse("/editor/" + (f"?{qs}" if qs else ""))


@router.get("/editor/", include_in_schema=False)
async def editor_index() -> HTMLResponse:
    html = (_web_dir() / "index.html").read_text("utf-8")
    tag = '<script src="./aice_bridge.js"></script>\n'
    # A bridge-nek a WASM-modul ELŐTT kell futnia (TrunkApplicationStarted-figyelő).
    marker = '<script type="module">'
    html = html.replace(marker, tag + marker, 1) if marker in html else html.replace("</head>", tag + "</head>", 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get("/editor/{asset}", include_in_schema=False)
async def editor_asset(asset: str, request: Request):
    if asset == "aice_bridge.js":
        return FileResponse(BRIDGE_JS, media_type="text/javascript", headers={"Cache-Control": "no-cache"})
    if not _ASSET_RE.match(asset) or asset.endswith(".gz"):
        raise HTTPException(404)
    d = _web_dir()
    p = d / asset
    if not p.is_file():
        raise HTTPException(404)
    suffix = p.suffix.lower()
    media = _MEDIA.get(suffix)
    headers = {"Cache-Control": _IMMUTABLE if suffix in (".wasm", ".js") else "no-cache", "Vary": "Accept-Encoding"}
    gz = d / f"{asset}.gz"
    if gz.is_file() and "gzip" in request.headers.get("accept-encoding", ""):
        return FileResponse(gz, media_type=media, headers={**headers, "Content-Encoding": "gzip"})
    return FileResponse(p, media_type=media, headers=headers)


@router.post("/api/editor/save")
async def editor_save(
    request: Request,
    name: str = Query(..., description="a szerkesztő által adott fájlnév"),
    source: Optional[str] = Query(None, description="a megnyitott eredeti file_id"),
) -> dict[str, Any]:
    data = await request.body()
    if not data:
        raise HTTPException(400, "üres törzs")
    if len(data) > settings.EDITOR_MAX_SAVE_MB * 1024 * 1024:
        raise HTTPException(413, f"nagyobb, mint {settings.EDITOR_MAX_SAVE_MB} MB")
    fname = Path(name).name or "document.docx"
    ext = Path(fname).suffix.lstrip(".").lower()
    if ext not in SAVE_FORMATS:
        raise HTTPException(400, f"nem várt formátum: .{ext}")
    if source and not (input_path(source).exists() or output_path(source).exists()):
        source = None

    fid = new_file_id(ext)
    await save_bytes(output_path(fid), data)
    stamp = _now_hu().strftime("%H:%M")
    label = f"{Path(fname).stem} (szerk. {stamp}).{ext}"
    write_meta(fid, label=label, source_file_id=source, operation="kézi szerkesztés", original_filename=fname)
    result = {"file_id": fid, "url": public_url(fid), "size_kb": len(data) // 1024, "label": label}
    events.emit("tool_done", tool="editor_save", params={"source": source, "name": fname}, result=result)
    return result
