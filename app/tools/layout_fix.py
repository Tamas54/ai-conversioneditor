"""Tördelési hibák javítása visszaellenőrzéssel (10-22, egy MCP-ügynök jelentéséből: „a validátor már talál problémákat;
innen továbblépnék egy »javítsd a tördelési hibákat« műveletre … minden javítás után újrarenderelés és ellenőrzés,
korlátozott számú körrel”).

Egy kör: a dokumentum mindkét tördelése (a beépített szerkesztő = WordCraft, a letöltött fájl = LibreOffice) →
a validátor hibái (validate.py) → a javíthatók determinisztikus javítása a DOCX-ben → újra. Legfeljebb MAX_ROUNDS kör;
ami a végén is fennáll, azt a válasz megnevezi (nem hallgatja el).

Javítások — ugyanazok, amiket a validátor `fix`-e mond, csak gép teszi:
  • cím a lap alján (orphan_heading): „együtt a következővel” + „sorok együtt”; ha a következő körben is ott van, vagy
    csak a szerkesztő motorja hagyja el (a címen már rajta van) → oldaltörés előtte, így a két nézet egyezik;
  • képaláírás külön lapon (caption_split): az előbb álló (aláírás vagy kép) „együtt a következővel”; ismétlésnél
    oldaltörés;
  • táblázatsor két lapon (table_row_split): a sor ne törjön („cantSplit”) — ha már így van, a sor magasabb a lapnál:
    marad, és megmondjuk;
  • táblázat fejléce nem ismétlődik (table_header_not_repeated): az első sor fejlécsor.
Ami nem gépi döntés (üres oldal, a margóba lógó szöveg, számozás), az a maradék listában marad a validátor tanácsával.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("aice.layout_fix")

MAX_ROUNDS = 3
FIXABLE = ("orphan_heading", "caption_split", "table_row_split", "table_header_not_repeated")


def _key(it: dict[str, Any]) -> tuple:
    return (it["kind"], it.get("_pos"), it.get("row"), "pic" if str(it.get("message", "")).startswith("The picture is") else "")


def _plan(issues: list[dict[str, Any]], tried: dict[tuple, int]) -> list[tuple[dict[str, Any], str]]:
    """A kör javításai: [(hiba, mit)] — mindkét nézet ugyanazon hibája egyszer."""
    out, seen = [], set()
    for it in issues:
        if it["kind"] not in FIXABLE or it.get("_pos") is None:
            continue
        k = _key(it)
        if k in seen:
            continue
        seen.add(k)
        n = tried.get(k, 0)
        msg = it.get("message", "")
        if it["kind"] == "orphan_heading":
            how = "break" if (n >= 1 or "already set to keep" in msg) else "keep"
        elif it["kind"] == "caption_split":
            how = "break" if n >= 1 else "keep"
        elif it["kind"] == "table_row_split":
            if "although it is set not to break" in msg or n >= 1:
                continue                                # a sor magasabb a lapnál — gép nem dönthet a tartalmáról
            how = "row"
        else:
            if n >= 1:
                continue
            how = "header"
        out.append((it, how))
    return out


def _apply(src: Path, dst: Path, todo: list[tuple[dict[str, Any], str]]) -> list[dict[str, Any]]:
    """A javítások egy DOCX-másolaton → dst. → [{kind, object, page, how}] (ami tényleg megtörtént)."""
    from docx import Document
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph
    from app.tools.validate import _W, _body
    d = Document(str(src))
    blocks = list(_body(d.element.body))
    done = []

    def content_before(pos: int):
        for el in reversed(blocks[:pos]):
            if el.tag == _W + "tbl" or "".join(t.text or "" for t in el.iter(_W + "t")).strip() or \
                    next(el.iter("{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}inline"), None) is not None:
                return el
        return None

    def row_flag(tr, tag: str) -> None:
        trpr = tr.get_or_add_trPr()
        if trpr.find(qn(tag)) is None:
            from docx.oxml import OxmlElement
            trpr.append(OxmlElement(tag))

    for it, how in todo:
        el = blocks[it["_pos"]] if 0 <= it["_pos"] < len(blocks) else None
        if el is None:
            continue
        if it["kind"] == "caption_split" and _key(it)[3] == "pic":
            el = content_before(it["_pos"])               # a kép UTÁNI aláírás: a képet tartjuk együtt vele
            if el is None or el.tag != _W + "p":
                continue
        what = it.get("object") or it["kind"]
        if how in ("keep", "break") and el.tag == _W + "p":
            pf = Paragraph(el, None).paragraph_format
            if how == "keep":
                pf.keep_with_next, pf.keep_together = True, True
                text = "kept with the next paragraph (keep with next, keep lines together)"
            else:
                pf.page_break_before = True
                text = "a page break before it, so it starts the next page with what follows (both views agree)"
        elif how == "row" and el.tag == _W + "tbl":
            rows = el.findall(_W + "tr")
            r = (it.get("row") or 1) - 1
            if not 0 <= r < len(rows):
                continue
            row_flag(rows[r], "w:cantSplit")
            text = f"row {r + 1} set not to break across pages"
        elif how == "header" and el.tag == _W + "tbl":
            first = el.find(_W + "tr")
            if first is None:
                continue
            row_flag(first, "w:tblHeader")
            text = "the first row repeats as a header on every page"
        else:
            continue
        done.append({"kind": it["kind"], "object": what, "page": it.get("page"), "how": text})
    d.save(str(dst))
    return done


async def _issues(path: Path) -> tuple[list[dict[str, Any]], list[str], int]:
    from app.tools import validate as v
    layouts, notes = await v._layouts(path, "document")
    if not layouts:
        raise RuntimeError("The document could not be laid out. " + " ".join(notes))
    try:
        issues, _ = await asyncio.to_thread(v._analyze_doc, path, layouts)
        pages = max(L.pages for L in layouts)
    finally:
        for L in layouts:
            L.close()
    return issues, notes, pages


async def fix(file_id: str, progress: Optional[Callable[..., None]] = None, rounds: int = MAX_ROUNDS) -> dict[str, Any]:
    """→ {file_id (az új változat, vagy a régi, ha nem kellett javítani), fixed [...], remaining [validátor-hibák],
    rounds, summary}."""
    from app.storage import display_label, new_file_id, output_path, public_url, temp_path, write_meta
    from app.storage.naming import result_stem
    from app.tools import validate as v
    from app.tools.crafts import existing
    step = progress or (lambda *a, **k: None)
    src = existing(file_id)
    ext = src.suffix.lstrip(".").lower()
    if ext not in v.WORDISH:
        raise ValueError("Layout fixing works on Word documents (.docx, .odt, .rtf, .md) — for a deck, use the office "
                         "fixes in validate_document's issues.")
    work = temp_path(f"layoutfix_{os.urandom(5).hex()}")
    work.mkdir(parents=True)
    try:
        cur = work / "r0.docx"
        shutil.copyfile(await v._as_docx(src), cur)
        tried: dict[tuple, int] = {}
        fixed: list[dict[str, Any]] = []
        n = 0
        issues, notes, pages = await _issues(cur)
        for n in range(1, max(1, rounds) + 1):
            todo = _plan(issues, tried)
            if not todo:
                n -= 1
                break
            step(n, rounds + 1, f"round {n}: fixing {len(todo)} layout problem(s)")
            nxt = work / f"r{n}.docx"
            done = await asyncio.to_thread(_apply, cur, nxt, todo)
            for it, _ in todo:
                tried[_key(it)] = tried.get(_key(it), 0) + 1
            fixed += done
            cur = nxt
            step(n, rounds + 1, f"round {n}: laying it out again")
            issues, notes, pages = await _issues(cur)
        remaining = v.merge([i for i in issues], sorted({i["view"] for i in issues if i.get("view")}) or ["editor", "word"])
        out = {"rounds": n, "fixed": fixed, "remaining": [v.public(i) for i in remaining], "summary": v.counts(remaining),
               "pages": pages, "notes": notes}
        if not fixed:
            return {**out, "file_id": file_id, "url": public_url(file_id), "label": display_label(file_id), "changed": False}
        fid = new_file_id("docx")
        shutil.copyfile(cur, output_path(fid))
        label = f"{result_stem(file_id, 'layout fixed')}.docx"
        write_meta(fid, label=label, source_file_id=file_id, operation="layout fix")
        return {**out, "file_id": fid, "url": public_url(fid), "label": label, "changed": True}
    finally:
        shutil.rmtree(work, ignore_errors=True)
