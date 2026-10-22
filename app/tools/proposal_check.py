"""A javaslat épsége (10-22, robot-tanulmány v5): követett javaslatban MINDEN eltérés jelölt — mindent elutasítva
pontosan a kiinduló változat szövege jön vissza. A v5-nél ezt kézzel igazoltuk (pandoc --track-changes=reject = v4);
itt a validate_document gépi ellenőrzése: a jelöletlenül (követés nélkül) beírt eltérés a szerző tudta nélkül megy át
az elfogadáson, és elutasítva sem tűnik el.

Alap (kiinduló változat): a megadott base_file_id, különben a változatláncon visszafelé az első változat, amelyben nincs
függő követett változás. Összevetés:
  • törzs: a mostani fájl „elutasítva” nézete (a törölt szöveg benne, a beszúrt nélkül; a beszúrt bekezdésjelnél a két
    darab egy bekezdés) ↔ az alap szövege, bekezdésenként (az üres bekezdés nem számít);
  • lábjegyzetek / végjegyzetek: az elutasítva is megmaradó hivatkozások jegyzetszövege ↔ az alap jegyzetei (sorrend
    nélkül — az újraszámozás nem eltérés).
Ha a lánc közben csökkent a függő változások száma (elfogadás / elutasítás lehetett), az eltérés lehet döntés is →
figyelmeztetés, nem hiba.
"""
from __future__ import annotations

import difflib
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from app.tools import review_loop as L

log = logging.getLogger("aice.proposal_check")

_NOTES = (("word/footnotes.xml", "footnote", "footnoteReference"), ("word/endnotes.xml", "endnote", "endnoteReference"))
_MAX_CHAIN = 60
_MAX_SHOWN = 8


def _norm(s: str) -> str:
    return " ".join((s or "").split())


def _inside(el, tags: tuple, stop=None) -> bool:
    for a in el.iterancestors():
        if a is stop:
            return False
        if a.tag in tags:
            return True
    return False


def _mark(p, tag) -> bool:
    """A bekezdésjel követett beszúrás (tag = INS) vagy törlés (DEL): pPr/rPr/w:ins | w:del."""
    ppr = p.find(L.PPR)
    rpr = ppr.find(L.RPR) if ppr is not None else None
    return rpr is not None and rpr.find(tag) is not None


def _changes(pkg: L._Pkg) -> int:
    n = 0
    for name in ["word/document.xml"] + [part for part, _, _ in _NOTES]:
        root = pkg.xml(name)
        if root is not None:
            n += sum(1 for _ in root.iter(L.INS, L.DEL, L.MOVE_FROM, L.MOVE_TO))
    return n


def _view(pkg: L._Pkg, before: bool) -> list[tuple[int, str]]:
    """[(bekezdés sorszáma 1-től, ahogy az edit.goto számol; szöveg)] — before: a változások előtti nézet, a beszúrt
    bekezdésjelnél a következő bekezdéssel egyben; különben a végleges nézet, a törölt bekezdésjelnél egyben."""
    out: list[tuple[int, str]] = []
    pending: Optional[tuple[int, str]] = None
    for i, p in enumerate(L._paras(L._body(pkg)), 1):
        t = L._orig_text(p) if before else L._ptext(p)
        if pending is not None:
            i, t = pending[0], pending[1] + t
            pending = None
        if _mark(p, L.INS if before else L.DEL):
            pending = (i, t)
            continue
        if _norm(t):
            out.append((i, _norm(t)))
    if pending is not None and _norm(pending[1]):
        out.append((pending[0], _norm(pending[1])))
    return out


def _note_texts(pkg: L._Pkg, before: bool) -> Counter:
    """A megmaradó hivatkozások jegyzetszövegei (multihalmaz). before: a beszúrt hivatkozás nem marad meg, a jegyzet
    szövege a változások előtti nézetben; különben a törölt hivatkozás nem marad meg, a szöveg a végleges nézetben."""
    out: Counter = Counter()
    doc = pkg.xml("word/document.xml")
    gone = (L.INS, L.MOVE_TO) if before else (L.DEL, L.MOVE_FROM)
    for part, tag, ref in _NOTES:
        notes = pkg.xml(part)
        if notes is None:
            continue
        by_id = {n.get(L.q("id")): n for n in notes.findall(L.q(tag)) if (n.get(L.q("type")) or "normal") == "normal"}
        for r in doc.iter(L.q(ref)):
            n = by_id.get(r.get(L.q("id")))
            if n is None or _inside(r, gone):
                continue
            text = " ".join(L._orig_text(p) if before else L._ptext(p) for p in n.iter(L.P))
            out[f"{tag}: {_norm(text)}"] += 1
    return out


def _clip(s: str, n: int = 90) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _word_diff(a: str, b: str) -> str:
    """„… régi → új …” — a két szöveg első eltérése néhány szónyi környezettel."""
    aw, bw = a.split(), b.split()
    sm = difflib.SequenceMatcher(None, aw, bw, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        pre = " ".join(aw[max(0, i1 - 4):i1])
        old, new = " ".join(aw[i1:i2]) or "∅", " ".join(bw[j1:j2]) or "∅"
        return f"…{pre} [{_clip(old, 60)} → {_clip(new, 60)}]…"
    return ""


def compare(current: Path, base: Path) -> dict[str, Any]:
    """→ {changed: [{paragraph, base, now, diff}], notes: [{base_only | now_only}], tracked: függő változások száma}."""
    cur, bas = L._Pkg(current), L._Pkg(base)
    a, b = [t for _, t in _view(bas, before=False)], _view(cur, before=True)
    sm = difflib.SequenceMatcher(None, a, [t for _, t in b], autojunk=False)
    changed: list[dict[str, Any]] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        old, new = " ¶ ".join(a[i1:i2]), " ¶ ".join(t for _, t in b[j1:j2])
        para = b[j1][0] if j1 < len(b) else (b[-1][0] if b else 1)
        changed.append({"paragraph": para, "base": _clip(old), "now": _clip(new), "diff": _word_diff(old, new),
                        "kind": {"replace": "changed", "delete": "removed", "insert": "added"}[op]})
    na, nb = _note_texts(bas, before=False), _note_texts(cur, before=True)
    notes = ([{"base_only": _clip(t)} for t in (na - nb)] + [{"now_only": _clip(t)} for t in (nb - na)])
    return {"changed": changed, "notes": notes, "tracked": _changes(cur)}


def _chain(file_id: str) -> list[str]:
    """A változatlánc visszafelé: [file_id, forrása, annak forrása, …]."""
    from app.storage import read_meta
    out, fid = [file_id], file_id
    while len(out) < _MAX_CHAIN:
        fid = (read_meta(fid) or {}).get("source_file_id")
        if not fid or fid in out:
            break
        out.append(fid)
    return out


def _docx(fid: str) -> Optional[Path]:
    from app.tools.crafts import existing
    try:
        p = existing(fid)
    except (FileNotFoundError, ValueError):
        return None
    return p if p.suffix.lower() in L._DOCX else None


def find_base(file_id: str) -> tuple[Optional[str], bool]:
    """→ (az alap file_id, döntés lehetett-e közben): az első követett változás nélküli ős a láncban."""
    counts: list[int] = []
    for fid in _chain(file_id):
        p = _docx(fid)
        if p is None:
            return None, False
        try:
            n = _changes(L._Pkg(p))
        except Exception:
            return None, False
        if n == 0 and counts:
            return fid, any(later < earlier for later, earlier in zip(counts, counts[1:]))
        counts.append(n)
    return None, False


def issues(file_id: str, current: Path, base_file_id: Optional[str] = None) -> tuple[list[dict[str, Any]], Optional[str]]:
    """→ (validate-tételek, a „verified” sor). Csak követett változást tartalmazó docx-en fut."""
    from app.storage import display_label
    from app.tools.validate import ERROR, WARNING, _issue
    try:
        if _changes(L._Pkg(current)) == 0:
            return [], None
    except Exception as e:
        log.info("épség-ellenőrzés kimaradt (%s): %s", file_id, e)
        return [], None
    decided = False
    if base_file_id:
        base = base_file_id
    else:
        base, decided = find_base(file_id)
    bp = _docx(base) if base else None
    if bp is None:
        return [], ("Proposal integrity not checked: no earlier version without tracked changes in this file's history "
                    "(pass base_file_id to name the one it was made from)." if not base_file_id else
                    f"Proposal integrity not checked: base_file_id {base_file_id} is not a Word file of yours.")
    res = compare(current, bp)
    name = display_label(base)
    if not res["changed"] and not res["notes"]:
        return [], (f"Proposal integrity: rejecting all {res['tracked']} tracked change(s) gives back “{name}” exactly "
                    "(body text and notes) — nothing was changed without tracking.")
    sev = WARNING if decided else ERROR
    why = (" Changes were accepted or rejected along the way, so some of these may be those decisions." if decided else "")
    out = []
    for c in res["changed"][:_MAX_SHOWN]:
        out.append(_issue(
            "untracked_change", sev,
            f"Paragraph {c['paragraph']} was {c['kind']} WITHOUT a tracked change against “{name}”: {c['diff'] or c['now']} "
            "— accepting or rejecting the proposal won't show it to the person." + why,
            fix=f'wordcraft: edit.goto {{"paragraph": {c["paragraph"]}}} — redo this change with tracking on '
                "(review.trackChanges), or put the base text back"))
    if len(res["changed"]) > _MAX_SHOWN:
        out.append(_issue("untracked_change", sev, f"…and {len(res['changed']) - _MAX_SHOWN} more paragraph(s) changed "
                          f"without tracking against “{name}” (files(action=\"compare\") lists them)." + why))
    if res["notes"]:
        sample = "; ".join(next(iter(n.values())) for n in res["notes"][:3])
        out.append(_issue(
            "untracked_change", sev,
            f"{len(res['notes'])} footnote/endnote text(s) differ from “{name}” without a tracked change: {sample}" + why,
            fix="redo the note edits with tracking on, or put the base text back"))
    for it in out:
        it["_structural"] = True
    return out, None
