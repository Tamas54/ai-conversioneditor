"""Előnézet minden fájlhoz — egy kép, amit egy külső AI (ChatGPT, Claude) letölthet és MEGNÉZHET.

A ChatGPT-teszt (10-12) fő tanulsága: „a technikai siker és a feladat teljesülése legyen külön állapot" — a
kivágás „sikeres" volt, közben eltűnt a gitárnyak. Ehhez az ügynöknek LÁTNIA kell az eredményt, nem csak a
file_id-t. Ezért minden MCP-fájleredmény kap egy `preview_url`-t: nyilvános, aláírt link (ugyanaz a token,
mint a /files/ linké) egy PNG-re, oldalanként/diánként.

Renderelés típusonként (gyors utak, különben PDF-en át):
  kép → Pillow · psd/psb/pcraft/exr → photocraft-cli convert (lapítva) · pdf → pdftoppm
  pptx/deckcraft → deckcraft-cli render --slide · docx/odt/rtf/md/txt/html → wordcraft-cli render --page
  egyéb (svg/vectorcraft/ai/eps, designcraft, xlsx…) → PDF (anyconvert / designcraft-cli) → pdftoppm
A kész kép a felhasználó temp-mappájában gyorsítótárazva (a forrás mtime-jához kötve).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from app import tenancy
from app.config import settings

DEFAULT_W = 1200
_IMG = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff"}
_PHOTO = {"psd", "psb", "pcraft", "exr", "tga"}
_WORD = {"docx", "doc", "odt", "rtf", "md", "txt", "html", "htm"}
_DECK = {"pptx", "deckcraft"}
_VIDEO = {"mp4", "mov", "webm", "m4v"}
VIDEO_FRAMES = (0.15, 0.5, 0.85)            # videó-„oldalak”: három kocka az elejéről, a közepéről, a végéről
# ami előnézhető (a többi — zip, json, mp3… — nem kap preview_url-t)
PREVIEWABLE = _IMG | _PHOTO | _WORD | _DECK | {"pdf", "svg", "vectorcraft", "ai", "eps", "designcraft", "idml",
                                              "xlsx", "xls", "ods", "csv", "tsv", "odp", "ppt"} | _VIDEO | {"ecproj"}
_gate = asyncio.Semaphore(3)


def can_preview(file_id: str) -> bool:
    return "." in file_id and file_id.rsplit(".", 1)[1].lower() in PREVIEWABLE


def preview_url(file_id: str, page: int = 1) -> str:
    """Nyilvános, aláírt PNG-előnézet linkje (a token a fájlé — ugyanaz a jog, mint a letöltésé)."""
    from app.storage.files import sign_file_id
    uid = tenancy.current_user_id()
    token, _ = sign_file_id(file_id, uid=uid)
    user = "" if uid == tenancy.ADMIN_ID else f"&u={uid}"
    pg = f"&p={int(page)}" if page and int(page) > 1 else ""
    return f"{settings.PUBLIC_BASE_URL.rstrip('/')}/files/{file_id}/preview.png?t={token}{pg}{user}"


async def _run(*args: str, timeout: float = 90) -> None:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"{Path(args[0]).name}: timed out")
    if proc.returncode != 0:
        raise RuntimeError(err.decode("utf-8", "replace")[-300:] or f"{Path(args[0]).name} failed")


def _fresh(dst: Path, src: Path) -> bool:
    return dst.exists() and dst.stat().st_size > 0 and dst.stat().st_mtime >= src.stat().st_mtime


async def as_pdf(src: Path) -> Path:
    """A fájl PDF-változata (csendben, temp-cache; a PDF maga önmaga) — oldalszámhoz, összevetéshez, előnézethez."""
    from app.storage import temp_path
    ext = src.suffix.lstrip(".").lower()
    if ext == "pdf":
        return src
    dst = temp_path(f"pv-{src.stem}-{ext}.pdf")
    if _fresh(dst, src):
        return dst
    if ext in ("designcraft", "idml"):
        from app.tools import crafts
        await crafts.office_convert("layout", src, dst)
    elif ext in _PHOTO:
        png = await _photo_png(src)
        from app.tools.anyconvert import _image_to_pdf
        await asyncio.to_thread(_image_to_pdf, png, dst)
    else:
        from app.tools.anyconvert import to_pdf
        await to_pdf(src, dst)
    return dst


async def _photo_png(src: Path) -> Path:
    from app.storage import temp_path
    from app.tools import crafts
    dst = temp_path(f"pv-{src.stem}-flat.png")
    if not _fresh(dst, src):
        await crafts.run_cli(crafts.photo_cli(), "photocraft-cli", "convert", str(src), str(dst))
    return dst


def _shrink(src: Path, dst: Path, width: int) -> None:
    from PIL import Image, ImageOps
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.width > width:
            im = im.resize((width, max(1, round(im.height * width / im.width))), Image.Resampling.LANCZOS)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA")
        im.save(dst, "PNG", optimize=True)


def page_count_pdf(pdf: Path) -> int:
    import fitz
    with fitz.open(str(pdf)) as d:
        return d.page_count


async def page_count(src: Path) -> int:
    """Oldalak / diák / képek száma (a kép 1)."""
    ext = src.suffix.lstrip(".").lower()
    if ext in _IMG | _PHOTO:
        return 1
    if ext in _VIDEO or ext == "ecproj":
        return len(VIDEO_FRAMES)
    if ext == "pptx":
        from pptx import Presentation
        return len(Presentation(str(src)).slides)
    return await asyncio.to_thread(page_count_pdf, await as_pdf(src))


async def render(src: Path, page: int = 1, width: int = DEFAULT_W) -> Path:
    """A fájl `page`-edik oldala/diája PNG-ként (legfeljebb `width` px széles), temp-cache."""
    from app.storage import temp_path
    from app.tools import crafts, wordcraft
    ext = src.suffix.lstrip(".").lower()
    page, width = max(1, int(page or 1)), max(200, min(2400, int(width or DEFAULT_W)))
    dst = temp_path(f"pv-{src.stem}-{ext}-{page}-{width}.png")
    if _fresh(dst, src):
        return dst
    raw = temp_path(f"pv-{src.stem}-{ext}-{page}-{width}.raw.png")
    async with _gate:
        if ext == "ecproj":                      # a videóprojekt képe = az utolsó renderjének kockája
            src = _project_render(src)
            ext = src.suffix.lstrip(".").lower()
        if ext in _VIDEO:
            await asyncio.to_thread(_video_frame, src, VIDEO_FRAMES[min(page, len(VIDEO_FRAMES)) - 1], raw)
        elif ext in _IMG:
            raw = src
        elif ext in _PHOTO:
            raw = await _photo_png(src)
        elif ext == "pdf":
            await _pdftoppm(src, page, width, raw)
        elif ext in _DECK:
            # a deckcraft-cli --slide 0-tól számol (10-12: az 1. oldal kérésére a 2. diát adta, egydiás paklinál hibát)
            await crafts.run_cli(crafts.office_cli("deck"), "deckcraft-cli", "render", str(src), "--slide", str(page - 1),
                                 "--scale", f"{width / 960:.3f}", str(raw))
        elif ext in _WORD:
            await crafts.run_cli(wordcraft.cli_path(), "wordcraft-cli", "render", str(src), str(raw),
                                 "--page", str(page), "--scale", f"{width / 794:.3f}")
        else:
            await _pdftoppm(await as_pdf(src), page, width, raw)
        if not raw.exists():
            raise RuntimeError("The preview came out empty.")
        await asyncio.to_thread(_shrink, raw, dst, width)
    if raw != src and raw.parent == dst.parent and raw.name.endswith(".raw.png"):
        raw.unlink(missing_ok=True)
    return dst


def _project_render(src: Path) -> Path:
    from app.storage import read_meta
    from app.tools.crafts import existing
    rid = (read_meta(src.name) or {}).get("render_file_id")
    if not rid:
        raise ValueError("This video project has no render yet — render it first (video action=render).")
    return existing(rid)


def _video_frame(src: Path, at: float, dst: Path) -> None:
    import cv2
    c = cv2.VideoCapture(str(src))
    try:
        n = c.get(cv2.CAP_PROP_FRAME_COUNT)
        c.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(n * at) - 1))
        ok, fr = c.read()
    finally:
        c.release()
    if not ok or fr is None:
        raise RuntimeError("No frame could be read from the video.")
    cv2.imwrite(str(dst), fr)


async def _pdftoppm(pdf: Path, page: int, width: int, raw: Path) -> None:
    n = await asyncio.to_thread(page_count_pdf, pdf)
    if page > n:
        raise ValueError(f"There is no page {page} (the file has {n}).")
    await _run("pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-scale-to", str(width), "-png",
               str(pdf), str(raw.with_suffix("")))
    # a pdftoppm a megadott névhez maga teszi a .png-t
    produced = raw.with_suffix("").with_suffix(".png") if raw.suffix == ".png" else raw
    if produced != raw and produced.exists():
        produced.rename(raw)


async def render_jpeg(src: Path, page: int = 1, width: int = 1000) -> bytes:
    """Kisebb JPEG ugyanebből — MCP-képtartalomnak (a PNG túl nagy lenne a kliens kontextusába)."""
    import io
    from PIL import Image
    png = await render(src, page, width)

    def enc() -> bytes:
        with Image.open(png) as im:
            if im.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                rgba = im.convert("RGBA")
                bg.paste(rgba, mask=rgba.split()[3])
                im = bg
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=82, optimize=True)
            return buf.getvalue()
    return await asyncio.to_thread(enc)


def temp_path_for(file_id: str, t: float) -> Path:
    from app.storage import temp_path
    return temp_path(f"pv-{Path(file_id).stem}-at{int(t * 100)}.png")


def video_frame_at(src: Path, seconds: float, dst: Path) -> None:
    """Egy videókocka adott másodpercnél (a jelenetenkénti előnézethez)."""
    import cv2
    c = cv2.VideoCapture(str(src))
    try:
        fps = c.get(cv2.CAP_PROP_FPS) or 30
        n = c.get(cv2.CAP_PROP_FRAME_COUNT) or 1
        c.set(cv2.CAP_PROP_POS_FRAMES, min(n - 1, max(0, int(seconds * fps))))
        ok, fr = c.read()
    finally:
        c.release()
    if not ok or fr is None:
        raise RuntimeError("No frame could be read at that time.")
    cv2.imwrite(str(dst), fr)


def to_jpeg(src: Path, width: int = 1000) -> bytes:
    import io
    from PIL import Image
    im = Image.open(src).convert("RGB")
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82)
    return buf.getvalue()
