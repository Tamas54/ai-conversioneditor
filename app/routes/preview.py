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


def _inline_disposition(file_id: str, ext: str) -> str:
    """Content-Disposition ékezetes névvel is: a HTTP-fejléc latin-1, ezért ASCII tartalék-név +
    RFC 5987 filename*=UTF-8''… („Budapest a jövőben.pptx” előnézete 500-at dobott)."""
    import unicodedata
    from urllib.parse import quote
    name = _preview_filename(file_id, ext)
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().replace('"', "") or f"preview.{ext}"
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


def _preview_filename(file_id: str, ext: str) -> str:
    """Build a sensible filename for the preview download. Strips any old
    extension from the label and appends the preview format's extension —
    so saving a DOCX preview gets `page28.pdf`, not `page28.docx.pdf` and
    definitely not the raw uuid+wrong extension that becomes a kentaur."""
    label = display_label(file_id) or file_id
    stem = label.rsplit(".", 1)[0] if "." in label else label
    return f"{stem}.{ext}"


WORDCRAFT_PREVIEW_FORMATS = {"docx", "doc", "odt", "rtf"}
# A böngésző nem rajzolja → PNG-re rajzoljuk: PhotoCraft (a rétegek összképe), VectorCraft (a dizájn, ahogy kinyomtatnák)
PHOTOCRAFT_PREVIEW_FORMATS = {"psd", "psb", "tif", "tiff", "pcraft", "exr"}
VECTORCRAFT_PREVIEW_FORMATS = {"vectorcraft", "ai", "eps"}
VIDEO_PREVIEW_FORMATS = {"mp4", "webm", "mov", "m4v"}

# Videó-előnézet: a böngésző lejátszója, illesztve (álló videó is egészben látszik)
_VIDEO_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title><style>
:root {{ --bg: #F1F2EE; color-scheme: light dark; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #202526; }} }}
html, body {{ margin: 0; height: 100%; background: var(--bg); }}
body {{ display: grid; place-items: center; overflow: hidden; }}
video {{ display: block; max-width: calc(100vw - 32px); max-height: calc(100vh - 32px); background: #000; box-shadow: 0 6px 24px rgba(0,0,0,.18); }}
</style></head><body><video src="{src}" poster="{poster}" controls playsinline preload="metadata" aria-label="{name}"></video></body></html>"""

# Kép-előnézet a fájlnézetben: az EGÉSZ kép látszik (illesztve), kattintásra 100% ↔ illesztés; az átlátszó
# rész (kivágott alak) sakktáblán. A nyers /preview/<id> marad a régi fogyasztóknak.
_FIT_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title><style>
:root {{ --bg: #F1F2EE; --a: #FFFFFF; --b: #E4E6E1; --ink: #5C6870; color-scheme: light dark; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #202526; --a: #2A3032; --b: #22282A; --ink: #9AA6AC; }} }}
html, body {{ margin: 0; height: 100%; background: var(--bg); }}
body {{ display: grid; place-items: center; overflow: hidden; }}
body.full {{ display: block; overflow: auto; }}
img {{ display: block; max-width: calc(100vw - 32px); max-height: calc(100vh - 32px); object-fit: contain; cursor: zoom-in;
  background: repeating-conic-gradient(var(--a) 0 25%, var(--b) 0 50%) 0 0 / 20px 20px; box-shadow: 0 6px 24px rgba(0,0,0,.18); }}
body.full img {{ max-width: none; max-height: none; margin: 16px; cursor: zoom-out; }}
.hint {{ position: fixed; right: 10px; bottom: 8px; font: 500 11px/1 ui-monospace, monospace; color: var(--ink); opacity: .8; pointer-events: none; }}
</style></head><body><img src="{src}" alt="{name}"><span class="hint" id="h"></span>
<script>
const img = document.querySelector("img"), h = document.getElementById("h");
const show = () => {{ if (!img.naturalWidth) return; const z = img.clientWidth / img.naturalWidth;
  h.textContent = img.naturalWidth + " × " + img.naturalHeight + " px · " + (document.body.classList.contains("full") ? 100 : Math.round(Math.min(1, z) * 100)) + "%"; }};
img.addEventListener("load", show); addEventListener("resize", show);
img.addEventListener("click", () => {{ document.body.classList.toggle("full"); show(); }});
</script></body></html>"""


async def _render_png(src: Path, file_id: str, mtime: int, ext: str) -> Path:
    """PSD/TIFF… (PhotoCraft) vagy dizájn (VectorCraft) → PNG (cache: mtime szerint)."""
    from app.tools import crafts
    cache = PREVIEW_DIR / f"{file_id}_{mtime}.png"
    if not cache.exists():
        tmp = cache.with_suffix(".tmp.png")
        if ext in VECTORCRAFT_PREVIEW_FORMATS:
            await crafts.run_cli(crafts.vector_cli(), "vectorcraft-cli", "run", "--in", str(src), "--export", str(tmp), "--scale", "1")
        else:
            await crafts.run_cli(crafts.photo_cli(), "photocraft-cli", "convert", str(src), str(tmp))
        if not tmp.exists():
            raise HTTPException(404, "could not render a preview")
        tmp.replace(cache)
    return cache


_PAGES_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title><style>
:root {{ --bg: #F1F2EE; color-scheme: light dark; }} @media (prefers-color-scheme: dark) {{ :root {{ --bg: #202526; }} }}
html, body {{ margin: 0; background: var(--bg); }}
main {{ display: grid; gap: 12px; padding: 12px; justify-items: center; }}
img {{ display: block; width: 100%; max-width: 900px; height: auto; background: #fff; box-shadow: 0 4px 16px rgba(0,0,0,.16); }}
</style></head><body><main>{pages}</main></body></html>"""


def _pdf_page_png(src: Path, file_id: str, mtime: int, page: int) -> Path:
    """Egy PDF-oldal PNG-ként (telefonon a böngésző nem rajzol PDF-et iframe-ben) — cache: mtime szerint."""
    import fitz
    cache = PREVIEW_DIR / f"{file_id}_{mtime}_p{page}.png"
    if not cache.exists():
        with fitz.open(src) as doc:
            if not 1 <= page <= doc.page_count:
                raise HTTPException(404, "no such page")
            doc[page - 1].get_pixmap(dpi=110).save(cache)
    return cache


@router.get("/preview/{file_id}")
async def preview(file_id: str, request: Request, view: str = "", page: int = 0):
    src = _resolve(file_id)
    fmt = detect_from_path(src)
    ext = src.suffix.lstrip(".").lower()
    mtime = int(src.stat().st_mtime)

    if fmt == "pdf" and page:
        import asyncio
        png = await asyncio.to_thread(_pdf_page_png, src, file_id, mtime, page)
        return FileResponse(png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})
    if fmt == "pdf" and view == "pages":
        import fitz
        from html import escape
        try:
            with fitz.open(src) as doc:
                n = 0 if doc.needs_pass else doc.page_count
                w, h = (doc[0].rect.width, doc[0].rect.height) if n else (595, 842)
        except Exception:
            n, w, h = 0, 595, 842
        if n:           # jelszavas / sérült PDF: marad a sima PDF-válasz lent
            imgs = "".join(f'<img src="/preview/{file_id}?page={i}" alt="Page {i}" loading="lazy" width="{int(w)}" height="{int(h)}">'
                           for i in range(1, n + 1))
            return HTMLResponse(_PAGES_PAGE.format(name=escape(_preview_filename(file_id, "pdf")), pages=imgs),
                                headers={"Cache-Control": "no-cache"})

    if ext in VIDEO_PREVIEW_FORMATS or ext == "ecproj":
        # film: lejátszó (a /files/ link Range-kéréseket is kiszolgál); videóprojekt: a legutóbbi renderje, vagy egy kocka
        from html import escape
        from app.storage import public_url, read_meta
        movie = file_id if ext != "ecproj" else (read_meta(file_id) or {}).get("render_file_id")
        if movie and not (output_path(movie).exists() or input_path(movie).exists()):
            movie = None
        if not movie:     # még nem volt render: a címjelenet egy kockája
            return HTMLResponse(_FIT_PAGE.format(name=escape(_preview_filename(file_id, "png")), src=f"/api/thumb/{file_id}"),
                                headers={"Cache-Control": "no-cache"})
        u = public_url(movie)
        return HTMLResponse(_VIDEO_PAGE.format(name=escape(_preview_filename(movie, movie.rsplit(".", 1)[-1])),
                                               src=escape(u[u.index("/files/"):] if "/files/" in u else u),
                                               poster=f"/api/thumb/{file_id}"),
                            headers={"Cache-Control": "no-cache"})

    if ext in PHOTOCRAFT_PREVIEW_FORMATS | VECTORCRAFT_PREVIEW_FORMATS:
        if view == "fit":
            from html import escape
            return HTMLResponse(_FIT_PAGE.format(name=escape(_preview_filename(file_id, ext)), src=f"/preview/{file_id}?view=png"),
                                headers={"Cache-Control": "no-cache"})
        try:
            png = await _render_png(src, file_id, mtime, ext)
        except HTTPException:
            raise
        except Exception as e:
            log.warning("PNG-előnézet nem sikerült (%s): %s", file_id, e)
            raise HTTPException(404, "could not render a preview")
        return FileResponse(png, media_type="image/png", headers={"Content-Disposition": _inline_disposition(file_id, "png")})

    if view == "fit" and fmt in IMAGE_FORMATS:
        from html import escape
        return HTMLResponse(_FIT_PAGE.format(name=escape(_preview_filename(file_id, fmt)), src=f"/preview/{file_id}"),
                            headers={"Cache-Control": "no-cache"})

    # Direct serve formats
    if fmt == "pdf":
        return FileResponse(
            src, media_type="application/pdf",
            headers={"Content-Disposition": _inline_disposition(file_id, "pdf")},
        )

    if fmt in IMAGE_FORMATS:
        return FileResponse(
            src, media_type=IMAGE_MIME.get(fmt, "image/png"),
            headers={"Content-Disposition": _inline_disposition(file_id, fmt)},
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
        if not cache_path.exists() and fmt in WORDCRAFT_PREVIEW_FORMATS:
            # Word-szerű dokumentum: a WordCraft ms-ok alatt rajzol, és ugyanúgy tördel, mint a
            # szerkesztő (Caladea/Carlito mindkettőben); hibánál marad a LibreOffice.
            try:
                from app.tools import wordcraft
                if wordcraft.available():
                    await wordcraft.convert_file(src, cache_path)
            except Exception as e:
                log.warning("WordCraft-előnézet nem sikerült (%s), LibreOffice jön: %s", file_id, e)
                cache_path.unlink(missing_ok=True)
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
                "Content-Disposition": _inline_disposition(file_id, "pdf"),
            },
        )

    # Unknown — fall through to text
    try:
        return PlainTextResponse(src.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return Response(content=src.read_bytes(), media_type="application/octet-stream")
