"""„New” — üres fájl minden eszközhöz, rögtön a látható szerkesztőbe (AI és feltöltés nélkül, vendégnek is).

EGY nyilvántartás (KINDS): eszköz → méretek, és EGY belépési pont (make). Az eszközök:
    social  — üres VectorCraft-vászon (.vectorcraft) a közösségi méretekben (px = pt, mint a social-sablonnál)
    print   — üres VectorCraft-oldal (A4/A3/A5/Letter, álló/fekvő)
    photo   — üres PhotoCraft-vászon (PSD), fehér vagy átlátszó háttérrel
    book    — üres könyv Word-fájlként: könyvstílusok, tükörmargó, címoldal + „Chapter 1”
    pdf     — üres PDF (A4/Letter), egy oldal
A dokumentumnak és a paklinak saját „New” módja van (/api/make/document, /api/make/deck mode=blank).
"""
from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

from app.storage import output_path, public_url, write_meta
from app.storage.files import new_file_id

PT_PER_MM = 72 / 25.4
KINDS: dict[str, dict[str, Any]] = {
    "social": {"label": "Untitled design", "sizes": {"square": (1080, 1080), "portrait": (1080, 1350),
                                                     "story": (1080, 1920), "landscape": (1200, 630)}},
    "print": {"label": "Untitled design", "sizes": {"A4": (595.3, 841.9), "A3": (841.9, 1190.6),
                                                    "A5": (419.5, 595.3), "Letter": (612, 792)}},
    "photo": {"label": "Untitled photo", "sizes": {"1920x1080": (1920, 1080), "1080x1080": (1080, 1080),
                                                   "1080x1350": (1080, 1350), "3000x2000": (3000, 2000)}},
    "book": {"label": "Untitled book", "sizes": {"A5": (148, 210), "A4": (210, 297), "6x9": (152.4, 228.6)}},   # mm
    "pdf": {"label": "Untitled PDF", "sizes": {"A4": (595.3, 841.9), "Letter": (612, 792)}},
}
MAX_PX = 12000


def _size(kind: str, size: Optional[str], orientation: Optional[str], width: Any, height: Any) -> tuple[float, float]:
    spec = KINDS[kind]
    if kind == "photo" and (size == "custom" or (width and height and not size)):
        try:
            w, h = int(float(width)), int(float(height))
        except (TypeError, ValueError):
            raise ValueError("Give the canvas width and height in pixels.")
        if not (1 <= w <= MAX_PX and 1 <= h <= MAX_PX):
            raise ValueError(f"Width and height must be between 1 and {MAX_PX} pixels.")
        return w, h
    w, h = spec["sizes"].get(size or "", next(iter(spec["sizes"].values())))
    if orientation == "landscape" and h > w or orientation == "portrait" and w > h:
        w, h = h, w
    return w, h


async def _vector(kind: str, w: float, h: float) -> str:
    from app.tools import crafts
    out = await crafts.vector_run(commands=[{"command": "file.new", "params": {
        "width": w, "height": h, "units": "Points", "name": KINDS[kind]["label"]}}], export=["vectorcraft"])
    return out["file_id"]


async def _photo(w: int, h: int, background: Optional[str]) -> str:
    from app.tools import crafts
    bg = background if background in ("white", "transparent", "black") else "white"
    out = await crafts.photo_run(new={"width": int(w), "height": int(h), "background": bg, "name": KINDS["photo"]["label"]},
                                 save_format="psd")
    return out["file_id"]


def _pdf(w: float, h: float) -> str:
    import fitz
    fid = new_file_id("pdf")
    doc = fitz.open()
    doc.new_page(width=w, height=h)
    doc.set_metadata({"title": KINDS["pdf"]["label"]})
    doc.save(str(output_path(fid)))
    doc.close()
    return fid


def _book(w_mm: float, h_mm: float) -> str:
    """Pandoc + a könyv-referencia (Caladea, sorkizárt, első soros behúzás, fejezetcím új oldalon) — ugyanaz a
    szedés, mint a kéziratból készült könyvé; címoldal-helyőrzővel, tükörmargóval."""
    import pypandoc
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt, RGBColor
    from app.tools import digitize
    fid = new_file_id("docx")
    with tempfile.TemporaryDirectory() as td:
        md = Path(td) / "book.md"
        md.write_text("# Chapter 1\n\nStart writing here. Each “Heading 1” starts a new chapter on a new page.\n", "utf-8")
        pypandoc.convert_file(str(md), "docx", outputfile=str(output_path(fid)),
                              extra_args=["--wrap=none", f"--reference-doc={digitize._book_reference()}"])
    doc = Document(str(output_path(fid)))
    big = w_mm > 180
    for sec in doc.sections:
        sec.page_width, sec.page_height = Mm(w_mm), Mm(h_mm)
        sec.left_margin, sec.right_margin = Mm(25 if big else 20), Mm(20 if big else 15)   # belső (gerinc) / külső
        sec.top_margin, sec.bottom_margin = Mm(22 if big else 18), Mm(22 if big else 20)
    settings = doc.settings.element
    if settings.find(qn("w:mirrorMargins")) is None:
        settings.insert(0, OxmlElement("w:mirrorMargins"))
    styles = {s.name for s in doc.styles}
    body = doc.element.body
    front = []
    for text, style, size in (("Book title", "Title", 26), ("Author", "Subtitle", 14)):
        p = doc.add_paragraph(text, style=style if style in styles else None)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(120 if style == "Title" else 12)
        for r in p.runs:
            r.font.size = Pt(size)
            r.font.color.rgb = RGBColor(0x11, 0x11, 0x11)     # mint a kéziratos könyv címoldala (a pandoc Title kék)
        front.append(p)
    for k, p in enumerate(front):                       # a címoldal a dokumentum elejére
        body.remove(p._p)
        body.insert(k, p._p)
    doc.save(str(output_path(fid)))
    return fid


async def make(body: dict[str, Any]) -> dict[str, Any]:
    kind = body.get("kind") or ""
    if kind not in KINDS:
        raise ValueError(f"kind: {', '.join(KINDS)} (got {kind!r})")
    w, h = _size(kind, body.get("size"), body.get("orientation"), body.get("width"), body.get("height"))
    if kind in ("social", "print"):
        fid = await _vector(kind, w, h)
    elif kind == "photo":
        fid = await _photo(int(w), int(h), body.get("background"))
    elif kind == "pdf":
        fid = await asyncio.to_thread(_pdf, w, h)
    else:
        fid = await asyncio.to_thread(_book, w, h)
    ext = fid.rsplit(".", 1)[1]
    label = f"{KINDS[kind]['label']}.{ext}"
    write_meta(fid, operation=f"new {kind}", label=label,
               extra={"blank": {"kind": kind, "size": body.get("size"), "orientation": body.get("orientation")}})
    return {"file_id": fid, "url": public_url(fid), "label": label, "size_kb": output_path(fid).stat().st_size // 1024}


def placeholder_title(text: str) -> bool:
    """A „New” fájlok helyőrző szövegei — mentéskor ezekből nem lesz fájlnév."""
    t = re.sub(r"\s+", " ", text.strip().lower())
    return t in {"book title", "author", "chapter 1"} or t.startswith("start writing here")
