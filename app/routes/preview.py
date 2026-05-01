"""
Preview route — converts files on-the-fly to a browser-renderable form
so the dashboard can show DOCX/PPTX/XLSX/ODT/RTF previews via PDF.

Cache key: {file_id}_{mtime}.pdf in /data/temp/previews/.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

from app.config import settings
from app.storage import display_label, input_path, output_path, temp_path
from app.tools.formats import LO_FILTERS, detect_from_path

log = logging.getLogger("aice.preview")
router = APIRouter()

PREVIEW_DIR = settings.DATA_DIR / "temp" / "previews"
PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

LO_PREVIEW_FORMATS = {"docx", "doc", "odt", "rtf", "pptx", "xlsx"}
IMAGE_FORMATS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"}
IMAGE_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
    "svg": "image/svg+xml",
}


def _resolve(file_id: str) -> Path:
    p = output_path(file_id)
    if p.exists():
        return p
    p = input_path(file_id)
    if p.exists():
        return p
    raise HTTPException(404, "file not found")


def _preview_filename(file_id: str, ext: str) -> str:
    """Build a sensible filename for the preview download. Strips any old
    extension from the label and appends the preview format's extension —
    so saving a DOCX preview gets `page28.pdf`, not `page28.docx.pdf` and
    definitely not the raw uuid+wrong extension that becomes a kentaur."""
    label = display_label(file_id) or file_id
    stem = label.rsplit(".", 1)[0] if "." in label else label
    return f"{stem}.{ext}"


@router.get("/preview/{file_id}")
async def preview(file_id: str, request: Request):
    src = _resolve(file_id)
    fmt = detect_from_path(src)
    mtime = int(src.stat().st_mtime)

    # Direct serve formats
    if fmt == "pdf":
        return FileResponse(
            src, media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{_preview_filename(file_id, "pdf")}"'},
        )

    if fmt in IMAGE_FORMATS:
        return FileResponse(
            src, media_type=IMAGE_MIME.get(fmt, "image/png"),
            headers={"Content-Disposition": f'inline; filename="{_preview_filename(file_id, fmt)}"'},
        )

    if fmt in {"html", "htm"}:
        return HTMLResponse(src.read_text(encoding="utf-8", errors="replace"))

    if fmt == "txt":
        return PlainTextResponse(src.read_text(encoding="utf-8", errors="replace"))

    if fmt == "md":
        # Render via pandoc to standalone HTML
        import pypandoc
        try:
            html = pypandoc.convert_file(
                str(src), "html", extra_args=["--standalone", "--metadata", f"title={src.stem}"]
            )
        except Exception as e:
            return PlainTextResponse(f"pandoc render failed: {e}\n\n" + src.read_text(encoding='utf-8', errors='replace'))
        return HTMLResponse(html)

    # LibreOffice-renderable → convert to PDF (cached by mtime)
    if fmt in LO_PREVIEW_FORMATS:
        cache_path = PREVIEW_DIR / f"{file_id}_{mtime}.pdf"
        if not cache_path.exists():
            pool = getattr(request.app.state, "pool", None)
            if not pool:
                raise HTTPException(503, "worker pool not ready")
            worker = await pool.acquire_lo()
            try:
                tmp_dir = temp_path()
                tmp_dir.mkdir(parents=True, exist_ok=True)
                target_filter = LO_FILTERS.get("pdf", "pdf")
                produced = await worker.convert(src, tmp_dir, target_filter)
                shutil.move(str(produced), cache_path)
                shutil.rmtree(tmp_dir, ignore_errors=True)
            finally:
                await pool.release_lo(worker)
        return FileResponse(
            cache_path,
            media_type="application/pdf",
            headers={
                # Match content type with filename extension so the user's
                # PDF-viewer "Save" button produces a `.pdf` file, not a
                # PDF-content-with-`.docx`-name kentaur.
                "Content-Disposition": f'inline; filename="{_preview_filename(file_id, "pdf")}"',
            },
        )

    # Unknown — fall through to text
    try:
        return PlainTextResponse(src.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return Response(content=src.read_bytes(), media_type="application/octet-stream")
