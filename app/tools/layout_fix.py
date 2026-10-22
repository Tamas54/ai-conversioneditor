"""Tördelési hibák javítása — JAVASLAT, JÓVÁHAGYÁS, VÉGREHAJTÁS, visszaellenőrzéssel (10-22).

Kommandant: „a szerkesztői intelligenciát mindenképpen »korlátok« között tartanám … az ember vállalja a felelősséget
a rendszer termékéért, tehát TELJESEN AUTOMATIKUS rendszer nem képzelhető el” és „A tördelésjavítónak is … néha van
»főnöki kérés«, amikor az ember a logikus tördelés HELYETT utasításra cselekszik. A rendszernek ezt intelligensen el kell
fogadnia.” Ezért:
  1. propose: a dokumentum mindkét tördelése (beépített szerkesztő = WordCraft, letöltött fájl = LibreOffice) → a
     validátor hibáiból a javítható javaslatok (F1, F2…) — semmi nem változik;
  2. apply(ids | "all"): CSAK a jóváhagyott javaslatok → új változat → újratördelés; ami utána is fennáll, arra az
     erősebb lépés (oldaltörés) ÚJRA javaslat, nem lép meg magától;
  3. keep(issue ids, reason): „így marad” — a döntés a fájl metájában, a későbbi változatok öröklik (a forrásláncon
     át); a validátor, a mentés utáni check és a javító is tudomásul veszi. A fájlban kifejezetten kikapcsolt
     „együtt a következővel” ugyanígy szándék (validate.py: keep_off).

Javaslatok (ugyanazok, amiket a validátor `fix`-e mond):
  • cím a lap alján (orphan_heading): „együtt a következővel” + „sorok együtt”; ha már rajta van, és egy nézet mégis
    elhagyja → oldaltörés előtte (így a két nézet egyezik);
  • képaláírás külön lapon (caption_split): az előbb álló (aláírás vagy kép) „együtt a következővel”; ha már rajta
    van → oldaltörés;
  • táblázatsor két lapon (table_row_split): a sor ne törjön — ha már így van, a sor magasabb a lapnál: nem gépi döntés;
  • táblázat fejléce nem ismétlődik (table_header_not_repeated): az első sor fejlécsor.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("aice.layout_fix")

FIXABLE = ("orphan_heading", "caption_split", "table_row_split", "table_header_not_repeated")
CHAIN_MAX = 40
_HOW = {"keep": "keep it with the next paragraph (keep with next, keep lines together)",
        "break": "start it on a new page (page break before) — the views then agree",
        "row": "set the row not to break across pages",
        "header": "make the first row a header row that repeats on every page"}


# ---------- „így marad” döntések ----------

def keeps_for(file_id: str) -> list[dict[str, Any]]:
    """Az ember tördelési döntései erre a változatra és az elődeire (a forrásláncon felfelé)."""
    from app.storage import read_meta
    out, seen, cur = [], set(), file_id
    for _ in range(CHAIN_MAX):
        if not cur or cur in seen:
            break
        seen.add(cur)
        m = read_meta(cur) or {}
        out += [k for k in m.get("layout_keep") or [] if isinstance(k, dict)]
        cur = m.get("source_file_id")
    return out


def _picture_first(it: dict[str, Any]) -> bool:
    return str(it.get("message", "")).startswith("The picture is")


def _plan(issues: list[dict[str, Any]], blocks: dict[int, Any]) -> list[dict[str, Any]]:
    """A javaslatok: [{id, issue, kind, object, page, change, _it, _how}] — egy hiba két nézetben egyszer."""
    out, seen = [], set()
    for it in issues:
        if it["kind"] not in FIXABLE or it.get("_pos") is None or it.get("on_purpose"):
            continue
        b = blocks.get(it["_pos"])
        target = b
        if it["kind"] == "caption_split" and _picture_first(it):
            target = next((x for p, x in sorted(blocks.items(), reverse=True) if p < it["_pos"] and not x.empty), None)
        key = (it["kind"], it["_pos"], it.get("row"), _picture_first(it))
        if key in seen or target is None:
            continue
        seen.add(key)
        if it["kind"] in ("orphan_heading", "caption_split"):
            if getattr(target, "break_before", False):
                continue
            how = "break" if getattr(target, "keep_next", False) else "keep"
        elif it["kind"] == "table_row_split":
            r = (it.get("row") or 1) - 1
            if b is None or (r < len(b.cant_split) and b.cant_split[r]):
                continue                                # a lapnál magasabb sor — a tartalmáról az ember dönt
            how = "row"
        else:
            if b is None or b.header_row:
                continue
            how = "header"
        out.append({"id": f"F{len(out) + 1}", "issue": it.get("id"), "kind": it["kind"], "object": it.get("object"),
                    "page": it.get("page"), "change": _HOW[how], "_it": it, "_how": how,
                    "_target": getattr(target, "pos", None)})
    return out


def public(p: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in p.items() if not k.startswith("_") and v is not None}


def _apply(src: Path, dst: Path, todo: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A jóváhagyott javaslatok egy DOCX-másolaton → dst. → [{kind, object, page, how}] (ami tényleg megtörtént)."""
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph
    from app.tools.validate import _W, _body
    d = Document(str(src))
    blocks = list(_body(d.element.body))
    done = []

    def row_flag(tr, tag: str) -> None:
        trpr = tr.get_or_add_trPr()
        if trpr.find(qn(tag)) is None:
            trpr.append(OxmlElement(tag))

    for p in todo:
        pos = p["_target"]
        el = blocks[pos] if pos is not None and 0 <= pos < len(blocks) else None
        if el is None:
            continue
        how = p["_how"]
        if how in ("keep", "break") and el.tag == _W + "p":
            pf = Paragraph(el, None).paragraph_format
            if how == "keep":
                pf.keep_with_next, pf.keep_together = True, True
            else:
                pf.page_break_before = True
        elif how == "row" and el.tag == _W + "tbl":
            rows = el.findall(_W + "tr")
            r = (p["_it"].get("row") or 1) - 1
            if not 0 <= r < len(rows):
                continue
            row_flag(rows[r], "w:cantSplit")
        elif how == "header" and el.tag == _W + "tbl":
            first = el.find(_W + "tr")
            if first is None:
                continue
            row_flag(first, "w:tblHeader")
        else:
            continue
        done.append({"id": p["id"], "kind": p["kind"], "object": p["object"], "page": p["page"], "how": p["change"]})
    d.save(str(dst))
    return done


async def analyze(path: Path, file_id: Optional[str]) -> dict[str, Any]:
    """Mindkét tördelés + hibák; az „így marad” döntések levéve. → {issues, kept, blocks (pos → Block), pages, notes}"""
    from app.tools import validate as v
    layouts, notes = await v._layouts(path, "document")
    if not layouts:
        raise RuntimeError("The document could not be laid out. " + " ".join(notes))
    try:
        issues, dc = await asyncio.to_thread(v._analyze_doc, path, layouts)
        pages = max(L.pages for L in layouts)
    finally:
        for L in layouts:
            L.close()
    issues, kept = v.split_kept(issues, dc.blocks, keeps_for(file_id) if file_id else [])
    return {"issues": issues, "kept": kept, "blocks": {b.pos: b for b in dc.blocks}, "pages": pages, "notes": notes}


async def _docx_of(file_id: str) -> Path:
    from app.tools import validate as v
    from app.tools.crafts import existing
    src = existing(file_id)
    if src.suffix.lstrip(".").lower() not in v.WORDISH:
        raise ValueError("Layout fixing works on Word documents (.docx, .odt, .rtf, .md).")
    return await v._as_docx(src)


async def propose(file_id: str) -> dict[str, Any]:
    """Javaslatok — a fájl nem változik."""
    a = await analyze(await _docx_of(file_id), file_id)
    return {"proposals": [public(p) for p in _plan(a["issues"], a["blocks"])], "kept": len(a["kept"])}


async def apply(file_id: str, ids: Any, progress: Optional[Callable[..., None]] = None) -> dict[str, Any]:
    """A jóváhagyott javaslatok (ids: ["F1", "F3"] | "all") → új változat, újratördelve. Ami utána is fennáll, arra a
    következő lépés JAVASLAT (proposals), nem hajtjuk végre."""
    from app.storage import new_file_id, output_path, public_url, temp_path, write_meta
    from app.storage.naming import result_stem
    step = progress or (lambda *a, **k: None)
    src = await _docx_of(file_id)
    step(0, 3, "laying it out (editor and Word)")
    a = await analyze(src, file_id)
    plan = _plan(a["issues"], a["blocks"])
    want = {str(x).strip().upper() for x in (ids if isinstance(ids, list) else [ids])}
    todo = plan if "ALL" in want else [p for p in plan if p["id"] in want]
    unknown = sorted(want - {p["id"] for p in plan} - {"ALL"})
    if unknown and not todo:
        raise ValueError(f"No such proposal: {', '.join(unknown)} — the proposals now are "
                         f"{', '.join(p['id'] for p in plan) or 'none'} (call validate_document(fix=true) to see them).")
    if not todo:
        return {"changed": False, "file_id": file_id, "fixed": [], "proposals": [], "note": "Nothing to fix."}
    work = temp_path(f"layoutfix_{os.urandom(5).hex()}")
    work.mkdir(parents=True)
    try:
        nxt = work / "fixed.docx"
        step(1, 3, f"applying {len(todo)} approved fix(es)")
        done = await asyncio.to_thread(_apply, src, nxt, todo)
        fid = new_file_id("docx")
        shutil.copyfile(nxt, output_path(fid))
        label = f"{result_stem(file_id, 'layout fixed')}.docx"
        write_meta(fid, label=label, source_file_id=file_id, operation="layout fix", extra={"layout_fixed": done})
        step(2, 3, "laying it out again")
        after = await analyze(output_path(fid), fid)
        follow = _plan(after["issues"], after["blocks"])
        return {"changed": True, "file_id": fid, "url": public_url(fid), "label": label, "fixed": done,
                "not_applied": unknown, "proposals": [public(p) for p in follow]}
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def keep(file_id: str, ids: Any, reason: Optional[str] = None) -> list[dict[str, Any]]:
    """„Így marad”: a megnevezett hibák (E2, W1…) döntésként a fájlra — a későbbi változatok öröklik. → a döntések."""
    from datetime import datetime, timezone
    from app.storage import read_meta, write_meta
    from app.tools import validate as v
    a = await analyze(await _docx_of(file_id), file_id)
    want = {str(x).strip().upper() for x in (ids if isinstance(ids, list) else [ids])}
    found = [it for it in a["issues"] if str(it.get("id") or "").upper() in want]
    missing = sorted(want - {str(it.get("id")).upper() for it in found})
    if missing:
        raise ValueError(f"No such issue now: {', '.join(missing)} — the current ones are "
                         f"{', '.join(i['id'] for i in a['issues'] if i.get('id')) or 'none'}.")
    why = " ".join(str(reason or "").split())[:200] or "kept as it is on request"
    new = []
    for it in found:
        sig = v.issue_sig(it, a["blocks"])
        if sig:
            new.append({"sig": sig, "kind": it["kind"], "object": it.get("object"), "reason": why,
                        "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    old = [k for k in (read_meta(file_id) or {}).get("layout_keep") or [] if k.get("sig") not in {n["sig"] for n in new}]
    write_meta(file_id, extra={"layout_keep": old + new})
    return new
