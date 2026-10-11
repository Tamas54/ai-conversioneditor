"""A termék webes felülete: /app (kezdőképernyő, fájlok, AI-kapcsolat, fájlnézet — egy oldal, hash-útvonalak)."""
from __future__ import annotations

import html
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings

router = APIRouter()
APP_HTML = Path(__file__).parent.parent / "templates" / "app.html"


@router.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/app", status_code=303)


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    from fastapi.responses import FileResponse
    return FileResponse(Path(__file__).parent.parent / "static" / "favicon.ico", media_type="image/x-icon",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/app", include_in_schema=False)
async def app_page() -> HTMLResponse:
    page = (APP_HTML.read_text("utf-8").replace("{{PRODUCT}}", html.escape(settings.PRODUCT_NAME))
            .replace("{{BASE}}", html.escape(settings.PUBLIC_BASE_URL.rstrip("/"))))
    return HTMLResponse(page, headers={"Cache-Control": "no-cache"})


# ---------- bélyegképek a fájlkártyákhoz ----------
# Első oldal / dia / kép kicsiben; a felhasználó temp-mappájában gyorsítótárazva (a forrás mtime-jához kötve).
# Amit nem tudunk renderelni → 404, a felület ilyenkor a típus-ikont mutatja.

import asyncio  # noqa: E402

from fastapi import HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

THUMB_W = 480
_thumb_gate = asyncio.Semaphore(3)   # egy fájllista ne indítson egyszerre 30 renderelőt
_IMG = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff"}


async def _run(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.wait(), timeout=40)
    except asyncio.TimeoutError:
        proc.kill()
        raise


def _shrink(src: Path, dst: Path) -> None:
    from PIL import Image
    with Image.open(src) as im:
        im.thumbnail((THUMB_W, THUMB_W * 2))
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA")
        im.save(dst, "PNG", optimize=True)


async def _render_thumb(src: Path, ext: str, dst: Path) -> None:
    from app.tools import crafts, wordcraft
    raw = dst.with_suffix(".raw.png")
    if ext in _IMG:
        raw = src
    elif ext == "pdf":
        await _run("pdftoppm", "-f", "1", "-l", "1", "-singlefile", "-scale-to", str(THUMB_W), "-png", str(src), str(raw.with_suffix("")))
    elif ext in ("pptx", "deckcraft"):
        await crafts.run_cli(crafts.office_cli("deck"), "deckcraft-cli", "render", str(src), "--slide", "1", "--scale", "0.5", str(raw))
    elif ext in ("docx", "doc", "odt", "rtf", "md", "txt", "html", "htm"):
        await crafts.run_cli(wordcraft.cli_path(), "wordcraft-cli", "render", str(src), str(raw), "--page", "1", "--scale", "0.6")
    elif ext in ("psd", "psb", "pcraft", "exr"):
        await crafts.run_cli(crafts.photo_cli(), "photocraft-cli", "convert", str(src), str(raw))
    elif ext in ("svg", "vectorcraft", "ai", "eps"):
        await crafts.run_cli(crafts.vector_cli(), "vectorcraft-cli", "run", "--in", str(src), "--export", str(raw), "--scale", "0.6")
    else:
        raise HTTPException(404, "no thumbnail for this type")
    if not raw.exists():
        raise HTTPException(404, "render produced nothing")
    await asyncio.to_thread(_shrink, raw, dst)
    if raw != src:
        raw.unlink(missing_ok=True)


@router.get("/api/thumb/{file_id}", include_in_schema=False)
async def thumb(file_id: str):
    from app.storage import temp_path
    from app.tools.crafts import existing
    try:
        src = existing(file_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404)
    ext = src.suffix.lstrip(".").lower()
    dst = temp_path(f"thumb-{src.stem}.png")
    if not (dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime):
        async with _thumb_gate:
            try:
                await _render_thumb(src, ext, dst)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(404, "could not render a thumbnail")
    return FileResponse(dst, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})
