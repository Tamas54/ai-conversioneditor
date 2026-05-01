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
    insert_image as t_insert_image,
)
from app.tools import docx_builder, docx_revisions, pptx_builder
from app.tools import vision_ops, chart_image, chart_redraw as chart_redraw_mod
from app.tools.visual_feedback import docx_render_pages as t_docx_render_pages

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


_CHUNK_BUFFERS: dict[str, dict[int, bytes]] = {}


@mcp.tool()
async def upload_from_url(url: str, filename: Optional[str] = None) -> dict:
    """
    Upload a file by fetching its public URL — the SERVER does the GET,
    so you avoid the base64 round-trip entirely. Best when:
      • you can put the file somewhere reachable (gist, S3, transfer.sh,
        another Railway service, anywhere with HTTP/HTTPS)
      • the file is already online (article PDF, github raw, etc.)

    Args:
      url: full HTTP/HTTPS URL the server can GET.
      filename: optional override for the human label. If omitted, taken
                from the URL's last path segment.

    Returns: same shape as upload_file: {file_id, url, size_kb, original_name}.
    """
    import httpx
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.content
    if not filename:
        filename = url.rstrip("/").split("/")[-1].split("?")[0] or "downloaded.bin"
    ext = Path(filename).suffix.lstrip(".") or "bin"
    fid = new_file_id(ext)
    await save_bytes(input_path(fid), data)
    write_meta(
        fid, original_filename=filename, label=filename,
        operation="upload_from_url", extra={"source_url": url},
    )
    return {
        "file_id": fid,
        "url": public_url(fid),
        "size_kb": len(data) // 1024,
        "original_name": filename,
    }


@mcp.tool()
async def upload_chunk(
    upload_id: str,
    chunk_index: int,
    total_chunks: int,
    filename: str,
    content_base64: str,
) -> dict:
    """
    Upload a large file as multiple base64 chunks across separate tool
    calls — bypasses the per-tool-call input size limit.

    Workflow:
      1. Pick any upload_id (e.g. 'page28-' + uuid). Use the SAME id for
         every chunk of the same file.
      2. Split the file's bytes into N pieces of ≤200KB each (raw bytes;
         after base64 each piece is ≤270KB — fits in tool input).
      3. For each chunk, base64-encode and call:
           upload_chunk(upload_id, chunk_index=i, total_chunks=N,
                        filename='page28.jpeg', content_base64=...)
      4. The LAST chunk's response contains the final file_id. Earlier
         chunks return {pending: i+1/N, received_bytes: ...}.

    Order does NOT matter — chunks can be sent in any order. The file is
    assembled when all `total_chunks` are present.
    """
    if total_chunks < 1:
        raise ValueError("total_chunks must be >= 1")
    if not (0 <= chunk_index < total_chunks):
        raise ValueError(f"chunk_index out of range: {chunk_index} not in [0,{total_chunks})")
    try:
        data = base64.b64decode(content_base64, validate=True)
    except Exception as e:
        raise ValueError(f"content_base64 is not valid base64: {e}")

    buf = _CHUNK_BUFFERS.setdefault(upload_id, {})
    buf[chunk_index] = data

    if len(buf) < total_chunks:
        return {
            "pending": True,
            "chunks_received": len(buf),
            "total_chunks": total_chunks,
            "received_bytes": sum(len(v) for v in buf.values()),
        }

    # All chunks present — assemble in index order and finalize
    full = b"".join(buf[i] for i in range(total_chunks))
    _CHUNK_BUFFERS.pop(upload_id, None)

    ext = Path(filename).suffix.lstrip(".") or "bin"
    fid = new_file_id(ext)
    await save_bytes(input_path(fid), full)
    write_meta(
        fid, original_filename=filename, label=filename,
        operation="upload_chunked",
        extra={"upload_id": upload_id, "n_chunks": total_chunks},
    )
    return {
        "file_id": fid,
        "url": public_url(fid),
        "size_kb": len(full) // 1024,
        "original_name": filename,
        "chunks_assembled": total_chunks,
    }


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


# ============================================================
# DOCX builder (build new documents from scratch)
# ============================================================


@mcp.tool()
async def docx_create(title: Optional[str] = None) -> dict:
    """Create a new empty DOCX with an optional centered title. Returns
    file_id. Chain with docx_add_* tools to fill the document."""
    return await docx_builder.docx_create(title=title)


@mcp.tool()
async def docx_add_heading(
    file_id: str, text: str, level: int = 1,
) -> dict:
    """Append a heading to a DOCX. level 1-9 (1=biggest). Returns new file_id."""
    return await docx_builder.docx_add_heading(file_id=file_id, text=text, level=level)


@mcp.tool()
async def docx_add_paragraph(
    file_id: str, text: str,
    bold: bool = False, italic: bool = False,
    font_size: Optional[float] = None, alignment: Optional[str] = None,
) -> dict:
    """Append a paragraph to a DOCX. alignment ∈ {left,center,right,justify}.
    Returns new file_id."""
    return await docx_builder.docx_add_paragraph(
        file_id=file_id, text=text, bold=bold, italic=italic,
        font_size=font_size, alignment=alignment,
    )


@mcp.tool()
async def docx_add_bulleted_list(file_id: str, items: list[str]) -> dict:
    """Append a bulleted list to a DOCX. Returns new file_id."""
    return await docx_builder.docx_add_bulleted_list(file_id=file_id, items=items)


@mcp.tool()
async def docx_add_numbered_list(file_id: str, items: list[str]) -> dict:
    """Append a numbered list to a DOCX. Returns new file_id."""
    return await docx_builder.docx_add_numbered_list(file_id=file_id, items=items)


@mcp.tool()
async def docx_add_table(
    file_id: str, headers: list[str], rows: list[list[str]],
    style: str = "Light Grid Accent 1",
) -> dict:
    """Append a table with headers + data rows. Returns new file_id."""
    return await docx_builder.docx_add_table(
        file_id=file_id, headers=headers, rows=rows, style=style,
    )


@mcp.tool()
async def docx_add_page_break(file_id: str) -> dict:
    """Insert a page break at the end of a DOCX. Returns new file_id."""
    return await docx_builder.docx_add_page_break(file_id=file_id)


@mcp.tool()
async def docx_render_pages(
    file_id: str, page_indices: Optional[list[int]] = None, dpi: int = 110,
) -> dict:
    """Render DOCX pages to PNG via LO+PyMuPDF for visual verification.
    page_indices=[0,1,2] for specific pages, or omit for all. Returns
    {pages: [{page_index, image_file_id, ...}]} — pass image_file_ids
    to describe_image / ocr_image. Use after building to catch overflow,
    layout breaks, or wrong styling before declaring done."""
    return await t_docx_render_pages(
        _pool(), file_id=file_id, page_indices=page_indices, dpi=dpi,
    )


@mcp.tool()
async def insert_image(
    file_id: str, anchor: str, image_file_id: str,
    width_inches: Optional[float] = None,
    svg_file_id: Optional[str] = None,
) -> dict:
    """Insert an image into the working DOCX after the anchor paragraph.
    image_file_id must point to an already-uploaded PNG/JPG. If svg_file_id
    is also given, a vector SVG layer is attached (Word 2016+ / LO 7+
    render the SVG as vector; older readers fall back to PNG). Combine with
    chart_redraw(also_svg=true) for vector charts in the final PDF."""
    return await t_insert_image(
        file_id=file_id, anchor=anchor, image_file_id=image_file_id,
        width_inches=width_inches, svg_file_id=svg_file_id,
    )


# ============================================================
# DOCX revisions / comments (collaborative editing)
# ============================================================


@mcp.tool()
async def docx_track_replace_paragraph(
    file_id: str, anchor: str, new_text: str,
    author: str = "Editor",
) -> dict:
    """Replace a paragraph as a TRACKED CHANGE: the original text becomes
    a <w:del>, the new text a <w:ins>. Word reviewers see it as a tracked
    edit they can accept/reject. Returns new file_id."""
    return await docx_revisions.docx_track_replace_paragraph(
        file_id=file_id, anchor=anchor, new_text=new_text, author=author,
    )


@mcp.tool()
async def docx_accept_all_revisions(file_id: str) -> dict:
    """Accept every <w:ins> / <w:del> in the document, producing a clean
    DOCX with no tracked changes. Returns new file_id."""
    return await docx_revisions.docx_accept_all_revisions(file_id=file_id)


@mcp.tool()
async def docx_reject_all_revisions(file_id: str) -> dict:
    """Reject every <w:ins> / <w:del>: insertions removed, deletions
    restored. Returns new file_id."""
    return await docx_revisions.docx_reject_all_revisions(file_id=file_id)


@mcp.tool()
async def docx_list_revisions(file_id: str) -> dict:
    """List every tracked-change in the DOCX (insertions + deletions)
    with author and content. Read-only inspection, doesn't modify file."""
    return await docx_revisions.docx_list_revisions(file_id=file_id)


@mcp.tool()
async def docx_add_comment(
    file_id: str, anchor: str, text: str, author: str = "Editor",
) -> dict:
    """Add a comment anchored to the first paragraph matching `anchor`
    text. Returns new file_id + comment_id."""
    return await docx_revisions.docx_add_comment(
        file_id=file_id, anchor=anchor, text=text, author=author,
    )


# ============================================================
# PPTX builder
# ============================================================


@mcp.tool()
async def pptx_create(title: str, subtitle: Optional[str] = None) -> dict:
    """Create a new PPTX with a cover slide. Returns file_id. Chain with
    pptx_add_* to fill in content slides, then pptx_apply_theme for chrome."""
    return await pptx_builder.pptx_create(title=title, subtitle=subtitle)


@mcp.tool()
async def pptx_add_bullets_slide(
    file_id: str, title: str, bullets: list[str],
) -> dict:
    """Add a bullets slide. Max 4-5 bullets, ≤70 chars each for readability.
    Returns new file_id."""
    return await pptx_builder.pptx_add_bullets_slide(
        file_id=file_id, title=title, bullets=bullets,
    )


@mcp.tool()
async def pptx_add_table_slide(
    file_id: str, title: str, headers: list[str], rows: list[list[str]],
) -> dict:
    """Add a slide with a real PPTX table (better than bullets-pretending-
    to-be-table). Best for 5-7 columns × 4-6 rows. Returns new file_id."""
    return await pptx_builder.pptx_add_table_slide(
        file_id=file_id, title=title, headers=headers, rows=rows,
    )


@mcp.tool()
async def pptx_add_chart_slide(
    file_id: str, title: str, chart_type: str,
    categories: list[str], series: list[dict],
) -> dict:
    """Add a slide with a NATIVE PPTX chart (right-click → Edit Data works
    in Word/PowerPoint). chart_type ∈ {column,bar,line,pie,doughnut,area}.
    Each series: {name, values[]}. Returns new file_id."""
    return await pptx_builder.pptx_add_chart_slide(
        file_id=file_id, title=title, chart_type=chart_type,
        categories=categories, series=series,
    )


@mcp.tool()
async def pptx_apply_theme(
    file_id: str,
    accent_color: Optional[str] = None,
    accent_position: str = "left",
    footer_text: Optional[str] = None,
    page_numbers: bool = True,
) -> dict:
    """Polish a deck with consistent visual chrome: accent bar, footer,
    page numbers, harmonized title/body colors. accent_position ∈
    {left,top,none}. REQUIRED for finished decks — without theme, decks
    look amateur. Returns new file_id."""
    return await pptx_builder.pptx_apply_theme(
        file_id=file_id, accent_color=accent_color,
        accent_position=accent_position, footer_text=footer_text,
        page_numbers=page_numbers,
    )


@mcp.tool()
async def pptx_render_slide(
    file_id: str, slide_index: Optional[int] = None, dpi: int = 110,
) -> dict:
    """Render PPTX slide(s) to PNG so you can VISUALLY verify via
    describe_image. slide_index=N for one slide; omit for all. Returns
    image_file_id(s)."""
    return await pptx_builder.pptx_render_slide(
        _pool(), file_id=file_id, slide_index=slide_index, dpi=dpi,
    )


# ============================================================
# Vision (Kimi K2.6 multimodal — read images)
# ============================================================


@mcp.tool()
async def ocr_image(image_file_id: str, language_hint: Optional[str] = None) -> dict:
    """Read all visible text from an image (handwriting OK). language_hint
    helps weak-light photos: 'Hungarian', 'English', etc."""
    return await vision_ops.ocr_image(image_file_id, language_hint=language_hint)


@mcp.tool()
async def describe_image(image_file_id: str, question: Optional[str] = None) -> dict:
    """Free-form Q&A on an image (Kimi K2.6 vision). Default: detailed
    description. Pass `question` for targeted queries: 'is there overlap?',
    'what colors dominate?', 'count the people', etc."""
    return await vision_ops.describe_image(image_file_id, question=question)


@mcp.tool()
async def extract_table_from_image(image_file_id: str) -> dict:
    """Extract a table from an image as structured rows. Returns
    {headers: [...], rows: [[...], ...]}."""
    return await vision_ops.extract_table_from_image(image_file_id)


@mcp.tool()
async def image_to_xlsx(image_file_id: str, sheet_name: str = "Sheet1") -> dict:
    """OCR a photographed table and save as a real XLSX (one shot).
    Returns the new XLSX file_id."""
    return await vision_ops.image_to_xlsx(image_file_id, sheet_name=sheet_name)


@mcp.tool()
async def generate_image(
    prompt: str,
    size: str = "1024x1024",
    model: str = "black-forest-labs/FLUX.2-pro",
) -> dict:
    """Generate an image from text via SiliconFlow image-gen (FLUX.2-pro
    by default). Useful for: missing figures, conceptual illustrations,
    decorative covers. size ∈ {1024x1024, 1792x1024, 1024x1792}.
    Returns image_file_id usable in insert_image / pptx slides."""
    img_bytes = await vision_ops.generate_image(prompt, model=model, size=size)
    # generate_image returns raw bytes; save as a file
    from app.storage import new_file_id, output_path, public_url
    fid = new_file_id("png")
    output_path(fid).write_bytes(img_bytes)
    return {
        "image_file_id": fid,
        "url": public_url(fid),
        "size_bytes": len(img_bytes),
    }


# ============================================================
# Charts
# ============================================================


@mcp.tool()
async def generate_chart_image(
    chart_type: str,
    categories: list[str],
    series: list[dict],
    title: Optional[str] = None,
    palette: Optional[list[str]] = None,
    x_label: Optional[str] = None, y_label: Optional[str] = None,
    show_data_labels: bool = False,
    also_svg: bool = False,
) -> dict:
    """Render a chart from data into a PNG via matplotlib (200 DPI default).
    chart_type ∈ {column,bar,line,line_markers,area,pie,doughnut,scatter}.
    Pass also_svg=true to ALSO produce a vector SVG copy (svg_file_id).
    Returns the PNG file_id, plus svg_file_id if also_svg."""
    return await chart_image.generate_chart_image(
        chart_type, categories, series,
        title=title, palette=palette,
        x_label=x_label, y_label=y_label,
        show_data_labels=show_data_labels,
        also_svg=also_svg,
    )


@mcp.tool()
async def chart_redraw(
    image_file_id: str,
    palette: Optional[list[str]] = None,
    brand: Optional[str] = None,
    chart_type_override: Optional[str] = None,
    title_override: Optional[str] = None,
    also_svg: bool = False,
) -> dict:
    """Read an existing chart from an image via Kimi K2.6 vision (extracts
    type, axes, categories, series, values), and re-render via matplotlib
    with a new palette/brand. Returns new image_file_id. Pass also_svg=true
    for a vector copy (use with insert_image's svg_file_id for vector
    charts in DOCX/PDF)."""
    return await chart_redraw_mod.chart_redraw(
        image_file_id,
        palette=palette, brand=brand,
        chart_type_override=chart_type_override,
        title_override=title_override,
        also_svg=also_svg,
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
