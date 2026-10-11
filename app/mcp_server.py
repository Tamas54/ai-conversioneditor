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
from app.tools import pptx_builder
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
    engine: Optional[str] = None,
) -> dict:
    """
    Convert a document between formats.

    Pass either `file_id` (already uploaded) or `source_url`. Specify
    `target_format` (pdf|docx|md|html|odt|pptx|xlsx|txt|rtf).

    Set `use_browser=True` for HTML→PDF when JS-rendering or modern CSS
    (flexbox/grid/recharts/d3) is needed. Defaults to WeasyPrint (faster,
    print-quality CSS only).

    Set `engine="wordcraft"` for the Rust Word engine: no LibreOffice, a few
    ms per call. Sources docx/doc/odt/rtf/html/md/tex/txt; targets
    docx/pdf/odt/rtf/html/md/tex/txt/png (png = first page).
    engine="photocraft": images (psd/png/jpg/webp/tif/exr…, PSD layers kept
    when saving psd). engine="vectorcraft": svg/pdf/ai/eps → pdf/svg/png/eps.
    """
    return await t_convert(
        _pool(),
        file_id=file_id,
        source_url=source_url,
        source_format=source_format,
        target_format=target_format,
        use_browser=use_browser,
        options=options,
        engine=engine,
    )


@mcp.tool()
async def wordcraft(
    file_id: Optional[str] = None,
    commands: Optional[list] = None,
    author: Optional[str] = None,
    save_format: str = "docx",
    template: Optional[str] = None,
    read: Optional[str] = None,
    query: Optional[str] = None,
    offset: int = 0,
    max_chars: int = 20000,
) -> dict:
    """
    Edit a document with the WordCraft Word engine: the same ~460 commands
    that drive Word's ribbon (styles, tables, track changes, comments, TOC,
    footnotes…), run headless. The result is a NEW file_id; the source stays.

    commands: in order, each {"command": id, "params": {...}} or "id={json}".
      [{"command": "review.trackChanges", "params": {"value": true}},
       {"command": "select.text", "params": {"text": "old"}},
       {"command": "text.insert", "params": {"text": "new"}}]
    author: name on tracked changes and comments (e.g. "Claude (copyedit)").
    Files in params: pass "file_id" instead of a path, e.g.
      {"command": "insert.picture", "params": {"file_id": <image>, "width": 430}}.
    Jump to the n-th paragraph (1-based) with edit.goto {"paragraph": n}.
    After insert.picture the picture stays SELECTED: run caret.end before
      text.newParagraph, or Enter replaces (deletes) it.
    para.alignCenter/Left/Right TOGGLE like Word's buttons; to set an
      alignment use para.align {"value": "center"}.
    edit.replaceAll: ALWAYS pass "matchCase" explicitly. Find options carry
      over between commands (like Word's dialog), and a case-insensitive
      replace does not keep the found text's capitals ("az X" → "Az X").
    Build a TOC (references.toc) after review.acceptAll: on a document with
      pending changes it lists deleted and inserted text side by side.
    No file_id → starts from `template` (blank|sample|letter|resume|report).
    save_format: docx (default) | pdf | odt | rtf | html | md | tex | txt | png.

    read instead of editing: "text" | "info" | "inspect" (structure JSON,
    shows tracked-change runs) | "review" (compact list of tracked changes
    and comments: who changed what, in which sentence — read a human's
    edits back cheaply) | "commands" (ids only; `query`, e.g. "table",
    gives params). text/inspect/commands are paged: pass the returned
    `next_offset` as `offset` (chars for text, blocks for inspect);
    `max_chars` caps one reply (default 20000).
    A human can open any docx in the dashboard editor (✏️) to continue by hand.
    """
    from app.tools import wordcraft as wc
    if read:
        return await wc.read(read, file_id=file_id, query=query, offset=offset, max_chars=max_chars)
    return await wc.run(
        file_id=file_id, commands=commands, author=author,
        save_format=save_format, template=template,
    )


@mcp.tool()
async def pptx_build(spec: dict) -> dict:
    """
    Build a whole 16:9 PowerPoint deck in ONE call, designed and illustrated.
    spec = {"title", "footer"?, "brand"? | "theme"? {primary, accent, bg,
            text, muted, font_heading, font_body}, "slides": [...]}
    Slide layouts (each may carry "notes"):
      cover {title, subtitle?, kicker?, image?}   section {title, subtitle?, number?, image?}
      bullets {title, bullets[], text?, image?}    image_left|image_right {title, bullets[]|text, image}
      full_image {image, title?, caption?}         quote {quote, author?, image?}
      stats {title, stats:[{value, label}] ≤4}     chart {title, chart_type, categories, series}
      table {title, headers, rows}                 gallery {title, images[≤4], captions?}
      closing {title?, subtitle?}
    image = "<file_id>" | {"file_id"} | {"url"} | {"generate": "<FLUX prompt>"}
            | {"stock": "<search words>"} (free CC photos, credit goes to notes).
    Images are cropped to their frame; FLUX images generate in parallel.
    Preview a slide with pptx_render_slide.
    """
    from app.tools import pptx_deck
    return await pptx_deck.build(spec)


@mcp.tool()
async def history(
    action: str = "inbox",
    file_id: Optional[str] = None,
    review_id: Optional[int] = None,
    resolution_file_id: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """
    Version history and the human review inbox (Postgres).
      action="lineage" + file_id   → ancestors (source chain) and descendants
                                     of a file: who made which version how.
      action="inbox"               → open "back to Claude" requests a person
                                     sent from the dashboard (file + note).
      action="request" + file_id, note → ask a person to review a file.
      action="resolve" + review_id (+ resolution_file_id, note) → close one.
    Typical loop: inbox → wordcraft(read="review") on the file → edit with
    tracked changes → resolve with the new file_id.
    """
    from app import db
    if action == "lineage":
        if not file_id:
            raise ValueError("lineage file_id-t kér")
        return await db.lineage(file_id)
    if action == "inbox":
        return {"open": await db.review_list("open", file_id=file_id)}
    if action == "request":
        if not file_id:
            raise ValueError("request file_id-t kér")
        return await db.review_create(file_id, note, "claude")
    if action == "resolve":
        if review_id is None:
            raise ValueError("resolve review_id-t kér")
        return await db.review_resolve(int(review_id), resolution_file_id, note)
    raise ValueError("action: lineage | inbox | request | resolve")


@mcp.tool()
async def design(
    template: str,
    data: dict,
    size: Optional[str] = None,
    theme: Optional[dict] = None,
    brand: Optional[str] = None,
    export: Optional[list[str]] = None,
) -> dict:
    """
    Designed print/social pieces from templates (VectorCraft) → PDF + PNG
    (export: pdf | png | svg | jpg). One call, no layout work.
    template / data:
      poster        {title, subtitle?, body?, image?, date?, place?, footer?}   size A4|A3|A5
      flyer         {title, kicker?, subtitle?, image?, body (2 columns), highlights[≤3]?, cta?, contact?}
      report_cover  {title, subtitle?, organization?, author?, date?, image?}
      certificate   {recipient, title?="Oklevél", lead?, text, place?, date?, signatures[{name, role}]}  (landscape)
      social        {title, subtitle?, handle?, image?}   size landscape (1200×630) | square | story
      booklet       {title, subtitle?, organization?, author?, date?, cover_image?,
                     sections[{heading, text, image?}]}   long text flows onto extra pages
    image = "<file_id>" | {"file_id"} | {"url"} | {"generate": "<FLUX prompt>"} | {"stock": "<words>"}
    theme {primary, accent, bg, text, muted} or brand=<name>. Titles shrink to fit.
    Edit the result by hand in the dashboard (✏️ Vector on the PDF).
    """
    from app.tools import design as dz
    return await dz.render(template, data, size=size, theme=theme, brand=brand, export=export)


@mcp.tool()
async def photocraft(
    file_id: Optional[str] = None,
    commands: Optional[list] = None,
    new: Optional[dict] = None,
    remove_background: bool = False,
    background_file_id: Optional[str] = None,
    background_blur: float = 0,
    save_format: Optional[str] = None,
    quality: Optional[int] = None,
    read: Optional[str] = None,
    query: Optional[str] = None,
    offset: int = 0,
    max_chars: int = 20000,
) -> dict:
    """
    Edit an image with PhotoCraft (Photoshop-like engine, ~860 commands):
    image.autoTone, image.imageSize {width, height}, image.crop, type.create
    {x, y, text, size, color}, filter.blur.gaussianBlur {radius},
    image.adjustments.photoFilter, layers… Result is a NEW file_id.

    remove_background=true cuts the subject out with a neural net (ISNet:
    keeps whiskers/hair). background_file_id puts that image behind the
    subject, filling the canvas; background_blur (px, e.g. 18) fakes a
    shallow depth of field. One call does "put this animal in front of
    the Parliament": file_id=photo, remove_background=true,
    background_file_id=<generate_image result>, background_blur=18.

    commands: [{"command": id, "params": {...}}]; files in params by
    "file_id" (e.g. file.placeEmbedded). new={"width":1200,"height":800}
    starts a blank canvas. save_format: png (keeps transparency) | jpg |
    webp | psd | tif | pcraft; quality 1-100 for jpg/webp.
    read: "info" (size, layers) | "commands" (ids; `query` for params).
    """
    from app.tools import crafts
    if read:
        return await crafts.photo_read(read, file_id=file_id, query=query, offset=offset, max_chars=max_chars)
    return await crafts.photo_run(
        file_id=file_id, new=new, commands=commands, save_format=save_format, quality=quality,
        remove_background=remove_background, background_file_id=background_file_id,
        background_blur=background_blur,
    )


@mcp.tool()
async def vectorcraft(
    file_id: Optional[str] = None,
    commands: Optional[list] = None,
    export: Optional[list[str]] = None,
    scale: Optional[float] = None,
    read: Optional[str] = None,
    query: Optional[str] = None,
    offset: int = 0,
    max_chars: int = 20000,
) -> dict:
    """
    Design and lay out with VectorCraft (Illustrator/InDesign-like, ~730
    commands): posters, flyers, brochures, multi-page PDFs, SVG. Opens
    file_id (svg/pdf/ai/eps/.vectorcraft) or starts empty; exports every
    format in `export` (default ["pdf"]; also svg, png, jpg, webp, eps,
    vectorcraft) as new file_ids.

    Typical: file.new {width:"210 mm", height:"297 mm"} → shape.rectangle
    {x,y,width,height} → text.create {x, y, text, size, font, color,
    area:{width,height}} (area = wrapping text box) → file.place
    {"file_id": <image>} → artboard.new {width,height} (next page).
    Coordinates in points (A4 = 595×842). Fonts: "Source Serif 4",
    "Source Sans 3" are built in. A new shape/placed image stays SELECTED:
    colour it with paint.setFill {color} / paint.setStroke {none: true};
    Place images with file.place {"file_id": <img>, "rect": [x, y, w, h]}
    (fits inside, aspect kept); without rect a photo lands at its physical
    size, and object.setBounds on a placed image does not re-render it.
    Later objects stack on top (draw backgrounds first).
    read: "info" (artboards, fonts, counts) | "commands" (ids; `query`).
    """
    from app.tools import crafts
    if read:
        return await crafts.vector_read(read, file_id=file_id, query=query, offset=offset, max_chars=max_chars)
    return await crafts.vector_run(file_id=file_id, commands=commands, export=export, scale=scale)


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


# ============================================================
# PPTX builder
# ============================================================


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
    decorative covers. size "WIDTHxHEIGHT", 256–2048 px a side, e.g.
    1792x1024 (wide), 1024x1792 (tall), 1024x1024, 1600x900 (16:9 slide).
    Returns image_file_id usable in insert_image."""
    return await vision_ops.generate_image(prompt, model=model, size=size)


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
    # Hitelesítés: app/auth.py (MCP-kulcs Bearer-ben vagy ?key=-ben).
    app.mount("/mcp", mcp_app)
    log.info("MCP endpoint mounted at /mcp")
