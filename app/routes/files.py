"""File-serving route — signed token required."""
from __future__ import annotations

import os
from typing import Optional

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


# ---------- Mentés a Fotókba (iOS, OMLETT 3.1): a megosztó lapnak való fájl ----------
# A böngésző (felhasználói gesztusra) letölti blobként, File-lá csomagolja, és navigator.share({files})-zal a
# megosztó lapra adja („Videó mentése” / „Kép mentése” → Fotók). A Fotók csak MP4/MOV (H.264/HEVC + AAC) videót és
# JPEG/PNG/HEIC/GIF képet vesz fel — ami más (WebM, MKV, VP9-es MP4, WebP, TIFF…), azt itt előbb átalakítjuk.
# Helyadat nélkül (privacy.export_path). Natív app (PHPhotoLibrary „add only”) majd később.

_PHOTO_TYPES = {"mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png", "heic": "image/heic", "heif": "image/heif", "gif": "image/gif"}
_PHOTO_VIDEO_IN = {"mp4", "m4v", "mov", "webm", "mkv", "avi", "wmv", "flv", "mpeg", "mpg", "ts", "3gp", "ogv", "ecproj"}
_PHOTO_IMAGE_IN = {"jpg", "jpeg", "png", "heic", "heif", "gif", "webp", "tif", "tiff", "bmp", "psd", "avif"}


def _photos_src(file_id: str):
    """A fájl (videóprojektnél az utolsó renderje) → útvonal."""
    from app.storage import read_meta
    from app.tools.crafts import existing
    if file_id.lower().endswith(".ecproj"):
        rid = (read_meta(file_id) or {}).get("render_file_id")
        if not rid:
            raise HTTPException(409, "Render the video first — then it can go to Photos.")
        file_id = rid
    try:
        return file_id, existing(file_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "file not found") from None


async def _photos_plan(src) -> Optional[str]:
    """None = mehet úgy, ahogy van; különben a cél-kiterjesztés (mp4 | jpg | png)."""
    ext = src.suffix.lower().lstrip(".")
    if ext in _PHOTO_IMAGE_IN:
        if ext in _PHOTO_TYPES:
            return None
        from PIL import Image
        with Image.open(src) as im:
            alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
        return "png" if alpha else "jpg"
    if ext not in _PHOTO_VIDEO_IN:
        raise HTTPException(415, "Only videos and pictures go to Photos — use Download for this file.")
    if ext in ("mp4", "m4v", "mov"):
        from app.convert.steps import probe
        info = await probe(src)
        v = [s.get("codec_name") for s in info.get("streams", []) if s.get("codec_type") == "video"]
        a = [s.get("codec_name") for s in info.get("streams", []) if s.get("codec_type") == "audio"]
        if v and v[0] in ("h264", "hevc") and all(c in ("aac", "alac") for c in a[:1]):
            return None
    return "mp4"


def _photos_cache(src, ext: str):
    from app.storage import temp_path
    st = src.stat()
    d = temp_path("photos")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{src.stem}-{st.st_mtime_ns}-{st.st_size}.{ext}"


async def _photos_convert(src, dst) -> None:
    from app.convert.engines import run
    tmp = dst.with_name(f".{dst.name}")
    try:
        if dst.suffix == ".mp4":
            from app import jobs
            async with jobs.heavy():                    # videókódolás: a közös kapun át (app/jobs.py)
                await run("ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
                          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                          "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:a", "aac", "-b:a", "192k",
                          "-map_metadata", "-1", "-movflags", "+faststart", "-f", "mp4", str(tmp), timeout=3600)
        else:
            from PIL import Image, ImageOps

            def go():
                with Image.open(src) as im:
                    im = ImageOps.exif_transpose(im)
                    if dst.suffix == ".jpg":
                        im.convert("RGB").save(tmp, "JPEG", quality=92)
                    else:
                        im.save(tmp, "PNG")
            import asyncio
            await asyncio.to_thread(go)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def _photos_name(file_id: str, ext: str) -> str:
    name, _ = download_meta(file_id, display_label(file_id) or file_id)
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return f"{stem}.{ext}"


@router.get("/api/photos/{file_id}")
async def photos_prepare(file_id: str) -> dict:
    """Mentés a Fotókba: {url, name, type, size, converted} — vagy job, ha előbb át kell alakítani (WebM → MP4)."""
    from app import jobs
    fid, src = _photos_src(file_id)
    want = await _photos_plan(src)
    out = {"url": f"/api/photos/{file_id}/file", "converted": bool(want)}
    if want is None:
        ext = src.suffix.lower().lstrip(".")
        return {**out, "name": _photos_name(fid, ext), "type": _PHOTO_TYPES[ext], "size": src.stat().st_size}
    dst = _photos_cache(src, want)
    ready = {**out, "name": _photos_name(fid, want), "type": _PHOTO_TYPES[want]}
    if dst.exists():
        return {**ready, "size": dst.stat().st_size}

    async def work(progress):
        progress(0, 1, "converting for Photos")
        await _photos_convert(src, dst)
        return {**ready, "size": dst.stat().st_size}
    return jobs.start("photos", work)


@router.get("/api/photos/{file_id}/file")
async def photos_file(file_id: str):
    from app.storage import privacy
    fid, src = _photos_src(file_id)
    want = await _photos_plan(src)
    if want is None:
        ext = src.suffix.lower().lstrip(".")
        path, name, media = await privacy.export_path(src), _photos_name(fid, ext), _PHOTO_TYPES[ext]
    else:
        path = _photos_cache(src, want)
        if not path.exists():
            raise HTTPException(409, "Not ready yet — prepare it first (GET /api/photos/<file_id>).")
        path, name, media = await privacy.export_path(path), _photos_name(fid, want), _PHOTO_TYPES[want]
    return FileResponse(path, filename=name, media_type=media, headers={"Cache-Control": "private, no-store"})
