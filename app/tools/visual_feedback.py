"""
Visual feedback tools — give the agent 'eyes' on DOCX, HTML, PDF.

For PPTX the agent already had pptx_render_slide. These tools extend the
same render→describe→adjust loop to other formats:

  * docx_render_pages(file_id) → list of PNG image_file_ids
      Uses LO to convert DOCX→PDF, then PyMuPDF to rasterize each page.
  * html_render_screenshot(file_id) → PNG image_file_id
      Uses headless Chromium for full-page screenshot.
  * pdf_render_pages(file_id) → list of PNGs
      Direct PyMuPDF rasterization (no conversion).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

from app.storage import (
    input_path,
    new_file_id,
    output_path,
    public_url,
)
from app.tools.formats import detect_from_path

log = logging.getLogger("aice.visual_feedback")


def _resolve(file_id: str) -> Path:
    p = output_path(file_id)
    if p.exists():
        return p
    return input_path(file_id)


def _rasterize_pdf(pdf_path: Path, page_indices: Optional[list[int]] = None,
                   dpi: int = 110) -> list[dict]:
    import fitz
    doc = fitz.open(str(pdf_path))
    n = doc.page_count
    if page_indices is None:
        idxs = list(range(n))
    else:
        idxs = [i for i in page_indices if 0 <= i < n]
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    out = []
    for i in idxs:
        pix = doc[i].get_pixmap(matrix=matrix)
        png_fid = new_file_id("png")
        with open(output_path(png_fid), "wb") as f:
            f.write(pix.tobytes("png"))
        out.append({
            "page_index": i,
            "image_file_id": png_fid,
            "url": public_url(png_fid),
            "width_px": pix.width,
            "height_px": pix.height,
        })
    doc.close()
    return out


async def pdf_render_pages(
    file_id: str, page_indices: Optional[list[int]] = None, dpi: int = 110,
) -> dict[str, Any]:
    src = _resolve(file_id)
    if detect_from_path(src) != "pdf":
        raise ValueError(f"pdf_render_pages only works on PDF (got {src.suffix})")
    t0 = time.time()
    rendered = _rasterize_pdf(src, page_indices, dpi)
    return {
        "page_count": _pdf_page_count(src),
        "rendered": rendered,
        "ms_elapsed": int((time.time() - t0) * 1000),
    }


def _pdf_page_count(pdf_path: Path) -> int:
    import fitz
    doc = fitz.open(str(pdf_path))
    n = doc.page_count
    doc.close()
    return n


async def docx_render_pages(
    pool, file_id: str, page_indices: Optional[list[int]] = None, dpi: int = 110,
) -> dict[str, Any]:
    """LO converts DOCX → PDF → PyMuPDF rasterizes pages → returns PNG file_ids."""
    from app.tools.convert import convert as _convert

    src = _resolve(file_id)
    fmt = detect_from_path(src)
    if fmt not in ("docx", "doc", "odt", "rtf"):
        raise ValueError(f"docx_render_pages: unsupported format {fmt}")

    t0 = time.time()
    pdf_out = await _convert(pool, file_id=file_id, target_format="pdf")
    pdf_path = _resolve(pdf_out["file_id"])

    rendered = _rasterize_pdf(pdf_path, page_indices, dpi)
    return {
        "page_count": _pdf_page_count(pdf_path),
        "rendered": rendered,
        "intermediate_pdf_fid": pdf_out["file_id"],
        "ms_elapsed": int((time.time() - t0) * 1000),
    }


async def html_render_screenshot(
    pool, file_id: str, *, viewport_width: int = 1024,
    full_page: bool = True,
) -> dict[str, Any]:
    """Headless-Chromium full-page screenshot of an HTML file."""
    src = _resolve(file_id)
    fmt = detect_from_path(src)
    if fmt not in ("html", "htm"):
        raise ValueError(f"html_render_screenshot only works on HTML (got {fmt})")

    t0 = time.time()
    ctx, page = await pool.acquire_chromium_page()
    try:
        await page.set_viewport_size({"width": viewport_width, "height": 1024})
        await page.goto(f"file://{src.resolve()}", wait_until="networkidle")
        png_bytes = await page.screenshot(full_page=full_page)
    finally:
        await pool.release_chromium_page(ctx, page)

    fid = new_file_id("png")
    with open(output_path(fid), "wb") as f:
        f.write(png_bytes)
    return {
        "image_file_id": fid,
        "url": public_url(fid),
        "size_bytes": len(png_bytes),
        "viewport_width": viewport_width,
        "ms_elapsed": int((time.time() - t0) * 1000),
    }
