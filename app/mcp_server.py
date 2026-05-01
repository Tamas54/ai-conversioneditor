"""
MCP-over-HTTP endpoint.

Uses the official `mcp` Python SDK in Streamable-HTTP mode. Tools are
registered as async functions; the SDK handles JSON-RPC framing.

Auth:
    If MCP_AUTH_TOKEN is set, clients must send `Authorization: Bearer <token>`.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.config import settings
from app.storage import (
    input_path, new_file_id, save_bytes, public_url, write_meta,
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

log = logging.getLogger("aice.mcp")

# streamable_http_path="/" — so when this sub-app is mounted at "/mcp"
# in main.py, the effective external URL is just `/mcp` (not `/mcp/mcp`).
# stateless_http=True — no server-side session storage needed; each request
# carries its own context. Required for serverless-style deploys (Railway)
# where memory state can be lost between containers.
# json_response=True — return JSON instead of SSE for simpler clients.
# transport_security: disable DNS-rebinding host validation. Without this,
# FastMCP returns "421 Invalid Host header" for ANY request on a deployed
# domain (Railway, Fly, Render, etc.) that wasn't explicitly whitelisted.
# We're publicly hosted with our own auth (or none, deliberately), so the
# DNS-rebinding protection is irrelevant and only breaks things.
mcp = FastMCP(
    "ai-conversioneditor",
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


@mcp.tool()
async def upload_file(filename: str, content_base64: str) -> dict:
    """
    Upload a file to the editor and get back a `file_id`.

    Use this FIRST when you have a local file you want to process — without
    a file_id, no other tool can act on your file. The returned file_id is
    then passed to convert, find_replace, edit_with_instruction, etc.

    Args:
      filename: original name (used for the human-readable label, e.g.
                "report.docx", "page28.jpeg"). Extension is preserved as
                the file_id's extension.
      content_base64: the file's bytes encoded as base64 (RFC 4648). For a
                local file in your sandbox: read it as bytes and call
                base64.b64encode(data).decode('ascii').

    Returns:
      {file_id, url, size_kb, original_name}

    Examples:
      • OCR a photo:  upload_file("scan.jpg", b64) → file_id → edit_with_instruction(file_id, "extract text and write to a clean DOCX")
      • Convert PDF: upload_file("report.pdf", b64) → file_id → convert(file_id=..., target_format="docx")
    """
    try:
        data = base64.b64decode(content_base64, validate=True)
    except Exception as e:
        raise ValueError(f"content_base64 is not valid base64: {e}")
    if not data:
        raise ValueError("content_base64 decoded to empty bytes")

    ext = Path(filename).suffix.lstrip(".") or "bin"
    fid = new_file_id(ext)
    await save_bytes(input_path(fid), data)
    write_meta(
        fid,
        original_filename=filename,
        label=filename,
        operation="upload",
    )
    return {
        "file_id": fid,
        "url": public_url(fid),
        "size_kb": len(data) // 1024,
        "original_name": filename,
    }


@mcp.tool()
async def convert(
    target_format: str,
    file_id: Optional[str] = None,
    source_url: Optional[str] = None,
    source_format: Optional[str] = None,
    use_browser: bool = False,
    options: Optional[dict] = None,
) -> dict:
    """
    Convert a document between formats.

    Pass either `file_id` (already uploaded) or `source_url`. Specify
    `target_format` (pdf|docx|md|html|odt|pptx|xlsx|txt|rtf).

    Set `use_browser=True` for HTML→PDF when JS-rendering or modern CSS
    (flexbox/grid/recharts/d3) is needed. Defaults to WeasyPrint (faster,
    print-quality CSS only).
    """
    return await t_convert(
        _pool(),
        file_id=file_id,
        source_url=source_url,
        source_format=source_format,
        target_format=target_format,
        use_browser=use_browser,
        options=options,
    )


@mcp.tool()
async def find_replace(
    file_id: str, pattern: str, replacement: str, regex: bool = False
) -> dict:
    """Replace a string (or regex) in a DOCX/MD/HTML/TXT file."""
    return await t_find_replace(file_id, pattern, replacement, regex)


@mcp.tool()
async def delete_section(
    file_id: str, heading: str, level: Optional[int] = None
) -> dict:
    """Delete a section by heading title (DOCX or MD). Removes content
    until the next same- or higher-level heading."""
    return await t_delete_section(file_id, heading, level)


@mcp.tool()
async def insert_after(file_id: str, anchor: str, content: str) -> dict:
    """Insert content after a heading (DOCX) or after a line (MD)."""
    return await t_insert_after(file_id, anchor, content)


@mcp.tool()
async def replace_heading(file_id: str, old_title: str, new_title: str) -> dict:
    """Replace a heading's text while preserving its level."""
    return await t_replace_heading(file_id, old_title, new_title)


@mcp.tool()
async def delete_pages(file_id: str, page_range: str) -> dict:
    """Delete pages from a PDF. Range examples: '3', '1-5', '1,3,5-7'."""
    return await t_delete_pages(file_id, page_range)


@mcp.tool()
async def merge(file_ids: list[str], target_format: str = "pdf") -> dict:
    """Merge multiple files (currently PDF) into one."""
    return await t_merge(file_ids, target_format)


@mcp.tool()
async def extract(file_id: str, mode: str = "text") -> dict:
    """Extract text or tables from a PDF/DOCX. modes: 'text'|'tables'."""
    return await t_extract(file_id, mode)


@mcp.tool()
async def edit_with_instruction(
    file_id: str,
    instruction: str,
    require_confidence: bool = True,
    return_format: Optional[str] = None,
) -> dict:
    """
    Edit a document using a natural-language instruction.

    Pipeline: load → extract text → Kimi K2.6 plans operations →
    deterministic execution → render back to original format.

    PDF inputs are auto-round-tripped via DOCX (with a layout-reflow warning).

    If `require_confidence=True` (default) and the LLM's confidence is
    below 0.7, returns the plan WITHOUT executing — letting you review.
    Set `require_confidence=False` to always execute.
    """
    return await t_edit_ai(
        _pool(),
        file_id=file_id,
        instruction=instruction,
        require_confidence=require_confidence,
        return_format=return_format,
    )


# --- Plumbing ---


_app_ref: FastAPI | None = None


def _pool():
    if _app_ref is None or not hasattr(_app_ref.state, "pool"):
        raise RuntimeError("Worker pool not initialized")
    return _app_ref.state.pool


def mount_mcp(app: FastAPI):
    """Mount the MCP streamable-HTTP endpoint at /mcp."""
    global _app_ref
    _app_ref = app

    mcp_app = mcp.streamable_http_app()

    if settings.MCP_AUTH_TOKEN:
        @app.middleware("http")
        async def auth_mw(request: Request, call_next):
            if request.url.path.startswith("/mcp"):
                auth = request.headers.get("authorization", "")
                if auth != f"Bearer {settings.MCP_AUTH_TOKEN}":
                    raise HTTPException(401, "Unauthorized")
            return await call_next(request)

    app.mount("/mcp", mcp_app)
    log.info("MCP endpoint mounted at /mcp")
