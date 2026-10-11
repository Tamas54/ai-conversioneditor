"""Dokumentum → pakli-forrás: a szöveg vázlatként (címsorok, felsorolások, táblázatok) + a képek a HELYÜKÖN jelölve.

docx / odt / rtf / html / md / txt → pandoc gfm (`--extract-media`): címsor = #, lista = -, a képek `[picture DOCIMG#k]`
doc                               → WordCraft-tal docx, aztán mint fent
pdf                               → oldalanként a szöveg (PyMuPDF), a nagyobb képek az oldaluk végén jelölve
A képek ideiglenes, rejtett (ephemeral) fájlokként mentődnek, így a paklira `{"file_id": …}`-ként kerülhetnek fel.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

MAX_CHARS = 40_000          # ennyi szöveg megy a modellnek (≈ 25–30 oldal); a fölötte lévő rész kimarad, ezt jelezzük
MIN_PICTURE_PX = 160        # ennél kisebb kép (ikon, díszítés, logó-morzsa) nem kerül diára
DOCS = {"docx", "doc", "odt", "rtf", "html", "htm", "md", "txt", "pdf"}


def _keep_picture(path: Path) -> bool:
    try:
        from PIL import Image
        with Image.open(path) as im:
            return min(im.size) >= MIN_PICTURE_PX
    except Exception:
        return False


def _save_picture(path: Path, source_id: str) -> str:
    """Kép → rejtett bemeneti fájl (jpg/png), a file_id-t adja vissza."""
    from PIL import Image
    from app.storage import input_path, write_meta
    from app.storage.files import new_file_id
    with Image.open(path) as im:
        has_alpha = im.mode in ("RGBA", "LA", "P") and "transparency" in im.info or im.mode in ("RGBA", "LA")
        ext = "png" if has_alpha else "jpg"
        fid = new_file_id(ext)
        im = im.convert("RGBA" if has_alpha else "RGB")
        im.save(input_path(fid), "PNG" if has_alpha else "JPEG", quality=90)
    write_meta(fid, source_file_id=source_id, operation="picture from document", extra={"ephemeral": True})
    return fid


def _from_pandoc(src: Path, fmt: str, source_id: str) -> tuple[str, list[dict[str, Any]]]:
    media = Path(tempfile.mkdtemp())
    try:
        md = subprocess.run(["pandoc", "-f", fmt, "-t", "gfm", "--wrap=none", f"--extract-media={media}", str(src)],
                            capture_output=True, text=True, check=True, timeout=120).stdout
        pics: list[dict[str, Any]] = []

        def pic(alt: str, ref: str) -> str:
            p = Path(ref) if Path(ref).is_absolute() else media / ref
            if not p.exists():
                cand = list(media.rglob(Path(ref).name))
                p = cand[0] if cand else p
            if not p.exists() or not _keep_picture(p):
                return ""
            k = len(pics) + 1
            pics.append({"token": f"DOCIMG#{k}", "file_id": _save_picture(p, source_id), "alt": alt.strip()})
            return f"[picture DOCIMG#{k}{': ' + alt.strip() if alt.strip() else ''}]"
        md = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)(\{[^}]*\})?", lambda m: pic(m.group(1) or "", m.group(2)), md)
        # a méretezett képet a pandoc <img src=…>-ként adja
        md = re.sub(r"<img [^>]*?src=\"([^\"]+)\"[^>]*>", lambda m: pic("", m.group(1)), md)
        return md, pics
    finally:
        shutil.rmtree(media, ignore_errors=True)


def _from_pdf(src: Path, source_id: str) -> tuple[str, list[dict[str, Any]]]:
    import pymupdf as fitz
    doc = fitz.open(str(src))
    parts, pics = [], []
    tmp = Path(tempfile.mkdtemp())
    try:
        for pno, page in enumerate(doc, 1):
            text = page.get_text("text").strip()
            parts.append(f"--- page {pno} ---\n{text}")
            for img in page.get_images(full=True):
                if len(pics) >= 40:
                    break
                try:
                    pix = fitz.Pixmap(doc, img[0])
                    if pix.n - pix.alpha >= 4:
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    if min(pix.width, pix.height) < MIN_PICTURE_PX:
                        continue
                    p = tmp / f"p{pno}_{img[0]}.png"
                    pix.save(str(p))
                except Exception:
                    continue
                k = len(pics) + 1
                pics.append({"token": f"DOCIMG#{k}", "file_id": _save_picture(p, source_id), "alt": ""})
                parts.append(f"[picture DOCIMG#{k}]")
        return "\n\n".join(parts), pics
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def read(file_id: str) -> dict[str, Any]:
    """{"text", "pictures": [{token, file_id, alt}], "truncated": bool, "title"}"""
    from app.storage import display_label
    from app.tools.crafts import existing
    src = existing(file_id)
    ext = src.suffix.lstrip(".").lower()
    if ext not in DOCS:
        raise ValueError(f"Slides can be made from Word, PDF, Markdown or text files — not .{ext}.")
    tmpdoc = None
    try:
        if ext == "doc":
            from app.tools import wordcraft
            tmpdoc = Path(tempfile.mkdtemp()) / "source.docx"
            await wordcraft.convert_file(src, tmpdoc)
            src, ext = tmpdoc, "docx"
        if ext == "pdf":
            text, pics = await asyncio.to_thread(_from_pdf, src, file_id)
        elif ext == "txt":
            text, pics = src.read_text("utf-8", errors="replace"), []
        else:
            fmt = {"md": "markdown", "htm": "html"}.get(ext, ext)
            text, pics = await asyncio.to_thread(_from_pandoc, src, fmt, file_id)
    finally:
        if tmpdoc:
            shutil.rmtree(tmpdoc.parent, ignore_errors=True)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 40:
        raise ValueError("This document has almost no text to make slides from.")
    truncated = len(text) > MAX_CHARS
    return {"text": text[:MAX_CHARS], "pictures": pics, "truncated": truncated, "title": Path(display_label(file_id)).stem}
