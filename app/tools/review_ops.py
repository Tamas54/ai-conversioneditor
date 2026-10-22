"""A lektori kör további mozdulatai (a review_loop szövegmodelljére építve, közvetlenül az OOXML-en):

    new_comment   saját megjegyzés egy szövegrészhez („on”: pontos szöveg, „paragraph”: n) — kérdés a bírálónak
    accept/reject követett változások elfogadása/elutasítása: "all" | azonosító(k) | {author, paragraph, type, ids}
    delete        megjegyzés-szál törlése (a válaszokkal, a horgonyokkal és a Word-kiegészítő bejegyzésekkel)
    reopen        lezárt szál újranyitása;  edit: egy megjegyzés szövegének átírása
    final         tiszta példány: minden változás elfogadva, minden megjegyzés törölve

Minden Word-rész karban marad: comments, commentsExtended (szál, lezárás), commentsIds / commentsExtensible
(tartós azonosító, UTC-idő), people (szerzők). A csomag többi része bájtra ugyanaz.
"""
from __future__ import annotations

import random
import unicodedata
from typing import Any, Optional

from lxml import etree

from app.tools import review_loop as L
from app.tools.review_loop import (CRE, CREF, CRS, DEL, DT, INS, MOVE_FROM, MOVE_TO, P, PPR, R, RPR, T, W14, W15,
                                   W16CEX, W16CID, XML_SPACE, _Fail, q)

KINDS = ("new_comment", "accept", "reject", "final")
_TRACK_RUN = (INS, DEL, MOVE_FROM, MOVE_TO)
_PR_CHANGES = tuple(q(t) for t in ("rPrChange", "pPrChange", "tblPrChange", "trPrChange", "tcPrChange", "sectPrChange",
                                   "tblGridChange", "numberingChange"))
_MOVE_MARKS = tuple(q(t) for t in ("moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd"))
_COMMENTS_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
_COMMENTS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
_STORY = ("word/header", "word/footer", "word/footnotes", "word/endnotes")


def kind(item: dict[str, Any]) -> str:
    return next((k for k in KINDS if k in item), "comment")


# ---------- közös ----------

def person(pkg: L._Pkg, author: str) -> None:
    """A people.xml-ben (ha a fájl használja) legyen ott a szerző — a Word ebből mutatja a megjegyzés gazdáját."""
    people = pkg.xml("word/people.xml")
    if people is None:
        return
    if any(p.get(q("author", W15)) == author for p in people.iter(q("person", W15))):
        return
    p = etree.SubElement(people, q("person", W15))
    p.set(q("author", W15), author)
    pi = etree.SubElement(p, q("presenceInfo", W15))
    pi.set(q("providerId", W15), "None")
    pi.set(q("userId", W15), author)
    pkg.put("word/people.xml", people)


def _durable(pkg: L._Pkg, pid: str, date: str) -> None:
    ids = pkg.xml("word/commentsIds.xml")
    cex = pkg.xml("word/commentsExtensible.xml")
    if ids is None and cex is None:
        return
    durable = "%08X" % random.randint(1, 0x7FFFFFFE)
    if ids is not None:
        x = etree.SubElement(ids, q("commentId", W16CID))
        x.set(q("paraId", W16CID), pid)
        x.set(q("durableId", W16CID), durable)
        pkg.put("word/commentsIds.xml", ids)
    if cex is not None:
        x = etree.SubElement(cex, q("commentExtensible", W16CEX))
        x.set(q("durableId", W16CEX), durable)
        x.set(q("dateUtc", W16CEX), date)
        pkg.put("word/commentsExtensible.xml", cex)


def _story_roots(pkg: L._Pkg) -> list:
    """A dokumentumtörzs + élőfej/élőláb/lábjegyzet részek, amelyekben megjegyzés-jelölő lehet."""
    roots = [pkg.xml("word/document.xml")]
    for name in pkg.raw:
        if name.startswith(_STORY) and name.endswith(".xml") and b"comment" in pkg.raw[name]:
            roots.append(pkg.xml(name))
    return [r for r in roots if r is not None]


def _top(node, p, ctx: L._Ctx, start: bool):
    """A futás a bekezdés közvetlen gyermekéig emelve — a követett-változás burkot kettévágva, hogy a megjegyzés
    tartománya ne nőjön; más burok (hivatkozás, mező) egészben kerül a tartományba."""
    while node.getparent() is not None and node.getparent() is not p:
        par = node.getparent()
        if par.tag in _TRACK_RUN:
            moving = ([node] if start else []) + list(node.itersiblings())
            if moving and not (start and node.getprevious() is None):
                twin = etree.Element(par.tag, attrib=dict(par.attrib))
                twin.set(q("id"), ctx.next_id())
                for x in moving:
                    twin.append(x)
                par.addnext(twin)
                node = twin if start else par
                continue
        node = par
    return node


# ---------- új megjegyzés ----------

def _locate_any(scan: dict[str, Any], on: str, para: Optional[int]) -> tuple[int, int, int]:
    texts = scan["texts"]
    rng = [para - 1] if para else range(len(texts))
    if para and not 0 <= para - 1 < len(texts):
        raise _Fail(f"paragraph {para} doesn't exist (the document has {len(texts)})")
    for loose in (False, True):
        needle = L._loose(on)[0] if loose else on
        hits = []
        for i in rng:
            if loose:
                ls, idx = L._loose(texts[i])
                hits += [(i, idx[h], idx[h + len(needle) - 1] + 1) for h in L._find_all(ls, needle)]
            else:
                hits += [(i, h, h + len(needle)) for h in L._find_all(texts[i], needle)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            where = f"paragraph {para}" if para else "the document"
            raise _Fail(f"“{on[:60]}” appears {len(hits)} times in {where} — give paragraph or a longer piece of text")
    raise _Fail(f"“{on[:60]}” was not found" + (f" in paragraph {para}" if para else "") + " (the text as it reads now)")


def new_comment(pkg: L._Pkg, body, comments: dict[str, dict[str, Any]], item: dict[str, Any], ctx: L._Ctx,
                taken: set[str]) -> dict[str, Any]:
    text = unicodedata.normalize("NFC", str(item.get("new_comment") or "")).strip()
    if not text:
        raise _Fail("new_comment is empty")
    on = item.get("on") or item.get("find")
    para = item.get("paragraph")
    para = int(para) if para not in (None, "") else None
    scan = L._scan(body)
    if on:
        i, s, e = _locate_any(scan, unicodedata.normalize("NFC", str(on)), para)
    elif para:
        if not 0 <= para - 1 < len(scan["texts"]):
            raise _Fail(f"paragraph {para} doesn't exist (the document has {len(scan['texts'])})")
        i, s, e = para - 1, 0, len(scan["texts"][para - 1])
    else:
        raise _Fail("give `on` (the exact text to comment on) or `paragraph` (its number)")
    p = scan["paras"][i]
    L._normalize(p)
    L._split(p, s)
    L._split(p, e)
    atoms = [a for a in L._atoms(p) if a[3]]
    inside = [r for _, r, st, n in atoms if st >= s and st + n <= e]
    croot = pkg.xml(L._COMMENTS)
    if croot is None:
        croot = L._ensure_part(pkg, L._COMMENTS, q("comments"), {"w": L.W, "w14": W14}, _COMMENTS_CT, _COMMENTS_REL)
    new_id = str(max([int(k) for k in comments if k.isdigit()] + [-1]) + 1)
    crs, cre = etree.Element(CRS), etree.Element(CRE)
    crs.set(q("id"), new_id)
    cre.set(q("id"), new_id)
    rr = etree.Element(R)
    etree.SubElement(etree.SubElement(rr, RPR), q("rStyle")).set(q("val"), "CommentReference")
    etree.SubElement(rr, CREF).set(q("id"), new_id)
    if inside:
        _top(inside[0], p, ctx, True).addprevious(crs)
        _top(inside[-1], p, ctx, False).addnext(cre)
    else:                                    # üres bekezdés / pont-megjegyzés: a bekezdés végén
        p.append(crs)
        p.append(cre)
    cre.addnext(rr)
    c = etree.SubElement(croot, q("comment"))
    c.set(q("id"), new_id)
    c.set(q("author"), ctx.author)
    c.set(q("date"), ctx.date)
    c.set(q("initials"), L._initials(ctx.author))
    pid = ""
    for k, line in enumerate(text.split("\n")):
        cp = etree.SubElement(c, P)
        pid = L._para_id(taken)
        cp.set(q("paraId", W14), pid)
        cp.set(q("textId", W14), "77777777")
        if k == 0:
            ar = etree.SubElement(cp, R)
            etree.SubElement(etree.SubElement(ar, RPR), q("rStyle")).set(q("val"), "CommentReference")
            etree.SubElement(ar, q("annotationRef"))
        r = etree.SubElement(cp, R)
        t = etree.SubElement(r, T)
        t.text = line
        t.set(XML_SPACE, "preserve")
    pkg.put(L._COMMENTS, croot)
    L._comment_ex(pkg, pid)
    _durable(pkg, pid, ctx.date)
    person(pkg, ctx.author)
    comments[new_id] = {"el": c, "id": new_id, "author": ctx.author, "date": ctx.date, "text": text, "para_ids": [pid],
                        "ex_pid": pid, "parent": None, "root": new_id, "done": False}
    anchor = scan["texts"][i][s:e]
    return {"status": "commented", "comment_id": new_id, "paragraph": i + 1, "anchor": anchor[:200]}


# ---------- megjegyzés: törlés, újranyitás, átírás ----------

def delete_thread(pkg: L._Pkg, comments: dict[str, dict[str, Any]], root_id: str) -> int:
    ids = {k for k, v in comments.items() if v["root"] == root_id}
    pids = {pid for k in ids for pid in comments[k]["para_ids"]}
    croot = pkg.xml(L._COMMENTS)
    for k in ids:
        el = comments[k]["el"]
        if el.getparent() is not None:
            el.getparent().remove(el)
    pkg.put(L._COMMENTS, croot)
    for root in _story_roots(pkg):
        hit = False
        for el in list(root.iter(CRS, CRE, CREF)):
            if el.get(q("id")) not in ids:
                continue
            hit = True
            par = el.getparent()
            if el.tag == CREF and par is not None and par.tag == R and all(x.tag in (RPR, CREF) for x in par):
                par.getparent().remove(par)
            elif par is not None:
                par.remove(el)
        if hit:
            pkg.put(_name_of(pkg, root), root)
    ext = pkg.xml(L._COMMENTS_EX)
    if ext is not None:
        for e in list(ext.iter(q("commentEx", W15))):
            if e.get(q("paraId", W15)) in pids:
                ext.remove(e)
        pkg.put(L._COMMENTS_EX, ext)
    durables = set()
    cids = pkg.xml("word/commentsIds.xml")
    if cids is not None:
        for e in list(cids.iter(q("commentId", W16CID))):
            if e.get(q("paraId", W16CID)) in pids:
                durables.add(e.get(q("durableId", W16CID)))
                cids.remove(e)
        pkg.put("word/commentsIds.xml", cids)
    cex = pkg.xml("word/commentsExtensible.xml")
    if cex is not None and durables:
        for e in list(cex.iter(q("commentExtensible", W16CEX))):
            if e.get(q("durableId", W16CEX)) in durables:
                cex.remove(e)
        pkg.put("word/commentsExtensible.xml", cex)
    for k in ids:
        comments.pop(k, None)
    return len(ids)


def _name_of(pkg: L._Pkg, root) -> str:
    return next(n for n, r in pkg.trees.items() if r is root)


def reopen(pkg: L._Pkg, comments: dict[str, dict[str, Any]], root_id: str, taken: set[str]) -> None:
    L._comment_ex(pkg, L._thread_pid(pkg, comments[root_id], taken)).set(q("done", W15), "0")
    comments[root_id]["done"] = False


def edit_comment(pkg: L._Pkg, c: dict[str, Any], text: str) -> None:
    """A megjegyzés szövege helyett az új (a szálat és a lezárást tartó utolsó bekezdés marad)."""
    ps = list(c["el"].iter(P))
    if not ps:
        ps = [etree.SubElement(c["el"], P)]
    keep = next((p for p in ps if p.get(q("paraId", W14)) == c.get("ex_pid")), ps[-1])
    aref = next((r for p in ps for r in p.iter(R) if r.find(q("annotationRef")) is not None), None)
    for p in ps:
        if p is not keep:
            c["el"].remove(p)
    for x in list(keep):
        if x.tag != PPR:
            keep.remove(x)
    if aref is not None:
        keep.append(aref)
    r = etree.SubElement(keep, R)
    t = etree.SubElement(r, T)
    t.text = text.replace("\n", " ")
    t.set(XML_SPACE, "preserve")
    c["text"] = text


# ---------- követett változások ----------

def _changes(doc, part: str = "word/document.xml") -> list[dict[str, Any]]:
    """Minden függő változás: {el, id, author, type, para (0-tól, csak a törzsben) | None, part}."""
    body = doc.find(q("body"))
    scope = body if body is not None else doc            # láb-/végjegyzet, élőfej, élőláb: az egész rész
    index = {p: i for i, p in enumerate(L._paras(body))} if body is not None else {}
    out = []
    for el in scope.iter(*_TRACK_RUN, *_PR_CHANGES):
        par = el.getparent()
        if el.tag in (INS, DEL) and par is not None and par.tag == RPR and par.getparent() is not None \
                and par.getparent().tag == PPR:
            kind = "paragraph_mark_" + ("ins" if el.tag == INS else "del")
        elif el.tag in (INS, DEL) and par is not None and par.tag == q("trPr"):
            kind = "row_" + ("ins" if el.tag == INS else "del")
        elif el.tag in (INS, MOVE_TO):
            kind = "ins"
        elif el.tag in (DEL, MOVE_FROM):
            kind = "del"
        else:
            kind = "format"
        owner = next(el.iterancestors(P), None)
        if owner is None and kind.startswith("row_"):
            owner = next(par.getparent().iter(P), None)
        out.append({"el": el, "id": el.get(q("id")), "author": el.get(q("author")) or "?", "type": kind,
                    "para": index.get(owner), "part": part})
    return out


def _change_parts(pkg: L._Pkg) -> list[str]:
    """A törzs + minden rész, amelyben követett változás lehet (lábjegyzet, végjegyzet, élőfej, élőláb) — a tiszta
    példány a lábjegyzeten belüli változást is elfogadja (10-22: ott ins/del maradt)."""
    return ["word/document.xml"] + [n for n in sorted(pkg.raw) if n.startswith(_STORY) and n.endswith(".xml")]


def _matches(ch: dict[str, Any], spec: Any) -> bool:
    if spec in (True, "all", "*"):
        return True
    if isinstance(spec, (str, int)):
        return str(spec) == ch["id"]
    if isinstance(spec, list):
        return any(_matches(ch, s) for s in spec)
    if isinstance(spec, dict):
        ids = spec.get("ids") or ([spec["id"]] if spec.get("id") is not None else None)
        if ids is not None and ch["id"] not in {str(x) for x in ids}:
            return False
        if spec.get("author") and ch["author"] != spec["author"]:
            return False
        if spec.get("not_author") and ch["author"] == spec["not_author"]:
            return False
        if spec.get("paragraph") is not None and (ch["para"] is None or ch["para"] + 1 != int(spec["paragraph"])):
            return False
        if spec.get("type") and not ch["type"].startswith(str(spec["type"])):
            return False
        return True
    return False


def _unwrap(el) -> None:
    par = el.getparent()
    if par is None:
        return
    for x in list(el):
        el.addprevious(x)
    par.remove(el)


def _drop(el) -> None:
    if el.getparent() is not None:
        el.getparent().remove(el)


_ANCHORS = (CRS, CRE, q("bookmarkStart"), q("bookmarkEnd"), q("proofErr"), q("permStart"), q("permEnd"))


def _empty(p) -> bool:
    """Nincs benne semmi látható (szöveg, kép, mező, jegyzetjel) — csak a tulajdonságai és horgonyok."""
    return all(x.tag == PPR or x.tag in _ANCHORS or (x.tag == R and all(c.tag == RPR for c in x)) for x in p)


def _merge_next(p) -> None:
    """A bekezdésjel törlése elfogadva / beszúrása elutasítva: a következő bekezdés ebbe olvad. Ha a bekezdés egészében
    törölt volt (üresen maradt), ő tűnik el, és a következő a SAJÁT tulajdonságaival marad — ahogy Word fogadja el
    (10-22: a törölt bekezdések utáni „4 Magyarország…” címsor a törölt bekezdés Normal stílusát kapta)."""
    nxt = p.getnext()
    while nxt is not None and nxt.tag != P:
        nxt = nxt.getnext() if nxt.tag in (CRS, CRE, q("bookmarkStart"), q("bookmarkEnd")) else None
    if nxt is None:
        return
    ppr = p.find(PPR)
    if _empty(p) and (ppr is None or ppr.find(q("sectPr")) is None):      # szakaszhatárt hordozó bekezdés: a régi út
        at = 1 if nxt.find(PPR) is not None else 0
        for x in [x for x in p if x.tag != PPR]:                          # könyvjelző, megjegyzés-horgony átkerül
            nxt.insert(at, x)
            at += 1
        p.getparent().remove(p)
        return
    for x in list(nxt):
        if x.tag != PPR:
            p.append(x)
    nxt.getparent().remove(nxt)


def _restore_props(change) -> None:
    """Formázás-változás elutasítása: a változás előtti tulajdonságok vissza."""
    holder = change.getparent()
    old = change[0] if len(change) else None
    keep_tags = {q("rPr"), q("sectPr"), q("pPrChange")} if holder.tag == PPR else \
        {q("headerReference"), q("footerReference"), q("sectPrChange")} if holder.tag == q("sectPr") else \
        {q("cellIns"), q("cellDel"), q("cellMerge"), q("tcPrChange")} if holder.tag == q("tcPr") else {change.tag}
    for x in list(holder):
        if x is not change and x.tag not in keep_tags:
            holder.remove(x)
    if old is not None:
        for x in reversed(list(old)):
            if holder.tag == q("sectPr"):
                refs = [y for y in holder if y.tag in (q("headerReference"), q("footerReference"))]
                (refs[-1].addnext(x) if refs else holder.insert(0, x))
            else:
                holder.insert(0, x)
    holder.remove(change)


def _decide(ch: dict[str, Any], accept: bool) -> None:
    el, kind = ch["el"], ch["type"]
    if kind == "paragraph_mark_del":
        p = el.getparent().getparent().getparent()
        _drop(el)
        if accept:
            _merge_next(p)
    elif kind == "paragraph_mark_ins":
        p = el.getparent().getparent().getparent()
        _drop(el)
        if not accept:
            _merge_next(p)
    elif kind in ("row_ins", "row_del"):
        tr = el.getparent().getparent()
        _drop(el)
        if (kind == "row_del") == accept and tr.getparent() is not None:
            tr.getparent().remove(tr)
    elif kind == "ins":
        _unwrap(el) if accept else _drop(el)
    elif kind == "del":
        if accept:
            _drop(el)
        else:
            for t in el.iter(DT, q("delInstrText")):
                t.tag = T if t.tag == DT else q("instrText")
            _unwrap(el)
    else:                                   # formázás, táblázat, szakasz
        _drop(el) if accept else _restore_props(el)


def _pending(pkg: L._Pkg) -> list[dict[str, Any]]:
    return [c for name in _change_parts(pkg) if pkg.xml(name) is not None for c in _changes(pkg.xml(name), name)]


def decide(pkg: L._Pkg, spec: Any, accept: bool) -> dict[str, Any]:
    chosen = [c for c in _pending(pkg) if _matches(c, spec)]
    if not chosen:
        raise _Fail("no tracked change matches — read=\"comments\" lists them (id, author, paragraph, type)")
    by_type: dict[str, int] = {}
    for ch in reversed(chosen):              # hátulról: a beágyazott (belső) változás előbb
        _decide(ch, accept)
        by_type[ch["type"]] = by_type.get(ch["type"], 0) + 1
    for name in sorted({c["part"] for c in chosen}):
        root = pkg.xml(name)
        if not any(True for _ in root.iter(MOVE_FROM, MOVE_TO)):
            for m in list(root.iter(*_MOVE_MARKS)):
                _drop(m)
        pkg.put(name, root)
    authors = sorted({c["author"] for c in chosen})
    out = {"status": "accepted" if accept else "rejected", "changes": len(chosen), "by_type": by_type, "authors": authors}
    in_notes = sum(1 for c in chosen if c["part"] != "word/document.xml")
    if in_notes:
        out["in_notes_headers_footers"] = in_notes
    notes = tidy_notes(pkg)
    if notes["removed"]:
        out["notes_removed"] = notes["removed"]
    return out


# ---------- láb- és végjegyzetek rendben tartása ----------

_NOTES = (("word/footnotes.xml", "footnote", "footnoteReference"), ("word/endnotes.xml", "endnote", "endnoteReference"))


def tidy_notes(pkg: L._Pkg) -> dict[str, int]:
    """Árva láb-/végjegyzet (a hivatkozása elfogadott törléssel eltűnt) ki, a többi a hivatkozások sorrendjébe. A
    LibreOffice a jegyzeteket a fájlbeli SORRENDJÜK szerint rendeli a hivatkozásokhoz, nem azonosító szerint: egy bent
    maradt árva jegyzet után a hivatkozás alá a szomszéd szövege kerül (10-22: az IFR-jegyzet helyén a régi
    MI-rendeletes). Ami törölt (w:del) szövegben hivatkozott, az nem árva — elutasításkor kell."""
    out = {"removed": 0, "reordered": 0}
    for part, tag, notes, normal, order in _note_parts(pkg):
        orphans = [n for n in normal if n.get(q("id")) not in order]
        for n in orphans:
            notes.remove(n)
        kept = [n for n in normal if n.get(q("id")) in order]
        want = sorted(kept, key=lambda n: order[n.get(q("id"))])
        moved = want != kept
        if moved:
            for n in kept:
                notes.remove(n)
            for n in want:
                notes.append(n)
        if orphans or moved:
            pkg.put(part, notes)
        out["removed"] += len(orphans)
        out["reordered"] += int(moved)
    return out


def _note_parts(pkg: L._Pkg):
    """(rész, fajta, gyökér, a normál jegyzetek a fájl sorrendjében, hivatkozott id → első hivatkozás sorszáma)"""
    for part, tag, ref in _NOTES:
        notes = pkg.xml(part)
        if notes is None:
            continue
        roots = [pkg.xml("word/document.xml")] + [pkg.xml(n) for n in sorted(pkg.raw)
                                                  if n.startswith(_STORY) and n.endswith(".xml") and n != part]
        order: dict[str, int] = {}
        for root in roots + [notes]:
            for el in root.iter(q(ref)) if root is not None else ():
                order.setdefault(el.get(q("id")), len(order))
        normal = [n for n in notes.findall(q(tag)) if (n.get(q("type")) or "normal") == "normal"]
        yield part, tag, notes, normal, order


def notes_report(pkg: L._Pkg) -> dict[str, dict[str, Any]]:
    """{"footnote" | "endnote": {orphans: [id], out_of_order: bool}} — csak ahol baj van (a validátornak)."""
    out = {}
    for _, tag, _, normal, order in _note_parts(pkg):
        orphans = [n.get(q("id")) for n in normal if n.get(q("id")) not in order]
        kept = [order[n.get(q("id"))] for n in normal if n.get(q("id")) in order]
        if orphans or kept != sorted(kept):
            out[tag] = {"orphans": orphans, "out_of_order": kept != sorted(kept)}
    return out


def final(pkg: L._Pkg, comments: dict[str, dict[str, Any]]) -> dict[str, Any]:
    n_changes, notes = 0, {}
    if _pending(pkg):
        res = decide(pkg, "all", True)
        n_changes, notes = res["changes"], res
    else:
        tidy_notes(pkg)
    roots = [k for k, v in comments.items() if v["root"] == k]
    n_comments = sum(delete_thread(pkg, comments, r) for r in roots)
    out = {"status": "final", "changes_accepted": n_changes, "comments_removed": n_comments}
    for k in ("in_notes_headers_footers", "notes_removed"):
        if notes.get(k):
            out[k] = notes[k]
    return out
