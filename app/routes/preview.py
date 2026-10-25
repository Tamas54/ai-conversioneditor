"""
Preview route — converts files on-the-fly to a browser-renderable form
so the dashboard can show DOCX/PPTX/XLSX/ODT/RTF previews via PDF.

Cache key: {file_id}_{mtime}.pdf in /data/temp/previews/.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

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

# Videóprojekt szakasz-gyorsítótáras előnézettel (meta["preview"], app/tools/preview_cache.py): HLS-lejátszó, alatta
# az idővonal a változások jelölőivel, a szakaszok és a végleges render állapota. A lejátszó: app/static/preview-player.js.
HLS_JS = "https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.13/hls.min.js"
HLS_SRI = "sha512-bqtMkEqGSOPPb8qbYSd9gHwR7C5mgmN+Z5Uaquvhana/U9H3zppGvlLV9HOdM7eYHF/GzzhS1lOGzCMhUvYcjQ=="   # a cdnjs SRI-je
_PLAYER_JS = Path(__file__).resolve().parent.parent / "static" / "preview-player.js"
_HLS_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title><style>
:root {{ --bg: #F1F2EE; --ink: #15191B; --muted: #5C6870; --rule: #CDD2CB; --card: #FFFFFF; --mark: #E2431F;
  --tip: #15191B; --tip-ink: #FBFBF8; color-scheme: light dark; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #202526; --ink: #ECEFEC; --muted: #9AA6AC; --rule: #3A4346; --card: #2A3032;
  --mark: #FF5A36; --tip: #ECEFEC; --tip-ink: #191D1E; }} }}
* {{ box-sizing: border-box; }}
[hidden] {{ display: none !important; }}
html, body {{ margin: 0; min-height: 100%; background: var(--bg); color: var(--ink); overflow-x: hidden; }}
body {{ display: grid; place-items: center; min-height: 100vh; padding: 16px;
  font: 13px/1.4 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; -webkit-font-smoothing: antialiased; }}
.pl {{ width: min(100%, max(640px, calc((100vh - 150px) * {ar}))); display: grid; gap: 6px; }}
video {{ display: block; margin: 0 auto 6px; background: #000; box-shadow: 0 6px 24px rgba(0,0,0,.18);
  aspect-ratio: {w} / {h}; width: min(100%, max(160px, calc((100vh - 150px) * {ar}))); }}
.bar {{ position: relative; height: 28px; cursor: pointer; touch-action: manipulation; }}
.rail {{ position: absolute; left: 0; right: 0; top: 12px; height: 4px; border-radius: 2px; background: var(--rule); }}
.mk {{ all: unset; box-sizing: border-box; position: absolute; top: 5px; height: 18px; min-width: 6px; margin-left: -3px;
  border-radius: 3px; background: var(--mark); cursor: pointer; opacity: .82; }}
.mk::before {{ content: ""; position: absolute; inset: -5px -9px; }}
.mk:hover, .mk.on {{ opacity: 1; }}
.mk:focus-visible {{ opacity: 1; outline: 2px solid var(--ink); outline-offset: 2px; }}
.tip {{ position: absolute; bottom: calc(100% + 8px); left: 50%; transform: translateX(-50%); z-index: 2; max-width: min(320px, 80vw);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding: 5px 8px; border-radius: 6px;
  background: var(--tip); color: var(--tip-ink); font-weight: 600; font-size: 12px; pointer-events: none; visibility: hidden; }}
.mk:hover .tip, .mk:focus-visible .tip {{ visibility: visible; }}
.head {{ position: absolute; top: 1px; bottom: 1px; width: 2px; margin-left: -1px; border-radius: 1px; background: var(--ink); pointer-events: none; }}
.head::after {{ content: ""; position: absolute; top: 8px; left: -4px; width: 10px; height: 10px; border-radius: 50%;
  background: var(--ink); box-shadow: 0 0 0 2px var(--bg); }}
.row {{ display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; }}
.time {{ font: 500 12px/1 ui-monospace, SFMono-Regular, Menlo, monospace; font-variant-numeric: tabular-nums; }}
.note {{ color: var(--muted); flex: 1 1 auto; }}
.which {{ display: inline-flex; margin-left: auto; border: 1px solid var(--rule); border-radius: 8px; overflow: hidden; background: var(--card); }}
.which button {{ all: unset; box-sizing: border-box; cursor: pointer; white-space: nowrap; font: inherit; font-weight: 600; font-size: 12px; line-height: 1; padding: 8px 10px; color: var(--muted); }}
.which button + button {{ border-left: 1px solid var(--rule); }}
.which button:hover {{ color: var(--ink); }}
.which button[aria-pressed="true"] {{ background: var(--ink); color: var(--bg); cursor: default; }}
.which button:focus-visible, .act:focus-visible {{ outline: 2px solid var(--mark); outline-offset: -2px; }}
.st {{ margin: 0; color: var(--muted); }}
.act {{ font: inherit; font-weight: 600; font-size: 12px; line-height: 1; padding: 6px 10px; border: 1px solid var(--rule); border-radius: 6px; background: var(--card); color: var(--ink); cursor: pointer; }}
</style></head><body>
<div class="pl" id="pl" data-hls-js="{hls_js}" data-hls-sri="{hls_sri}">
<video id="pv" controls playsinline preload="metadata" poster="{poster}" aria-label="{name}"></video>
<div class="bar" id="pv-bar"><div class="rail"></div><div id="pv-marks" role="group" aria-label="Changes in this version"></div><div class="head" id="pv-head" hidden></div></div>
<div class="row"><span class="time" id="pv-time">0:00</span><span class="note" id="pv-note"></span>
<span class="which" id="pv-which" role="group" aria-label="Which video plays" hidden><button type="button" data-src="preview" aria-pressed="true">Preview</button><button type="button" data-src="final" aria-pressed="false">Final video</button></span></div>
<p class="st" id="pv-seg" hidden></p><p class="st" id="pv-final" role="status" hidden></p><p class="st" id="pv-msg" role="alert" hidden></p>
</div>
<script type="application/json" id="pv-cfg">{cfg}</script>
<script src="/static/preview-player.js?v={ver}" defer></script>
</body></html>"""

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


def _same_origin(u: str) -> str:
    """Abszolút link (PUBLIC_BASE_URL) → a lap saját eredetéről kért útvonal (/api/… vagy /files/…)."""
    for p in ("/api/", "/files/"):
        if p in u:
            return u[u.index(p):]
    return u


def _hls_page(file_id: str, meta: dict) -> Optional[HTMLResponse]:
    """Videóprojekt, aminek a verziójához szakasz-gyorsítótáras előnézet van (meta["preview"], VIDEO_PREVIEW_HLS ≠ 0):
    a HLS-lejátszó lapja — különben None (marad a mai lap: a legutóbbi render, vagy egy kocka)."""
    import json
    from html import escape
    from urllib.parse import quote
    pv = meta.get("preview")
    if not isinstance(pv, dict):
        return None
    try:
        from app.tools import preview_cache, preview_serve
    except ImportError:            # a gyorsítótár modulja nélkül nincs lista: a mai lap
        return None
    if not preview_cache.enabled():
        return None
    q = (preview_serve.signed(file_id) or "").strip()    # „?t=<exp>.<sig>” (a projekt fölött, mint a /files/ link)
    q = q if q.startswith("?") else "?" + q if "=" in q else "?t=" + q
    base = f"/api/video/{quote(file_id)}"
    video = meta.get("video") or {}
    try:
        w, h = max(1, int(video.get("width") or 16)), max(1, int(video.get("height") or 9))
    except (TypeError, ValueError):
        w, h = 16, 9
    markers = []
    for m in pv.get("markers") or []:
        try:
            markers.append({"from": float(m["from"]), "to": float(m.get("to", m["from"])), "what": str(m.get("what") or "")})
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    final = None            # a végleges render már kész (a preview.json mondja a többit: sorban, renderel, hiba)
    rid = meta.get("render_file_id")
    if rid and not meta.get("render_draft") and (output_path(rid).exists() or input_path(rid).exists()):
        from app.storage import public_url
        final = {"status": "done", "video_url": _same_origin(public_url(rid))}
    try:
        dur = float(video.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    cfg = {"pid": file_id, "playlist": f"{base}/preview.m3u8{q}", "json": f"{base}/preview.json{q}",
           "duration": dur, "markers": markers, "final": final}
    label = display_label(file_id) or file_id
    stem = label.rsplit(".", 1)[0] if "." in label else label
    try:
        ver = int(_PLAYER_JS.stat().st_mtime)
    except OSError:
        ver = 0
    # a JSON a <script>-ben: a „<” kódolva, hogy egy rétegnév („</script>…”) se zárhassa le
    return HTMLResponse(_HLS_PAGE.format(name=escape(f"{stem} — preview"), poster=f"/api/thumb/{quote(file_id)}", w=w, h=h,
                                         ar=f"{w / h:.5f}", hls_js=HLS_JS, hls_sri=HLS_SRI, ver=ver,
                                         cfg=json.dumps(cfg, ensure_ascii=False).replace("<", "\\u003c")),
                        headers={"Cache-Control": "no-cache"})


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
        meta = (read_meta(file_id) or {}) if ext == "ecproj" else {}
        page = _hls_page(file_id, meta) if ext == "ecproj" else None
        if page is not None:      # szakasz-gyorsítótáras előnézet: HLS-lejátszó a változások jelölőivel
            return page
        movie = file_id if ext != "ecproj" else meta.get("render_file_id")
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
        from app.storage import privacy
        try:
            src = await privacy.export_path(src)        # a böngészőből menthető: helyadat (GPS) nélkül
        except privacy.LocationNotRemoved as e:
            raise HTTPException(422, str(e)) from None
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
