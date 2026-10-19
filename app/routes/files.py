"""File-serving route — signed token required."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app import tenancy
from app.storage import (
    display_label, input_path, output_path, verify_file_token,
)

router = APIRouter()


@router.api_route("/files/{file_id}", methods=["GET", "HEAD"])     # HEAD: iOS/Quick Look és letöltéskezelők előre kérdeznek
async def get_file(file_id: str, t: str = Query(..., description="signed token"),
                   u: str = Query(tenancy.ADMIN_ID, description="workspace (signed into t)")):
    if not verify_file_token(file_id, t, uid=u):
        raise HTTPException(403, "invalid or expired token")
    tenancy.set_user(u)  # a link aláírása igazolja: ennek a munkaterületnek a fájlja
    p = output_path(file_id)
    if not p.exists():
        p = input_path(file_id)
    if not p.exists():
        raise HTTPException(404, "file not found")
    from app.storage import privacy
    try:
        p = await privacy.export_path(p)        # export: helyadat (GPS) nélkül — iPhone-felvétel, fotó (STRIP_LOCATION)
    except privacy.LocationNotRemoved as e:
        raise HTTPException(422, str(e)) from None
    # Use the human label as the download filename so the user gets
    # `page28.docx` instead of `f09de87…docx`. Falls back to file_id.
    download_name, media_type = download_meta(file_id, display_label(file_id) or file_id)
    return FileResponse(p, filename=download_name, media_type=media_type)


def download_meta(file_id: str, label: str) -> tuple[str, str]:
    """A letöltött fájl neve mindig a valódi kiterjesztésre végződik, a típusa pedig a valódi MIME — különben
    a Starlette a címkéből találgat, kiterjesztés nélkül „text/plain”-t küld, és a Safari „.txt”-t fűz a
    .pptx után (a Keynote/PowerPoint meg sem kapja)."""
    import mimetypes
    from pathlib import PurePosixPath
    from app.catalog import formats
    ext = formats.from_filename(file_id) or (file_id.rsplit(".", 1)[-1].lower() if "." in file_id else "")
    name = label.replace("/", "-").replace("\\", "-").strip() or file_id
    if ext and formats.from_filename(name) != ext:
        suffix = PurePosixPath(name).suffix.lower().lstrip(".")
        stem = name[: -len(suffix) - 1] if suffix and formats.get(suffix) else name
        name = f"{stem}.{ext}"
    fmt = formats.get(ext) if ext else None
    media = (fmt.mime if fmt and fmt.mime else None) or mimetypes.guess_type(f"x.{ext}")[0] or "application/octet-stream"
    return name, media


@router.get("/files/{file_id}/preview.png")
async def get_preview(file_id: str, t: str = Query(..., description="signed token"),
                      u: str = Query(tenancy.ADMIN_ID, description="workspace (signed into t)"),
                      p: int = Query(1, ge=1, description="page or slide"),
                      w: int = Query(1200, ge=200, le=2400, description="width in px")):
    """Az MCP-eredmények `preview_url`-je: a fájl egy oldala/diája PNG-ként — ugyanaz az aláírt token, mint a letöltésé,
    így egy külső AI is megnézheti, mi lett (nem csak a file_id-t látja)."""
    from app.tools import previews
    if not verify_file_token(file_id, t, uid=u):
        raise HTTPException(403, "invalid or expired token")
    tenancy.set_user(u)
    src = output_path(file_id)
    if not src.exists():
        src = input_path(file_id)
    if not src.exists():
        raise HTTPException(404, "file not found")
    if not previews.can_preview(file_id):
        raise HTTPException(415, "no preview for this file type")
    try:
        png = await previews.render(src, p, w)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(422, f"could not render a preview: {str(e)[:200]}")
    return FileResponse(png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})
