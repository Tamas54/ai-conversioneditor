"""A termék webes felülete: /app (kezdőképernyő, fájlok, AI-kapcsolat, fájlnézet — egy oldal, hash-útvonalak)."""
from __future__ import annotations

import functools
import hashlib
import html
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings

router = APIRouter()
APP_HTML = Path(__file__).parent.parent / "templates" / "app.html"
STATIC = Path(__file__).parent.parent / "static"


def static_v(name: str) -> str:
    """/static/<név>?v=<tartalom-hash> — a stíluslapnak nincs Cache-Control-ja, a böngésző heurisztikusan
    a régit tartotta meg deploy után (új HTML + régi CSS = szétesett ejtőzóna, 10-12)."""
    p = STATIC / name
    try:
        st = p.stat()
    except OSError:
        return f"/static/{name}"
    return f"/static/{name}?v={_digest(str(p), st.st_mtime_ns, st.st_size)}"


@functools.lru_cache(maxsize=16)
def _digest(path: str, mtime_ns: int, size: int) -> str:
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()[:10]


@router.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/app", status_code=301)


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    from fastapi.responses import FileResponse
    return FileResponse(Path(__file__).parent.parent / "static" / "favicon.ico", media_type="image/x-icon",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/app", include_in_schema=False)
@router.get("/app/{rest:path}", include_in_schema=False)
async def app_page(rest: str = "") -> HTMLResponse:
    """Egy oldal, STATIKUS útvonalakkal (/app/make/<eszköz>/<mód>, /app/file/<id>/edit/<szerkesztő>, /app/files…):
    a szerver útvonalanként adja a címet, leírást, kanonikus linket, schema.org-ot és az eszköz látható ismertetőjét."""
    from app import seo
    meta = seo.page_for(rest)
    e = html.escape
    page = (APP_HTML.read_text("utf-8")
            .replace("{{TITLE}}", e(meta["title"])).replace("{{DESCRIPTION}}", e(meta["desc"]))
            .replace("{{CANONICAL}}", e(meta["canonical"])).replace("{{SEO_HEAD}}", seo.head_html(meta))
            .replace("{{ABOUT}}", meta["about"])
            .replace("{{PRODUCT}}", e(settings.PRODUCT_NAME)).replace("{{BASE}}", e(settings.PUBLIC_BASE_URL.rstrip("/")))
            .replace('href="/static/product.css"', f'href="{static_v("product.css")}"'))
    return HTMLResponse(page, headers={"Cache-Control": "no-cache", **seo.robots_header(meta)})


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
        await crafts.run_cli(crafts.office_cli("deck"), "deckcraft-cli", "render", str(src), "--slide", "0", "--scale", "0.5", str(raw))   # 0-tól számol: az 1. dia
    elif ext in ("docx", "doc", "odt", "rtf", "md", "txt", "html", "htm"):
        await crafts.run_cli(wordcraft.cli_path(), "wordcraft-cli", "render", str(src), str(raw), "--page", "1", "--scale", "0.6")
    elif ext in ("psd", "psb", "pcraft", "exr"):
        await crafts.run_cli(crafts.photo_cli(), "photocraft-cli", "convert", str(src), str(raw))
    elif ext in ("svg", "vectorcraft", "ai", "eps"):
        await crafts.run_cli(crafts.vector_cli(), "vectorcraft-cli", "run", "--in", str(src), "--export", str(raw), "--scale", "0.6")
    elif ext == "ecproj" or ext in ("mp4", "mov", "webm", "m4v"):
        from app.tools import video          # videó: projektnél a címjelenet egy kockája, filmnél az 1. másodperc
        await video.thumb(src, raw)
    else:
        raise HTTPException(404, "no thumbnail for this type")
    if not raw.exists():
        raise HTTPException(404, "render produced nothing")
    await asyncio.to_thread(_shrink, raw, dst)
    if raw != src:
        raw.unlink(missing_ok=True)


_print_gate = asyncio.Semaphore(2)


@router.get("/api/print/{file_id}", include_in_schema=False)
async def print_pdf(file_id: str):
    """Nyomtatható PDF bármelyik fájlból — a böngésző ezt nyomtatja. PDF: maga; más: PDF-re alakítva, temp-cache
    (a forrás módosításáig), verzió-sor nélkül."""
    from app.storage import temp_path
    from app.tools import anyconvert
    from app.tools.crafts import existing
    try:
        src = existing(file_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404)
    headers = {"Content-Disposition": 'inline; filename="print.pdf"', "Cache-Control": "private, max-age=60"}
    if src.suffix.lower() == ".pdf":
        return FileResponse(src, media_type="application/pdf", headers=headers)
    dst = temp_path(f"print-{src.stem}-{src.suffix.lstrip('.')}.pdf")
    if not (dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime):
        async with _print_gate:
            try:
                await anyconvert.to_pdf(src, dst)
            except ValueError as e:
                raise HTTPException(415, str(e))
            except Exception:
                dst.unlink(missing_ok=True)
                raise HTTPException(422, "Could not prepare this file for printing — download it and print from your computer.")
    return FileResponse(dst, media_type="application/pdf", headers=headers)


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
