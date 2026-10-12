"""Tartalmi ujjlenyomat (pptx, docx): ugyanaz-e két fájl a felhasználó szemében.

A szerkesztők (DeckCraft, WordCraft) mentéskor másképp sorosítanak (más részfájlok, más XML), ezért bájt-
összevetés nem jó. Itt ami LÁTSZIK: diák/bekezdések sorrendje, alakzatok helye és mérete, szöveg, betű-
formázás (félkövér, dőlt, aláhúzás, méret, szín), képek tartalma, táblázatok szövege, oldalbeállítás.
Kétes esetben „más” (None vagy eltérő érték) — így legfeljebb fölösleges új verzió lesz, adat nem vész el.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("aice.digest")
_GRID = 12700   # 1 pt EMU-ban: a szerkesztők kerekítése ne számítson változásnak


def _q(v: Optional[int]) -> Optional[int]:
    return None if v is None else round(v / _GRID)


def _color(font) -> Optional[str]:
    try:
        return str(font.color.rgb) if font.color is not None and font.color.type is not None else None
    except Exception:
        return None


def _runs(paragraphs) -> list:
    out = []
    for p in paragraphs:
        runs = [(r.text, r.font.bold, r.font.italic, r.font.underline,
                 r.font.size.pt if r.font.size else None, _color(r.font)) for r in p.runs if r.text]
        # a szomszédos, azonos formázású futamok összevonva (a szerkesztők másképp darabolnak)
        merged: list = []
        for t, *fmt in runs:
            if merged and merged[-1][1:] == fmt:
                merged[-1][0] += t
            else:
                merged.append([t, *fmt])
        out.append((getattr(p, "level", 0), [tuple(m) for m in merged]))
    return out


def _pptx(path: Path) -> list:
    from pptx import Presentation
    from pptx.util import Emu  # noqa: F401
    prs = Presentation(str(path))
    slides = []

    def shapes(coll) -> list:
        items = []
        for sh in coll:
            d: dict[str, Any] = {"t": str(sh.shape_type), "g": (_q(sh.left), _q(sh.top), _q(sh.width), _q(sh.height))}
            if sh.has_text_frame:
                d["x"] = _runs(sh.text_frame.paragraphs)
            if getattr(sh, "image", None) is not None:
                try:
                    d["img"] = hashlib.sha1(sh.image.blob).hexdigest()
                except Exception:
                    pass
            if getattr(sh, "has_table", False) and sh.has_table:
                d["tbl"] = [[c.text for c in r.cells] for r in sh.table.rows]
            if getattr(sh, "has_chart", False) and sh.has_chart:
                ch = sh.chart
                d["ch"] = (str(ch.chart_type), [(s.name, list(s.values)) for s in ch.plots[0].series] if ch.plots else [])
            if str(sh.shape_type).startswith("GROUP"):
                d["grp"] = shapes(sh.shapes)
            items.append(d)
        return sorted(items, key=repr)       # a z-sorrend helyett a tartalom számít

    for s in prs.slides:
        notes = s.notes_slide.notes_text_frame.text if s.has_notes_slide else ""
        slides.append({"layout": s.slide_layout.name, "shapes": shapes(s.shapes), "notes": notes.strip()})
    return [(_q(prs.slide_width), _q(prs.slide_height)), slides]


def _docx(path: Path) -> list:
    from docx import Document
    doc = Document(str(path))
    body = []
    for p in doc.paragraphs:
        body.append(("p", p.style.name if p.style is not None else None, p.paragraph_format.alignment,
                     _runs([p])[0][1]))
    tables = [[[c.text for c in r.cells] for r in t.rows] for t in doc.tables]
    imgs = sorted(hashlib.sha1(rel.target_part.blob).hexdigest() for rel in doc.part.rels.values()
                  if "image" in rel.reltype and not rel.is_external)
    secs = [(_q(s.page_width), _q(s.page_height), _q(s.left_margin), _q(s.right_margin), _q(s.top_margin),
             _q(s.bottom_margin)) for s in doc.sections]
    return [body, tables, imgs, secs]


def digest(path: Path) -> Optional[str]:
    """sha1 a látható tartalomról; None, ha a formátum nem támogatott vagy nem olvasható."""
    ext = path.suffix.lower().lstrip(".")
    try:
        data = _pptx(path) if ext == "pptx" else _docx(path) if ext == "docx" else None
    except Exception as e:
        log.info("ujjlenyomat nem készült (%s): %s", path.name, e)
        return None
    return None if data is None else hashlib.sha1(repr(data).encode("utf-8")).hexdigest()


def same_content(a: Path, b: Path) -> bool:
    da, db = digest(a), digest(b)
    return da is not None and da == db
