"""Egy dokumentum összevetése az új szövegével — követett változásként, a lábjegyzetek, képek, mezők és megjegyzések
megtartásával (a WordCraft `review.compare {"text"}` helyett).

10-22, szerkesztői próba: a motor saját összevetése a módosult bekezdés lábjegyzet-hivatkozását ÉS a lábjegyzetet is
eldobta, és a változások szerzője „Compare” lett. Itt a review_loop szövegmodelljén dolgozunk: csak a szövegatomok
cserélődnek (szószinten, mint a lektori javításnál), minden más elem a helyén marad.

A szöveg alakja a `document.text`-é: soronként egy bekezdés (a bekezdésen belüli sortörés is új sor), táblázatsor
tabulátorral elválasztott cellákkal. Soronkénti illesztés után:
  • módosult sor → szószintű követett csere a bekezdésben (a cellában);
  • új sor → követett új bekezdés (a szomszéd bekezdés formájával; cím után a következő szövegbekezdés formájával);
  • eltűnt bekezdés → követett törlés (szöveg, jegyzetjel, kép és a bekezdésjel is);
  • táblázatsort hozzáadni / törölni így nem lehet — a válasz megmondja (table.* parancsokkal megy).
"""
from __future__ import annotations

import difflib
import re
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from lxml import etree

from app.tools import review_loop as L
from app.tools.review_loop import DEL, INS, MOVE_FROM, P, PPR, R, RPR, q

_TBL, _TR, _TC = q("tbl"), q("tr"), q("tc")


def _lines(body) -> list[tuple[Any, int, str]]:
    """(tulajdonos elem, sor a tulajdonoson belül, szöveg) a dokumentum-sorrendben — bekezdés (w:p) vagy táblázatsor (w:tr)."""
    out: list[tuple[Any, int, str]] = []

    def walk(el):
        for ch in el.iterchildren():
            if ch.tag == P:
                for k, line in enumerate(L._ptext(ch).split("\n")):
                    out.append((ch, k, line))
            elif ch.tag == _TBL:
                for tr in ch.iter(_TR):
                    if next(tr.iterancestors(_TBL)) is ch:
                        out.append((tr, 0, "\t".join(_cell_text(tc) for tc in tr.findall(_TC))))
            elif ch.tag in (q("sdt"), q("customXml"), q("sdtContent")):
                walk(ch.find(q("sdtContent")) if ch.tag == q("sdt") else ch)
    walk(body)
    return out


def _cell_text(tc) -> str:
    return " ".join(L._ptext(p).replace("\n", " ") for p in tc.findall(P))


_WORD = re.compile(r"\w+")
_PAIR_MIN = 0.4                 # ennél kevésbé hasonló sor nem ugyanannak a bekezdésnek az új alakja


def _similar(a: str, b: str) -> float:
    """Szavak multihalmazának Dice-hasonlósága (gyors: egy teljes átírásnál is több száz × több száz sor)."""
    ca, cb = Counter(_WORD.findall(a.lower())), Counter(_WORD.findall(b.lower()))
    n = sum(ca.values()) + sum(cb.values())
    return 2 * sum((ca & cb).values()) / n if n else float(a == b)


def _pair(olds: list[int], news: list[int], old_t: list[str], new_t: list[str]) -> list[tuple[Optional[int], Optional[int]]]:
    """Egy cserélt szakasz sorainak párosítása sorrendtartóan, a hasonlóság összegét maximálva (az elé írt új sor és a
    törölt sor nem tolja el a párokat — a lábjegyzet a saját bekezdésénél marad). Ha semmi sem hasonlít, és ugyanannyi
    sor van: helyben átírás, egy az egyben."""
    n, m = len(olds), len(news)
    if n * m > 400_000:                                  # óriás csere: egy az egyben, a maradék új / törölt
        k = min(n, m)
        return list(zip(olds[:k], news[:k])) + [(i, None) for i in olds[k:]] + [(None, j) for j in news[k:]]
    gain = [[_similar(old_t[i], new_t[j]) - _PAIR_MIN for j in news] for i in olds]
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for a in range(1, n + 1):
        for b in range(1, m + 1):
            g = gain[a - 1][b - 1]
            best[a][b] = max(best[a - 1][b], best[a][b - 1], best[a - 1][b - 1] + g if g > 0 else float("-inf"))
    out: list[tuple[Optional[int], Optional[int]]] = []
    a, b = n, m
    while a or b:
        g = gain[a - 1][b - 1] if a and b else 0
        if a and b and g > 0 and best[a][b] == best[a - 1][b - 1] + g:
            out.append((olds[a - 1], news[b - 1]))
            a, b = a - 1, b - 1
        elif a and best[a][b] == best[a - 1][b]:
            out.append((olds[a - 1], None))
            a -= 1
        else:
            out.append((None, news[b - 1]))
            b -= 1
    out.reverse()
    if n == m and not any(i is not None and j is not None for i, j in out):
        return list(zip(olds, news))
    return out


def _is_heading(p) -> bool:
    ppr = p.find(PPR)
    st = ppr.find(q("pStyle")) if ppr is not None else None
    name = (st.get(q("val")) if st is not None else "") or ""
    return name.lower().startswith(("heading", "title", "cim", "címsor")) or \
        (ppr is not None and ppr.find(q("outlineLvl")) is not None)


def _mark(ppr, tag: str, ctx: L._Ctx) -> None:
    """A bekezdésjel követett beszúrása / törlése (w:pPr/w:rPr/w:ins|w:del)."""
    rpr = ppr.find(RPR)
    if rpr is None:
        rpr = etree.Element(RPR)
        tail = next((x for x in ppr if x.tag in (q("sectPr"), q("pPrChange"))), None)
        tail.addprevious(rpr) if tail is not None else ppr.append(rpr)
    m = etree.Element(tag)
    rpr.insert(0, m)                                 # a séma szerint a bekezdésjel ins/del-je az rPr ELEJÉN áll
    m.set(q("id"), ctx.next_id())
    m.set(q("author"), ctx.author)
    m.set(q("date"), ctx.date)
    ctx.added += 1


def _tracked_mark(p) -> bool:
    rpr = p.find(f"{PPR}/{RPR}")
    return rpr is not None and any(x.tag in (INS, DEL, q("moveFrom"), q("moveTo")) for x in rpr)


def _new_para(text: str, model, ctx: L._Ctx, mark: bool = True):
    """Új bekezdés követett beszúrással. mark=False: a bekezdésjele az eredeti (ahogy Word írja, ha egy bekezdés VÉGÉN
    Entert ütnek: az előző bekezdés jele lesz az új, a beírt szöveg az eredeti jel elé kerül) — így az elutasítás a
    dokumentum végén sem hagy üres bekezdést."""
    p = etree.Element(P)
    ppr = deepcopy(model.find(PPR)) if model is not None and model.find(PPR) is not None else etree.Element(PPR)
    for x in ppr.findall(q("sectPr")) + ppr.findall(q("pPrChange")):
        ppr.remove(x)
    rpr = ppr.find(RPR)
    for x in list(rpr) if rpr is not None else ():
        if x.tag in (INS, DEL, q("moveFrom"), q("moveTo")):
            rpr.remove(x)
    p.append(ppr)
    if mark:
        _mark(ppr, INS, ctx)
    src = next((a[1] for a in L._atoms(model) if a[3]), None) if model is not None else None
    if text:
        p.append(L._new_ins(text, L._rpr_copy(src), ctx))
        ctx.added += 1
    return p


def _delete_para(p, ctx: L._Ctx) -> None:
    """Az egész bekezdés követett törlése: a szöveg (a saját beszúrás egyszerűen eltűnik), a nem-szöveg futások
    (jegyzetjel, kép, mező) és a bekezdésjel. A jegyzet így a helyén marad — elutasításkor visszajön, elfogadáskor a
    tiszta példány árvaként kiveszi."""
    text = L._ptext(p)
    if text:
        L._apply(p, 0, len(text), "", ctx)
    for r in list(p.iter(R)):
        if L._owner(r) is not p or L._hidden(r, p) or not any(c.tag != RPR for c in r):
            continue
        d = etree.Element(DEL)
        d.set(q("id"), ctx.next_id())
        d.set(q("author"), ctx.author)
        d.set(q("date"), ctx.date)
        r.addprevious(d)
        d.append(r)
        for t in r.iter(q("instrText")):
            t.tag = q("delInstrText")
        ctx.added += 1
    ppr = p.find(PPR)
    if ppr is None:
        ppr = etree.Element(PPR)
        p.insert(0, ppr)
    _mark(ppr, DEL, ctx)


def _edit(p, new: str, ctx: L._Ctx) -> bool:
    """Szószintű követett csere egy bekezdésben; ha nem pontosan a kért szöveg lesz belőle, visszaáll. → sikerült-e"""
    old = L._ptext(p)
    if old == new:
        return True
    snap, before = deepcopy(p), ctx.added
    for s, e, t in sorted(L._word_ops(old, new), reverse=True):
        L._apply(p, s, e, t, ctx)
    if L._ptext(p) == new:
        return True
    p.getparent().replace(p, snap)
    ctx.added = before
    return False


def compare(src: Path, dst: Path, text: str, author: str) -> dict[str, Any]:
    """src → dst: a dokumentum `text`-re igazítva, követett változásként `author` nevén."""
    pkg = L._Pkg(src)
    body = L._body(pkg)
    doc = pkg.xml("word/document.xml")
    roots = [doc] + [pkg.xml(n) for n in pkg.raw if n.startswith(("word/footnotes", "word/endnotes", "word/comments"))]
    ctx = L._Ctx(author, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), L._max_id(*roots))
    old = _lines(body)
    new = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", "\n")
    if new.endswith("\n") and not (old and old[-1][2] == ""):
        new = new[:-1]
    new_t, old_t = new.split("\n"), [t for _, _, t in old]
    pairs: list[tuple[Optional[int], Optional[int]]] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_t, new_t, autojunk=False).get_opcodes():
        if op == "equal":
            pairs += [(i1 + k, j1 + k) for k in range(i2 - i1)]
        else:
            pairs += _pair(list(range(i1, i2)), list(range(j1, j2)), old_t, new_t)

    # tulajdonosonként: a bekezdés új sorai (None = a sor eltűnt), és ami utána új bekezdésként jön
    lines_of: dict[Any, list[Optional[str]]] = {}
    after: dict[Any, list[str]] = {}
    before_first: list[str] = []
    last_owner = None
    for i, j in pairs:
        if i is not None:
            owner, k, _ = old[i]
            lines_of.setdefault(owner, [t for o, _, t in old if o is owner])
            lines_of[owner][k] = new_t[j] if j is not None else None
            last_owner = owner
        elif last_owner is None:
            before_first.append(new_t[j])
        else:
            after.setdefault(last_owner, []).append(new_t[j])

    res: dict[str, Any] = {"changed": 0, "inserted": 0, "deleted": 0, "not_done": []}
    owners = list(dict.fromkeys(o for o, _, _ in old))
    for n, owner in enumerate(owners):
        lines = lines_of.get(owner)
        if owner.tag == _TR:
            if lines is not None and lines[0] is None:
                res["not_done"].append(f"table row “{old_t[[o for o, _, _ in old].index(owner)][:50]}” was left in — "
                                       "remove rows with table.deleteRow")
            elif lines is not None:
                cells, want = owner.findall(_TC), lines[0].split("\t")
                if len(want) != len(cells):
                    res["not_done"].append(f"table row “{lines[0][:50]}”: {len(want)} cells given, the row has "
                                           f"{len(cells)} — not changed")
                else:
                    for tc, w in zip(cells, want):
                        ps = tc.findall(P)
                        if _cell_text(tc) == w:
                            continue
                        if len(ps) == 1 and _edit(ps[0], w, ctx):
                            res["changed"] += 1
                        else:
                            res["not_done"].append(f"table cell “{_cell_text(tc)[:40]}” — change it with commands")
            if after.get(owner):
                res["not_done"].append(f"{len(after[owner])} line(s) after a table row weren't added as rows — "
                                       "add rows with table.insertRow")
            continue
        if lines is not None:
            kept = [t for t in lines if t is not None]
            if not kept:
                _delete_para(owner, ctx)
                res["deleted"] += 1
            elif "\n".join(kept) != L._ptext(owner):
                if _edit(owner, "\n".join(kept), ctx):
                    res["changed"] += 1
                else:
                    res["not_done"].append(f"paragraph “{L._ptext(owner)[:50]}” — the tracked change didn't come out "
                                           "exactly; not changed")
        extra = after.get(owner) or []
        if extra:
            model = owner
            if _is_heading(owner):                       # cím után szöveg jön: a következő (nem cím) bekezdés formája
                model = next((o for o in owners[n + 1:] if o.tag == P and not _is_heading(o)), owner)
            own = not _tracked_mark(owner) and owner.find(f"{PPR}/{q('sectPr')}") is None
            if own:                                     # az előző bekezdés jele lesz a beszúrt, az utolsó újé az eredeti
                ppr = owner.find(PPR)
                if ppr is None:
                    ppr = etree.Element(PPR)
                    owner.insert(0, ppr)
                _mark(ppr, INS, ctx)
            at = owner
            for k, t in enumerate(extra):
                np_ = _new_para(t, model, ctx, mark=not own or k < len(extra) - 1)
                at.addnext(np_)
                at = np_
                res["inserted"] += 1
    if before_first:
        first = next((o for o in owners if o.tag == P), None)
        at = None
        for t in before_first:
            np_ = _new_para(t, first, ctx)
            (at.addnext(np_) if at is not None else (first.addprevious(np_) if first is not None else body.insert(0, np_)))
            at = np_
            res["inserted"] += 1
    if ctx.added:
        pkg.put("word/document.xml", doc)
    pkg.save(dst)
    res["tracked_changes"] = ctx.added
    res["author"] = author
    return res
