"""
Dashboard routes — HTML UI + REST APIs that wrap the MCP tools.

The dashboard talks to these endpoints (not /mcp), so it can be used by
a human directly in the browser. Same business logic as the MCP tools,
just wrapped in plain HTTP.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

from app import events
from app.config import settings
from app.brands import get_brand, list_brands
from app.brands.renderers import (
    pptx_renderer as brand_pptx_renderer,
    docx_renderer as brand_docx_renderer,
    html_renderer as brand_html_renderer,
)
from app.tools.convert import convert as t_convert
from app.llm.agent import run_agent
from app.tools import vision_ops
from app.tools.formats import detect_from_path
from app.storage import (
    input_path, new_file_id, output_path, save_bytes, public_url,
    write_meta, read_meta, delete_meta, display_label, is_meta_path,
)
from app.tools import (
    convert as t_convert,
    delete_pages as t_delete_pages,
    delete_section as t_delete_section,
    edit_with_instruction as t_edit_ai,
    extract as t_extract,
    find_replace as t_find_replace,
    insert_after as t_insert_after,
    merge as t_merge,
    replace_heading as t_replace_heading,
)

log = logging.getLogger("aice.dashboard")
router = APIRouter()

DASHBOARD_HTML = Path(__file__).parent.parent / "templates" / "dashboard.html"


def _pool(request: Request):
    p = getattr(request.app.state, "pool", None)
    if not p:
        raise HTTPException(503, "worker pool not ready")
    return p


@router.get("/", include_in_schema=False)
async def root() -> HTMLResponse:
    return HTMLResponse(
        '<meta http-equiv="refresh" content="0; url=/dashboard">'
    )


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(
        DASHBOARD_HTML.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


# ----- File listing -----


def _list_dir(d: Path, kind: str, include_ephemeral: bool = False) -> list[dict[str, Any]]:
    if not d.exists():
        return []
    items = []
    for p in d.iterdir():
        if not p.is_file():
            continue
        if is_meta_path(p.name):
            continue  # sidecar metadata; not a user-visible file
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        meta = read_meta(p.name) or {}
        if meta.get("ephemeral") and not include_ephemeral:
            continue  # intermediate file from an agent run; hide by default
        items.append({
            "file_id": p.name,
            "kind": kind,
            "size_kb": st.st_size // 1024,
            "size_bytes": st.st_size,
            "mtime": st.st_mtime,
            "ext": p.suffix.lstrip(".").lower(),
            "url": public_url(p.name),
            "label": display_label(p.name, meta),
            "source_file_id": meta.get("source_file_id"),
            "source_label": (display_label(meta["source_file_id"])
                             if meta.get("source_file_id") else None),
            "operation": meta.get("operation"),
            "original_filename": meta.get("original_filename"),
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


@router.get("/api/files")
async def api_files(include_ephemeral: bool = False) -> dict[str, Any]:
    return {
        "inputs": _list_dir(
            settings.DATA_DIR / "inputs", "input", include_ephemeral=include_ephemeral,
        ),
        "outputs": _list_dir(
            settings.DATA_DIR / "outputs", "output", include_ephemeral=include_ephemeral,
        ),
    }


@router.delete("/api/files/{file_id}")
async def api_delete_file(file_id: str) -> dict[str, Any]:
    removed = []
    for d in (settings.DATA_DIR / "inputs", settings.DATA_DIR / "outputs"):
        p = d / file_id
        if p.exists():
            p.unlink()
            removed.append(str(p))
    if not removed:
        raise HTTPException(404, "not found")
    delete_meta(file_id)
    events.emit("file_deleted", file_id=file_id)
    return {"deleted": removed}


# ----- Tool wrappers -----


@router.post("/api/upload")
async def api_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    if not file.filename:
        raise HTTPException(400, "filename required")
    ext = Path(file.filename).suffix.lstrip(".") or "bin"
    fid = new_file_id(ext)
    data = await file.read()
    await save_bytes(input_path(fid), data)
    write_meta(
        fid,
        original_filename=file.filename,
        label=file.filename,
        operation="upload",
    )
    events.emit(
        "uploaded",
        file_id=fid,
        original_name=file.filename,
        size_kb=len(data) // 1024,
    )
    return {
        "file_id": fid,
        "size_kb": len(data) // 1024,
        "original_name": file.filename,
        "url": public_url(fid),
    }


async def _run_with_event(name: str, params: dict[str, Any], coro_factory):
    t0 = time.time()
    events.emit("tool_started", tool=name, params=params)
    try:
        result = await coro_factory()
        events.emit(
            "tool_done",
            tool=name,
            params=params,
            result=result,
            ms_elapsed=int((time.time() - t0) * 1000),
        )
        return result
    except Exception as e:
        log.exception("tool %s failed", name)
        events.emit(
            "tool_error",
            tool=name,
            params=params,
            error=str(e),
            ms_elapsed=int((time.time() - t0) * 1000),
        )
        raise HTTPException(500, str(e))


@router.post("/api/convert")
async def api_convert(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    pool = _pool(request)
    return await _run_with_event(
        "convert",
        body,
        lambda: t_convert(
            pool,
            file_id=body.get("file_id"),
            source_url=body.get("source_url"),
            source_format=body.get("source_format"),
            target_format=body["target_format"],
            use_browser=bool(body.get("use_browser", False)),
            options=body.get("options"),
        ),
    )


@router.post("/api/find_replace")
async def api_find_replace(body: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_event(
        "find_replace",
        body,
        lambda: t_find_replace(
            body["file_id"],
            body["pattern"],
            body["replacement"],
            bool(body.get("regex", False)),
        ),
    )


@router.post("/api/delete_section")
async def api_delete_section(body: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_event(
        "delete_section",
        body,
        lambda: t_delete_section(
            body["file_id"], body["heading"], body.get("level")
        ),
    )


@router.post("/api/insert_after")
async def api_insert_after(body: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_event(
        "insert_after",
        body,
        lambda: t_insert_after(body["file_id"], body["anchor"], body["content"]),
    )


@router.post("/api/replace_heading")
async def api_replace_heading(body: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_event(
        "replace_heading",
        body,
        lambda: t_replace_heading(
            body["file_id"], body["old_title"], body["new_title"]
        ),
    )


@router.post("/api/delete_pages")
async def api_delete_pages(body: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_event(
        "delete_pages",
        body,
        lambda: t_delete_pages(body["file_id"], body["page_range"]),
    )


@router.post("/api/merge")
async def api_merge(body: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_event(
        "merge",
        body,
        lambda: t_merge(body["file_ids"], body.get("target_format", "pdf")),
    )


@router.post("/api/extract")
async def api_extract(body: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_event(
        "extract",
        body,
        lambda: t_extract(body["file_id"], body.get("mode", "text")),
    )


@router.post("/api/edit_with_instruction")
async def api_edit_ai(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    pool = _pool(request)
    return await _run_with_event(
        "edit_with_instruction",
        body,
        lambda: t_edit_ai(
            pool,
            file_id=body["file_id"],
            instruction=body["instruction"],
            require_confidence=bool(body.get("require_confidence", True)),
            return_format=body.get("return_format"),
        ),
    )


@router.post("/api/ocr_image")
async def api_ocr_image(body: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_event(
        "ocr_image", body,
        lambda: vision_ops.ocr_image(body["image_file_id"], language_hint=body.get("language_hint")),
    )


@router.post("/api/describe_image")
async def api_describe_image(body: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_event(
        "describe_image", body,
        lambda: vision_ops.describe_image(body["image_file_id"], question=body.get("question")),
    )


@router.post("/api/extract_table_from_image")
async def api_extract_table(body: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_event(
        "extract_table_from_image", body,
        lambda: vision_ops.extract_table_from_image(body["image_file_id"]),
    )


@router.post("/api/image_to_xlsx")
async def api_image_to_xlsx(body: dict[str, Any]) -> dict[str, Any]:
    return await _run_with_event(
        "image_to_xlsx", body,
        lambda: vision_ops.image_to_xlsx(body["image_file_id"], sheet_name=body.get("sheet_name", "Sheet1")),
    )


@router.get("/api/brands")
async def api_brands() -> dict[str, Any]:
    return {"brands": list_brands()}


@router.get("/api/brands/{name}")
async def api_brand_describe(name: str) -> dict[str, Any]:
    b = get_brand(name)
    if b is None:
        raise HTTPException(404, f"brand not found: {name}")
    return b.to_summary()


@router.post("/api/brand_apply")
async def api_brand_apply(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    file_id = body.get("file_id")
    brand_name = body.get("brand")
    brief_meta = body.get("brief_meta", {}) or {}
    if not file_id or not brand_name:
        raise HTTPException(400, "file_id and brand required")
    brand = get_brand(brand_name)
    if brand is None:
        raise HTTPException(404, f"unknown brand: {brand_name}")

    p = output_path(file_id)
    if not p.exists():
        p = input_path(file_id)
    if not p.exists():
        raise HTTPException(404, f"file_id not found: {file_id}")
    fmt = detect_from_path(p)
    pool = _pool(request)

    return await _run_with_event(
        f"brand_apply:{brand_name}", body,
        lambda: _do_brand_apply(pool, fmt, file_id, brand, brief_meta),
    )


async def _do_brand_apply(pool, fmt, file_id, brand, brief_meta):
    if fmt == "pptx":
        return await brand_pptx_renderer.apply_brand_to_pptx(file_id, brand, brief_meta=brief_meta)
    if fmt == "docx":
        return await brand_docx_renderer.apply_brand_to_docx(file_id, brand, brief_meta=brief_meta)
    if fmt in ("html", "htm", "md"):
        return await brand_html_renderer.apply_brand_to_html(file_id, brand, brief_meta=brief_meta)
    if fmt == "pdf":
        rt_docx = await t_convert(pool, file_id=file_id, target_format="docx")
        branded = await brand_docx_renderer.apply_brand_to_docx(
            rt_docx["file_id"], brand, brief_meta=brief_meta,
        )
        rt_pdf = await t_convert(pool, file_id=branded["file_id"], target_format="pdf")
        return {
            **rt_pdf,
            "brand": brand.name,
            "intermediate_docx": branded["file_id"],
            "headings_underlined": branded.get("headings_underlined"),
            "section_labels_added": branded.get("section_labels_added"),
        }
    raise HTTPException(501, f"brand_apply for {fmt} not yet implemented")


@router.post("/api/agent_edit")
async def api_agent_edit(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Autonomous agent edit — Kimi K2.6 iterates with tool-use until done."""
    pool = _pool(request)
    return await _run_with_event(
        "agent_edit",
        body,
        lambda: run_agent(
            pool=pool,
            file_id=body["file_id"],
            instruction=body["instruction"],
            max_iters=int(body.get("max_iters", 12)),
            wall_budget_s=float(body.get("wall_budget_s", 120)),
        ),
    )


# ----- Activity stream (SSE) -----


@router.get("/api/events")
async def api_events(request: Request):
    async def event_generator():
        # Replay history first
        for evt in events.history():
            if await request.is_disconnected():
                return
            yield {"event": "history", "data": json.dumps(evt)}

        q = events.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield {"event": "live", "data": json.dumps(evt)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            events.unsubscribe(q)

    return EventSourceResponse(event_generator())


# ----- Status -----


@router.get("/api/status")
async def api_status(request: Request) -> dict[str, Any]:
    pool = getattr(request.app.state, "pool", None)
    return {
        "lo_warm": len(pool._lo_workers) if pool else 0,
        "chromium_ready": bool(pool and pool._browser),
        "data_dir": str(settings.DATA_DIR),
        "siliconflow_configured": bool(settings.SILICONFLOW_API_KEY),
        "model_primary": settings.LLM_MODEL_PRIMARY,
        "model_fallback": settings.LLM_MODEL_FALLBACK,
    }
