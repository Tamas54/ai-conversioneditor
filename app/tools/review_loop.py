"""Lektori kör egy DOCX-en: a bíráló (pl. a főnök) megjegyzései → javítás EGY hívásban követett változásként, válasz
a szálba → a következő körben: mi változott a két verzió között.

    worklist(file_id)               a nyitott megjegyzés-szálak: id, szerző, szöveg, a kijelölt (horgony) szöveg, a
                                    bekezdés szövege és sorszáma (edit.goto {"paragraph": n}), válaszok, lezárt-e; a
                                    függő követett változások külön listában
    address(file_id, items, author) javítások szószintű követett változásként a szerző nevén + válasz a szálba
                                    (+ lezárás); a bíráló saját változásait nem fogadja el és nem veti el. Új file_id,
                                    tételenként fixed | replied | resolved | failed — semmi nem marad ki csendben
    changes(file_id, base_file_id)  mi változott a korábbi verzióhoz képest: bekezdésenként előtte/utána + szódiff,
                                    követett-e, új megjegyzések és válaszok, lezárt szálak

Közvetlenül az OOXML-en (lxml): a csomag többi része bájtra ugyanaz marad, a szétvágott futások (w:r) viszik a
formázásukat. A közös szövegmodell a „végleges nézet” (törlések nélkül, a beszúrásokkal — ahogy Word „egyszerű
jelölés” módban mutatja): ebben keres a find, és ezt adja vissza a bekezdés-szöveg.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import random
import re
import time
import unicodedata
import zipfile
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from lxml import etree

log = logging.getLogger("aice.review_loop")

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
W16CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
W16CEX = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def q(tag: str, ns: str = W) -> str:
    return f"{{{ns}}}{tag}"


P, R, T, DT, RPR, PPR = q("p"), q("r"), q("t"), q("delText"), q("rPr"), q("pPr")
INS, DEL, MOVE_FROM, MOVE_TO = q("ins"), q("del"), q("moveFrom"), q("moveTo")
CRS, CRE, CREF = q("commentRangeStart"), q("commentRangeEnd"), q("commentReference")
TXBX = q("txbxContent")
_CHARS = {q("tab"): "\t", q("br"): "\n", q("cr"): "\n", q("noBreakHyphen"): "‑"}
_TRACKED = (INS, DEL, MOVE_FROM, MOVE_TO)
_DOCX = (".docx", ".docm", ".dotx")
_COMMENTS = "word/comments.xml"
_COMMENTS_EX = "word/commentsExtended.xml"
_EX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml"
_EX_REL = "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
_PARSER = etree.XMLParser(huge_tree=True)


# ---------- csomag ----------

class _Pkg:
    """Egy docx zip-részei; mentéskor csak a módosított részek íródnak újra, a többi bájtra ugyanaz."""

    def __init__(self, path: Path):
        with zipfile.ZipFile(path) as z:
            self.infos = z.infolist()
            self.raw = {i.filename: z.read(i.filename) for i in self.infos}
        self.trees: dict[str, Any] = {}
        self.dirty: set[str] = set()

    def xml(self, name: str):
        if name not in self.trees:
            if name not in self.raw:
                return None
            self.trees[name] = etree.fromstring(self.raw[name], _PARSER)
        return self.trees[name]

    def put(self, name: str, root) -> None:
        self.trees[name] = root
        self.dirty.add(name)

    def save(self, dst: Path) -> None:
        def data(name: str) -> bytes:
            if name in self.dirty:
                return etree.tostring(self.trees[name], xml_declaration=True, encoding="UTF-8", standalone=True)
            return self.raw[name]
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
            for info in self.infos:
                z.writestr(info, data(info.filename))
            for name in sorted(self.dirty - {i.filename for i in self.infos}):
                z.writestr(name, data(name))


def _load(file_id: str) -> _Pkg:
    from app.tools.crafts import existing
    path = existing(file_id)
    if path.suffix.lower() not in _DOCX:
        raise ValueError("This works on Word files (.docx) — convert the file to docx first.")
    return _Pkg(path)


def _body(pkg: _Pkg):
    doc = pkg.xml("word/document.xml")
    body = doc.find(q("body")) if doc is not None else None
    if body is None:
        raise ValueError("The document has no body (word/document.xml).")
    return body


# ---------- szövegmodell: a végleges nézet ----------

def _paras(body) -> list:
    """Bekezdések dokumentum-sorrendben, a táblázatcellákéval és az üresekkel együtt — ahogy az edit.goto
    {"paragraph": n} számol —, a szövegdobozok belseje nélkül."""
    return [p for p in body.iter(P) if not any(a.tag == TXBX for a in p.iterancestors())]


def _owner(el):
    return next(el.iterancestors(P), None)


def _hidden(el, p) -> bool:
    """Törölt (w:del / w:moveFrom) tartalom-e — a végleges nézetben nem látszik."""
    for a in el.iterancestors():
        if a is p:
            return False
        if a.tag in (DEL, MOVE_FROM):
            return True
    return False


def _char(el) -> str:
    return (el.text or "") if el.tag == T else _CHARS.get(el.tag, "")


def _is_text(el) -> bool:
    return (el.tag == T or el.tag in _CHARS) and el.getparent() is not None and el.getparent().tag == R


def _atoms(p) -> list[tuple[Any, Any, int, int]]:
    """(szövegelem, futás, kezdő offset, hossz) — a bekezdés végleges nézetének darabjai."""
    out, off = [], 0
    for el in p.iter(T, *_CHARS):
        if not _is_text(el) or _owner(el) is not p or _hidden(el, p):
            continue
        n = len(_char(el))
        out.append((el, el.getparent(), off, n))
        off += n
    return out


def _ptext(p) -> str:
    return "".join(_char(a[0]) for a in _atoms(p))


def _marks(p) -> list[tuple[str, str, int]]:
    """(jelölő-tag, megjegyzés-id, offset) a bekezdés megjegyzés-jelölőire, a végleges nézet szerinti offsettel."""
    out, off = [], 0
    for el in p.iter():
        if el is p or _owner(el) is not p:
            continue
        if el.tag in (CRS, CRE, CREF):
            out.append((el.tag, el.get(q("id")), off))
        elif _is_text(el) and not _hidden(el, p):
            off += len(_char(el))
    return out


def _scan(body) -> dict[str, Any]:
    """Bekezdések, szövegük, és minden megjegyzés horgonya: start/end/ref → (bekezdés-index, offset)."""
    paras = _paras(body)
    texts = [_ptext(p) for p in paras]
    pos: dict[str, dict[str, tuple[int, int]]] = {"start": {}, "end": {}, "ref": {}}
    key = {CRS: "start", CRE: "end", CREF: "ref"}
    for i, p in enumerate(paras):
        for tag, cid, off in _marks(p):
            pos[key[tag]].setdefault(cid, (i, off))
    index = {p: i for i, p in enumerate(paras)}
    for el in body.iter(CRS, CRE):                      # bekezdések KÖZÖTT álló jelölő (a séma megengedi)
        if _owner(el) is not None:
            continue
        cid = el.get(q("id"))
        if el.tag == CRS:
            nxt = next((x for x in el.itersiblings() if x.tag == P or x.find(".//" + P) is not None), None)
            p = nxt if nxt is None or nxt.tag == P else next(nxt.iter(P))
            if p in index:
                pos["start"].setdefault(cid, (index[p], 0))
        else:
            prv = next((x for x in el.itersiblings(preceding=True) if x.tag == P or x.find(".//" + P) is not None), None)
            p = prv if prv is None or prv.tag == P else list(prv.iter(P))[-1]
            if p in index:
                pos["end"].setdefault(cid, (index[p], len(texts[index[p]])))
    return {"paras": paras, "texts": texts, **pos}


def _anchor(scan: dict[str, Any], cid: str) -> Optional[tuple[int, int, int, int]]:
    """(első bekezdés, kezdet, utolsó bekezdés, vég) — a megjegyzéshez kijelölt szöveg; pont-megjegyzésnél üres tartomány
    a hivatkozás helyén."""
    s, e, ref = scan["start"].get(cid), scan["end"].get(cid), scan["ref"].get(cid)
    if s and e and (e[0], e[1]) >= (s[0], s[1]):
        return s[0], s[1], e[0], e[1]
    at = ref or s or e
    return (at[0], at[1], at[0], at[1]) if at else None


def _anchor_text(scan: dict[str, Any], a: tuple[int, int, int, int]) -> str:
    ps, s, pe, e = a
    t = scan["texts"]
    if ps == pe:
        return t[ps][s:e]
    return "\n".join([t[ps][s:]] + t[ps + 1:pe] + [t[pe][:e]])


# ---------- megjegyzések, szálak ----------

def _comments(pkg: _Pkg) -> dict[str, dict[str, Any]]:
    """id → {author, date, text, para_ids, parent, root, done} a comments.xml + commentsExtended.xml alapján."""
    root = pkg.xml(_COMMENTS)
    if root is None:
        return {}
    ex: dict[str, tuple[Optional[str], bool]] = {}
    ext = pkg.xml(_COMMENTS_EX)
    if ext is not None:
        for e in ext.iter(q("commentEx", W15)):
            ex[e.get(q("paraId", W15))] = (e.get(q("paraIdParent", W15)), e.get(q("done", W15)) == "1")
    out: dict[str, dict[str, Any]] = {}
    by_para: dict[str, str] = {}
    for c in root.iter(q("comment")):
        cid = c.get(q("id"))
        ps = [p for p in c.iter(P)]
        pids = [p.get(q("paraId", W14)) for p in ps if p.get(q("paraId", W14))]
        out[cid] = {"el": c, "id": cid, "author": c.get(q("author")) or "?", "date": c.get(q("date")),
                    "text": "\n".join(_ptext(p) for p in ps).strip(), "para_ids": pids}
        for pid in pids:
            by_para[pid] = cid
    for c in out.values():
        # a commentEx a megjegyzés UTOLSÓ bekezdésére mutat (MS-DOCX); a régebbi írók az elsőre — mindkettőt nézzük
        c["ex_pid"] = next((pid for pid in reversed(c["para_ids"]) if pid in ex), None)
        info = ex.get(c["ex_pid"], (None, False)) if c["ex_pid"] else (None, False)
        c["parent"] = by_para.get(info[0]) if info[0] else None
        c["done"] = info[1]
    for c in out.values():
        r, seen = c, {c["id"]}
        while r["parent"] and r["parent"] in out and r["parent"] not in seen:
            seen.add(r["parent"])
            r = out[r["parent"]]
        c["root"] = r["id"]
    return out


def _revisions(scan: dict[str, Any]) -> list[dict[str, Any]]:
    """Függő követett változások bekezdésenként: ins | del | format, szerző, dátum, szöveg."""
    out = []
    for i, p in enumerate(scan["paras"]):
        for el in p.iter(INS, DEL, MOVE_TO, MOVE_FROM, q("rPrChange")):
            if _owner(el) is not p:
                continue
            if el.tag == q("rPrChange"):
                r = el.getparent().getparent() if el.getparent() is not None else None
                text = "".join(_char(x) for x in r.iter(T)) if r is not None else ""
                kind = "format"
            elif el.tag in (INS, MOVE_TO):
                text, kind = "".join(_char(x) for x in el.iter(T, *_CHARS) if _is_text(x)), "ins"
            else:
                text, kind = "".join(x.text or "" for x in el.iter(DT)), "del"
            if text:
                out.append({"paragraph": i + 1, "type": kind, "author": el.get(q("author")) or "?",
                            "date": el.get(q("date")), "text": text[:300]})
    return out


# ---------- 1. munkalista ----------

_FIX_HINT = ("Fix them in ONE call: wordcraft(file_id, author=<the person's name>, address=[{\"comment\": id, "
             "\"replace\": {\"find\": exact text from the anchor or its paragraph, \"with\": new text} or \"paragraph\": "
             "full new paragraph text, \"reply\": \"Fixed: …\", \"resolve\": false}, …]).")


def worklist(file_id: str, offset: int = 0, max_chars: int = 20_000, include_resolved: bool = False) -> dict[str, Any]:
    from app.tools.wordcraft import _page_items
    pkg = _load(file_id)
    scan = _scan(_body(pkg))
    comments = _comments(pkg)
    revs = _revisions(scan)
    revs_by_para: dict[int, list] = defaultdict(list)
    for r in revs:
        revs_by_para[r["paragraph"]].append({k: r[k] for k in ("type", "author", "text")})
    threads = []
    for c in comments.values():
        if c["root"] != c["id"]:
            continue
        a = _anchor(scan, c["id"])
        item: dict[str, Any] = {"id": c["id"], "author": c["author"], "date": c["date"], "comment": c["text"],
                                "resolved": c["done"]}
        if a:
            item["anchor"] = _anchor_text(scan, a)
            item["paragraph"] = a[0] + 1
            if a[2] != a[0]:
                item["paragraph_to"] = a[2] + 1
            item["paragraph_text"] = scan["texts"][a[0]]
            if revs_by_para.get(a[0] + 1):
                item["changes_in_paragraph"] = revs_by_para[a[0] + 1][:12]
        else:
            item["anchor"] = None
        item["replies"] = [{"id": r["id"], "author": r["author"], "date": r["date"], "text": r["text"]}
                           for r in comments.values() if r["root"] == c["id"] and r["id"] != c["id"]]
        item["_pos"] = (a[0], a[1]) if a else (10 ** 9, 0)
        threads.append(item)
    threads.sort(key=lambda t: t.pop("_pos"))
    n_open = sum(1 for t in threads if not t["resolved"])
    shown = [t for t in threads if include_resolved or not t["resolved"]]
    page, nxt = _page_items(shown, offset, max_chars)
    res: dict[str, Any] = {"file_id": file_id, "open": n_open, "resolved": len(threads) - n_open,
                           "threads": page, "threads_total": len(shown), "offset": offset, "next_offset": nxt}
    if offset == 0:
        res["tracked_changes"] = revs[:60]
        res["tracked_changes_total"] = len(revs)
    if not comments:
        res["note"] = "The document has no comments."
    elif n_open:
        res["next"] = _FIX_HINT
    return res


# ---------- 2. javítás: keresés a horgonyban ----------

_LOOSE = {" ": " ", " ": " ", " ": " ", " ": " ", " ": " ", "\t": " ", "\n": " ",
          "„": '"', "“": '"', "”": '"', "«": '"', "»": '"', "″": '"',
          "‚": "'", "‘": "'", "’": "'", "‹": "'", "›": "'", "′": "'",
          "–": "-", "—": "-", "‒": "-", "−": "-", "‑": "-", "‐": "-"}


def _loose(s: str) -> tuple[str, list[int]]:
    """Laza alak + eredeti indexek: nem törő szóköz, idézőjel- és kötőjelfajták egyformán, összevont szóközök,
    feltételes elválasztó nélkül — a modell ritkán adja vissza pontosan ezeket a karaktereket."""
    out, idx = [], []
    for i, ch in enumerate(s):
        if ch == "­":
            continue
        ch = _LOOSE.get(ch, " " if ch.isspace() else ch)
        if ch == " " and out and out[-1] == " ":
            continue
        out.append(ch)
        idx.append(i)
    return "".join(out), idx


def _find_all(hay: str, needle: str) -> list[int]:
    out, i = [], hay.find(needle)
    while i >= 0:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


class _Fail(Exception):
    pass


def _locate(scan: dict[str, Any], a: tuple[int, int, int, int], find: str) -> tuple[int, int, int, bool]:
    """(bekezdés-index, kezdet, vég, laza-e) — a find egyetlen előfordulása előbb a kijelölt szövegben, aztán a horgony
    bekezdés(ei)ben; pontos egyezés előbb, utána laza. Több előfordulás → hiba (kérj hosszabb find-ot)."""
    ps, s, pe, e = a
    texts = scan["texts"]
    scopes = [[(i, s if i == ps else 0, e if i == pe else len(texts[i])) for i in range(ps, pe + 1)],
              [(i, 0, len(texts[i])) for i in range(ps, pe + 1)]]
    for loose in (False, True):
        needle = _loose(find)[0] if loose else find
        if not needle.strip():
            raise _Fail("`find` is empty")
        for level, scope in enumerate(scopes):
            hits = []
            for i, a0, b0 in scope:
                seg = texts[i][a0:b0]
                if loose:
                    ls, idx = _loose(seg)
                    hits += [(i, a0 + idx[h], a0 + idx[h + len(needle) - 1] + 1) for h in _find_all(ls, needle)]
                else:
                    hits += [(i, a0 + h, a0 + h + len(needle)) for h in _find_all(seg, needle)]
            hits = sorted(set(hits))
            if len(hits) == 1:
                return hits[0] + (loose,)
            if len(hits) > 1:
                where = "the commented text" if level == 0 else "the paragraph"
                raise _Fail(f"`find` appears {len(hits)} times in {where} — give a longer, unique piece of text")
    snippet = texts[ps][:160] + ("…" if len(texts[ps]) > 160 else "")
    raise _Fail(f"`find` text not found in the commented passage or its paragraph (paragraph {ps + 1}: “{snippet}”)")


# ---------- 2. javítás: szószintű diff ----------

# egy szám ezres tagolással („1 200 000”, „1.200.000,50”) egyetlen szó: a jogász az egész összeg cseréjét olvassa
_TOKEN = re.compile(r"\d+(?:[   .,]\d+)*|\w+|\s+|[^\w\s]", re.UNICODE)


def _tokens(s: str) -> list[str]:
    return _TOKEN.findall(s)


def _word_ops(old: str, new: str) -> list[tuple[int, int, str]]:
    """Szószintű változtatások (kezdet, vég az old-ban, új szöveg) — a jogász ezt olvassa redline-ként. Ha a két szöveg
    alig hasonlít, egyetlen csere a közös eleje és vége között (a szavankénti konfetti olvashatatlan)."""
    if old == new:
        return []
    a, b = _tokens(old), _tokens(new)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    pos_a = [0]
    for t in a:
        pos_a.append(pos_a[-1] + len(t))
    if sm.ratio() < 0.5:                          # a közös eleje és vége marad, a köztes szavak egy cserében
        n = min(len(a), len(b))
        pre = 0
        while pre < n and a[pre] == b[pre]:
            pre += 1
        suf = 0
        while suf < n - pre and a[-1 - suf] == b[-1 - suf]:
            suf += 1
        return [(pos_a[pre], pos_a[len(a) - suf], "".join(b[pre:len(b) - suf]))]
    ops: list[list[Any]] = []
    gap: Optional[str] = None             # a két változás közti változatlan szakasz
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            gap = "".join(a[i1:i2])
            continue
        start, end, text = pos_a[i1], pos_a[i2], "".join(b[j1:j2])
        if ops and gap is not None and not gap.strip():          # csak szóköz választja el: egy változás
            ops[-1][1] = end
            ops[-1][2] += gap + text
        else:
            ops.append([start, end, text])
        gap = None
    return [(s, e, t) for s, e, t in ops]


# ---------- 2. javítás: OOXML-műtét ----------

class _Ctx:
    def __init__(self, author: str, date: str, last_id: int):
        self.author, self.date, self.n = author, date, last_id
        self.added = 0

    def next_id(self) -> str:
        self.n += 1
        return str(self.n)


def _max_id(*roots) -> int:
    m = 0
    for root in roots:
        if root is None:
            continue
        for el in root.iter():
            v = el.get(q("id"))
            if v and v.isdigit():
                m = max(m, int(v))
    return m


def _normalize(p) -> None:
    """Minden (látható) futásban egyetlen tartalomelem — így egy futás bárhol kettévágható, a formázás (rPr) másolva."""
    for r in list(p.iter(R)):
        if _owner(r) is not p or _hidden(r, p):
            continue
        content = [c for c in r if c.tag != RPR]
        if len(content) < 2 or not any(c.tag == T or c.tag in _CHARS for c in content):
            continue
        rpr = r.find(RPR)
        prev = r
        for c in content[1:]:
            nr = etree.Element(R, attrib=dict(r.attrib))
            if rpr is not None:
                nr.append(deepcopy(rpr))
            nr.append(c)
            prev.addnext(nr)
            prev = nr


def _split(p, pos: int) -> None:
    for el, r, s, n in _atoms(p):
        if s < pos < s + n and el.tag == T:
            k = pos - s
            r2 = deepcopy(r)
            t2 = r2.find(T)
            text = el.text or ""
            el.text, t2.text = text[:k], text[k:]
            el.set(XML_SPACE, "preserve")
            t2.set(XML_SPACE, "preserve")
            r.addnext(r2)
            return


def _rpr_copy(r) -> Any:
    rpr = r.find(RPR) if r is not None else None
    if rpr is None:
        return None
    rpr = deepcopy(rpr)
    for c in list(rpr):
        if c.tag in (q("rPrChange"), INS, DEL) or (c.tag == q("rStyle") and c.get(q("val")) == "CommentReference"):
            rpr.remove(c)
    return rpr


def _new_ins(text: str, rpr, ctx: _Ctx):
    ins = etree.Element(INS)
    ins.set(q("id"), ctx.next_id())
    ins.set(q("author"), ctx.author)
    ins.set(q("date"), ctx.date)
    r = etree.SubElement(ins, R)
    if rpr is not None:
        r.append(rpr)
    for piece in re.split(r"(\t|\n)", text):
        if piece == "\t":
            etree.SubElement(r, q("tab"))
        elif piece == "\n":
            etree.SubElement(r, q("br"))
        elif piece:
            t = etree.SubElement(r, T)
            t.text = piece
            t.set(XML_SPACE, "preserve")
    return ins


def _lift(el, ctx: _Ctx) -> None:
    """Követett beszúrás nem lehet másik követett változás (w:ins/w:del) belsejében: a befoglalót kettévágjuk, és a
    két fele közé kerül — a bíráló változása szövegében és szerzőjében ugyanaz marad."""
    parent = el.getparent()
    while parent is not None and parent.tag in _TRACKED:
        tail = list(el.itersiblings())
        if tail:
            twin = etree.Element(parent.tag, attrib=dict(parent.attrib))
            twin.set(q("id"), ctx.next_id())
            for x in tail:
                twin.append(x)
            parent.addnext(twin)
        parent.addnext(el)
        if len(parent) == 0:
            parent.getparent().remove(parent)
        parent = el.getparent()


def _apply(p, start: int, end: int, new: str, ctx: _Ctx) -> None:
    """A végleges nézet [start, end) szakasza helyett `new`, követett változásként: a régi futások w:del-be (a másik
    szerző beszúrásán belül beágyazva, ahogy Word teszi; a saját korábbi beszúrás egyszerűen törlődik), az új szöveg
    w:ins-be az első törölt futás formázásával."""
    _normalize(p)
    _split(p, start)
    _split(p, end)
    atoms = _atoms(p)
    victims = [r for _, r, s, n in atoms if n and s >= start and s + n <= end]
    if victims:
        src = victims[0]
    else:
        src = next((r for _, r, s, n in reversed(atoms) if n and s + n == start), None)
        if src is None:
            src = next((r for _, r, s, n in atoms if n and s == start), None)
    rpr = _rpr_copy(src)
    holder = etree.Element(q("placeholder"))           # a beszúrás helye (a törlés átrendezi a futásokat)
    if victims:
        last = victims[0]
        while last.getnext() is not None and last.getnext() in victims:
            last = last.getnext()
        last.addnext(holder)
    elif src is not None and any(r is src and s + n == start for _, r, s, n in atoms):
        src.addnext(holder)
    elif src is not None:
        src.addprevious(holder)
    else:
        p.append(holder)
    groups: list[list] = []
    for r in victims:
        if groups and groups[-1][-1].getnext() is r:
            groups[-1].append(r)
        else:
            groups.append([r])
    for g in groups:
        parent = g[0].getparent()
        if parent.tag in (INS, MOVE_TO) and parent.get(q("author")) == ctx.author:
            for r in g:                                       # saját beszúrás törlése: egyszerűen eltűnik
                parent.remove(r)
            if len(parent) == 0:
                parent.getparent().remove(parent)
            continue
        d = etree.Element(DEL)
        d.set(q("id"), ctx.next_id())
        d.set(q("author"), ctx.author)
        d.set(q("date"), ctx.date)
        g[0].addprevious(d)
        for r in g:
            for t in r.iter(T):
                t.tag = DT
                t.set(XML_SPACE, "preserve")
            d.append(r)
        ctx.added += 1
    if new:
        ins = _new_ins(new, rpr, ctx)
        holder.addprevious(ins)
        _lift(ins, ctx)
        ctx.added += 1
    parent = holder.getparent()
    parent.remove(holder)
    if parent.tag in _TRACKED and len(parent) == 0:     # kettévágás után üresen maradt változás-burok
        parent.getparent().remove(parent)


# ---------- 2. javítás: válasz, lezárás ----------

def _para_id(taken: set[str]) -> str:
    while True:
        v = "%08X" % random.randint(1, 0x7FFFFFFE)
        if v not in taken:
            taken.add(v)
            return v


def _ensure_part(pkg: _Pkg, name: str, root_tag: str, nsmap: dict[str, str], ctype: str, rel: str):
    root = pkg.xml(name)
    if root is not None:
        return root
    root = etree.Element(root_tag, nsmap=nsmap)
    pkg.put(name, root)
    ct = pkg.xml("[Content_Types].xml")
    if ct is not None:
        o = etree.SubElement(ct, q("Override", CT))
        o.set("PartName", "/" + name)
        o.set("ContentType", ctype)
        pkg.put("[Content_Types].xml", ct)
    rels = pkg.xml("word/_rels/document.xml.rels")
    if rels is not None:
        ids = [int(m.group(1)) for x in rels for m in [re.match(r"rId(\d+)$", x.get("Id") or "")] if m]
        x = etree.SubElement(rels, q("Relationship", PR))
        x.set("Id", f"rId{max(ids or [0]) + 1}")
        x.set("Type", rel)
        x.set("Target", name.split("/", 1)[1])
        pkg.put("word/_rels/document.xml.rels", rels)
    return root


def _comment_ex(pkg: _Pkg, pid: str):
    ext = _ensure_part(pkg, _COMMENTS_EX, q("commentsEx", W15), {"w15": W15}, _EX_CT, _EX_REL)
    pkg.put(_COMMENTS_EX, ext)
    for e in ext.iter(q("commentEx", W15)):
        if e.get(q("paraId", W15)) == pid:
            return e
    e = etree.SubElement(ext, q("commentEx", W15))
    e.set(q("paraId", W15), pid)
    e.set(q("done", W15), "0")
    return e


def _thread_pid(pkg: _Pkg, c: dict[str, Any], taken: set[str]) -> str:
    """Amire a commentsExtended már mutat; különben a megjegyzés utolsó bekezdésének w14:paraId-je (ha nincs, kap
    egyet)."""
    if c.get("ex_pid"):
        return c["ex_pid"]
    if c["para_ids"]:
        return c["para_ids"][-1]
    ps = list(c["el"].iter(P))
    if not ps:
        ps = [etree.SubElement(c["el"], P)]
    pid = _para_id(taken)
    ps[-1].set(q("paraId", W14), pid)
    c["para_ids"] = [pid]
    pkg.put(_COMMENTS, pkg.xml(_COMMENTS))
    return pid


def _initials(name: str) -> str:
    return "".join(w[0] for w in name.split()[:3] if w).upper() or "A"


def _reply(pkg: _Pkg, body, comments: dict[str, dict[str, Any]], root_id: str, text: str, ctx: _Ctx,
           taken: set[str]) -> str:
    """Válasz a szálba: új w:comment, commentsExtended paraIdParent, és a dokumentumban a szülő mellé saját
    tartomány + hivatkozás (Word és LibreOffice a hivatkozás nélküli választ eldobja)."""
    croot = pkg.xml(_COMMENTS)
    new_id = str(max([int(k) for k in comments if k.isdigit()] + [-1]) + 1)
    c = etree.SubElement(croot, q("comment"))
    c.set(q("id"), new_id)
    c.set(q("author"), ctx.author)
    c.set(q("date"), ctx.date)
    c.set(q("initials"), _initials(ctx.author))
    pid = ""
    for line in text.strip().split("\n"):
        cp = etree.SubElement(c, P)
        pid = _para_id(taken)
        cp.set(q("paraId", W14), pid)
        cp.set(q("textId", W14), "77777777")
        r = etree.SubElement(cp, R)
        t = etree.SubElement(r, T)
        t.text = line
        t.set(XML_SPACE, "preserve")
    pkg.put(_COMMENTS, croot)
    parent_pid = _thread_pid(pkg, comments[root_id], taken)
    _comment_ex(pkg, parent_pid)
    _comment_ex(pkg, pid).set(q("paraIdParent", W15), parent_pid)
    ids = pkg.xml("word/commentsIds.xml")                 # Word 2019+ tartós azonosítók, ha a fájl használja
    durable = "%08X" % random.randint(1, 0x7FFFFFFE)
    if ids is not None:
        x = etree.SubElement(ids, q("commentId", W16CID))
        x.set(q("paraId", W16CID), pid)
        x.set(q("durableId", W16CID), durable)
        pkg.put("word/commentsIds.xml", ids)
    cex = pkg.xml("word/commentsExtensible.xml")
    if cex is not None:
        x = etree.SubElement(cex, q("commentExtensible", W16CEX))
        x.set(q("durableId", W16CEX), durable)
        x.set(q("dateUtc", W16CEX), ctx.date)
        pkg.put("word/commentsExtensible.xml", cex)
    thread = {k for k, v in comments.items() if v["root"] == root_id}
    start = next((el for el in body.iter(CRS) if el.get(q("id")) == root_id), None)
    refs = [el for el in body.iter(CREF) if el.get(q("id")) in thread]
    end = next((el for el in body.iter(CRE) if el.get(q("id")) == root_id), None)
    after = refs[-1].getparent() if refs and refs[-1].getparent().tag == R else end
    if after is not None:
        if start is not None:
            s = etree.Element(CRS)
            s.set(q("id"), new_id)
            start.addnext(s)
        e = etree.Element(CRE)
        e.set(q("id"), new_id)
        rr = etree.Element(R)
        etree.SubElement(etree.SubElement(rr, RPR), q("rStyle")).set(q("val"), "CommentReference")
        etree.SubElement(rr, CREF).set(q("id"), new_id)
        after.addnext(e)
        e.addnext(rr)
    comments[new_id] = {"el": c, "id": new_id, "author": ctx.author, "date": ctx.date, "text": text.strip(),
                        "para_ids": [pid], "ex_pid": pid, "parent": root_id, "root": root_id, "done": False}
    return new_id


def _resolve(pkg: _Pkg, comments: dict[str, dict[str, Any]], root_id: str, taken: set[str]) -> None:
    _comment_ex(pkg, _thread_pid(pkg, comments[root_id], taken)).set(q("done", W15), "1")
    comments[root_id]["done"] = True


def _others(doc, author: str) -> list[tuple[str, str, str]]:
    """A többi szerző követett változásainak ujjlenyomata (fajta, szerző, szöveg) — a javítás után ugyanennyi kell."""
    out = []
    for el in doc.iter(INS, DEL, MOVE_TO, MOVE_FROM):
        a = el.get(q("author")) or "?"
        if a == author:
            continue
        kind = "ins" if el.tag in (INS, MOVE_TO) else "del"
        txt = "".join((x.text or "") for x in el.iter(T, DT)) if kind == "ins" else "".join(x.text or "" for x in el.iter(DT))
        out.append((kind, a, txt))
    merged: dict[tuple[str, str], str] = defaultdict(str)
    for kind, a, txt in out:
        merged[(kind, a)] += txt
    return sorted((k, a, t) for (k, a), t in merged.items())


# ---------- 2. javítás: a hívás ----------

def _norm_items(items: Any) -> list[dict[str, Any]]:
    if isinstance(items, dict):
        items = [items]
    if isinstance(items, str):
        items = json.loads(items)
    if not isinstance(items, list) or not items:
        raise ValueError("address: a list of {\"comment\": id, \"replace\" | \"paragraph\", \"reply\", \"resolve\"}")
    return [i if isinstance(i, dict) else {"comment": i} for i in items]


def _edits_for(scan: dict[str, Any], a: tuple[int, int, int, int], item: dict[str, Any]) -> tuple[list, list[str]]:
    """→ ([(bekezdés, kezdet, vég, új szöveg)], megjegyzések) a tétel replace/paragraph kéréséből."""
    notes: list[str] = []
    edits: list[tuple[int, int, int, str]] = []
    if item.get("paragraph") is not None:
        new = unicodedata.normalize("NFC", str(item["paragraph"])).strip("\n")
        if "\n" in new:
            raise _Fail("`paragraph` must be ONE paragraph (no line breaks) — use replace for smaller edits")
        edits.append((a[0], 0, len(scan["texts"][a[0]]), new))
    reps = item.get("replace")
    if isinstance(reps, dict):
        reps = [reps]
    for rep in reps or []:
        if not isinstance(rep, dict) or "find" not in rep:
            raise _Fail("`replace` needs {\"find\": …, \"with\": …}")
        find = unicodedata.normalize("NFC", str(rep["find"]))
        new = unicodedata.normalize("NFC", str(rep.get("with", rep.get("replace", ""))))
        i, s, e, loose = _locate(scan, a, find)
        if loose:
            notes.append("matched ignoring quote/space/dash differences")
        edits.append((i, s, e, new))
    spans = sorted((i, s, e) for i, s, e, _ in edits)
    for x, y in zip(spans, spans[1:]):
        if x[0] == y[0] and y[1] < x[2]:
            raise _Fail("two replacements overlap — merge them into one")
    return edits, notes


def _address_sync(file_id: str, items: Any, author: str, dry_run: bool = False) -> dict[str, Any]:
    from app.storage import output_path, public_url, write_meta
    from app.storage.files import new_file_id
    from app.storage.naming import result_stem
    t0 = time.time()
    items = _norm_items(items)
    author = (author or "").strip() or "Studio"
    pkg = _load(file_id)
    body = _body(pkg)
    doc = pkg.xml("word/document.xml")
    comments = _comments(pkg)
    if not comments:
        raise ValueError("The document has no comments to address.")
    others_before = _others(doc, author)
    ctx = _Ctx(author, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               _max_id(doc, pkg.xml(_COMMENTS)))
    taken = {v for root in (doc, pkg.xml(_COMMENTS)) for el in root.iter() for v in [el.get(q("paraId", W14))] if v}
    results: list[dict[str, Any]] = []
    touched: set[str] = set()
    for item in items:
        cid = str(item.get("comment", item.get("id", ""))).strip()
        res: dict[str, Any] = {"comment": cid}
        results.append(res)
        if cid not in comments:
            res.update(status="failed", error=f"no comment with id {cid!r} — ids come from read=\"comments\"")
            continue
        root_id = comments[cid]["root"]
        res["comment"] = root_id
        touched.add(root_id)
        scan = _scan(body)
        a = _anchor(scan, root_id)
        wants_edit = item.get("paragraph") is not None or bool(item.get("replace"))
        try:
            if wants_edit and a is None:
                raise _Fail("this comment is not anchored in the text — reply only, or edit with `commands`")
            edits, notes = _edits_for(scan, a, item) if wants_edit else ([], [])
        except _Fail as e:
            res.update(status="failed", error=str(e), paragraph=a[0] + 1 if a else None)
            if item.get("reply"):
                res["error"] += " (reply not posted either)"
            continue
        if notes:
            res["note"] = "; ".join(notes)
        n_changes = 0
        if edits:
            res["paragraph"] = edits[0][0] + 1
            expected: dict[int, str] = {}
            snap = {i: deepcopy(scan["paras"][i]) for i, *_ in edits}
            by_para: dict[int, list] = defaultdict(list)
            for i, s, e, new in edits:
                by_para[i].append((s, e, new))
            before_added = ctx.added
            for i, ops in by_para.items():
                text = scan["texts"][i]
                for s, e, new in sorted(ops, reverse=True):
                    text = text[:s] + new + text[e:]
                expected[i] = text
                pieces = [(s + a0, s + b0, t) for s, e, new in ops for a0, b0, t in _word_ops(scan["texts"][i][s:e], new)]
                for s, e, new in sorted(pieces, reverse=True):
                    _apply(scan["paras"][i], s, e, new, ctx)
            got = {i: _ptext(scan["paras"][i]) for i in expected}
            if got != expected:                              # biztonsági háló: a redline pontosan a kért szöveget adja
                for i, old in snap.items():
                    scan["paras"][i].getparent().replace(scan["paras"][i], old)
                ctx.added = before_added
                res.update(status="failed", error="the tracked change did not produce the expected text — not applied")
                log.warning("review_loop: redline mismatch on comment %s: %r != %r", root_id, got, expected)
                continue
            n_changes = ctx.added - before_added
            res["tracked_changes"] = n_changes
            i0 = edits[0][0]
            res["paragraph_now"] = got[i0][:600] + ("…" if len(got[i0]) > 600 else "")
        if wants_edit and not n_changes and not item.get("reply") and not item.get("resolve"):
            res.update(status="failed", error="the new text is the same as the old — nothing changed")
            continue
        if not wants_edit and not item.get("reply") and not item.get("resolve"):
            res.update(status="failed", error="nothing to do — give replace, paragraph, reply or resolve")
            continue
        if wants_edit and not n_changes:
            res["note"] = "; ".join(filter(None, [res.get("note"), "the new text is the same as the old — text unchanged"]))
        if item.get("reply"):
            res["reply_id"] = _reply(pkg, body, comments, root_id, unicodedata.normalize("NFC", str(item["reply"])), ctx, taken)
        if item.get("resolve"):
            _resolve(pkg, comments, root_id, taken)
            res["resolved"] = True
        res["status"] = "fixed" if n_changes else "replied" if item.get("reply") else "resolved"
    open_left = [{"id": c["id"], "author": c["author"], "comment": c["text"][:200]}
                 for c in comments.values() if c["root"] == c["id"] and not c["done"] and c["id"] not in touched]
    ok = [r for r in results if r["status"] != "failed"]
    out: dict[str, Any] = {"source_file_id": file_id, "author": author, "results": results, "not_addressed": open_left}
    if dry_run or not ok:
        out["file_id"] = None
        return out
    pkg.put("word/document.xml", doc)
    others_after = _others(doc, author)
    out_id = new_file_id("docx")
    pkg.save(output_path(out_id))
    write_meta(out_id, source_file_id=file_id, operation="wordcraft",
               label=f"{result_stem(file_id, 'comments addressed', 'document')}.docx")
    out.update(file_id=out_id, url=public_url(out_id), size_kb=output_path(out_id).stat().st_size // 1024,
               tracked_changes_added=sum(r.get("tracked_changes", 0) for r in results),
               others_untouched=others_before == others_after, ms_elapsed=int((time.time() - t0) * 1000))
    return out


async def address(file_id: str, items: Any, author: str) -> dict[str, Any]:
    """A javítások egy hívásban + a saját ellenőrzés (check): tételenként, a nyitva maradtak, a motor újranyitja-e."""
    res = await asyncio.to_thread(_address_sync, file_id, items, author)
    findings, verified = [], []
    for r in res["results"]:
        if r["status"] == "failed":
            findings.append(f"Comment {r['comment']}: FAILED — {r.get('error')}.")
    fixed = [r for r in res["results"] if r["status"] == "fixed"]
    replied = [r for r in res["results"] if r.get("reply_id")]
    resolved = [r for r in res["results"] if r.get("resolved")]
    if fixed:
        verified.append(f"{len(fixed)} comment(s) fixed as tracked changes by {res['author']} "
                        f"({sum(r.get('tracked_changes', 0) for r in fixed)} insertions/deletions); "
                        "each paragraph reads exactly as asked.")
    if replied:
        verified.append(f"Reply posted under {len(replied)} comment(s).")
    if resolved:
        verified.append(f"{len(resolved)} thread(s) marked resolved.")
    if res["not_addressed"]:
        findings.append(f"{len(res['not_addressed'])} open comment(s) not addressed: "
                        + "; ".join(f"{c['id']} ({c['author']}: {c['comment'][:60]})" for c in res["not_addressed"][:8]) + ".")
    if res.get("file_id"):
        if res.get("others_untouched"):
            verified.append("The other reviewers' tracked changes are untouched.")
        else:
            findings.append("The other reviewers' tracked changes look different — check them in the Word editor.")
        from app.tools import wordcraft
        if wordcraft.available():
            from app.tools.crafts import existing
            try:
                info = json.loads(await wordcraft._cli("info", str(existing(res["file_id"]))))
                verified.append(f"The file opens in the Word engine ({info.get('pages', '?')} page(s), "
                                f"{info.get('comments', '?')} comments incl. replies).")
            except Exception as e:
                findings.insert(0, f"The Word engine could not open the result: {str(e)[:200]}")
                res["check"] = {"status": "failed", "findings": findings, "verified": verified}
                return res
        if any(r["status"] == "failed" for r in res["results"]):
            findings.append("Fix the failed ones with another address call on THIS file_id (the others are done).")
    else:
        findings.insert(0, "Nothing was changed, no new file.")
    status = "failed" if not res.get("file_id") or any(r["status"] == "failed" for r in res["results"]) else \
        "needs_check" if findings else "ok"
    res["check"] = {"status": status, "findings": findings, "verified": verified}
    return res


# ---------- 3. mi változott ----------

def _segments(old: str, new: str) -> list[tuple[str, str]]:
    """Szószintű szakaszok: (régi, új) — egyenlőknél a kettő ugyanaz; a csak szóközzel elválasztott változások
    egybeolvasztva („[-joghatás kiváltására alkalmatlan-]{+nem vált ki joghatást+}”, nem szavanként)."""
    a, b = _tokens(old), _tokens(new)
    segs: list[list[str]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        x, y = "".join(a[i1:i2]), "".join(b[j1:j2])
        if tag != "equal" and len(segs) >= 2 and segs[-1][0] == segs[-1][1] and not segs[-1][0].strip() \
                and segs[-2][0] != segs[-2][1]:
            gap = segs.pop()[0]
            segs[-1][0] += gap + x
            segs[-1][1] += gap + y
        elif tag != "equal" and segs and segs[-1][0] != segs[-1][1]:
            segs[-1][0] += x
            segs[-1][1] += y
        else:
            segs.append([x, y])
    return [(x, y) for x, y in segs]


def _diff_str(segs: list[tuple[str, str]], ctx_chars: int = 50) -> str:
    """[-régi-]{+új+} jelölés; a hosszú változatlan szakaszok szóhatáron rövidítve."""
    out = []
    for k, (x, y) in enumerate(segs):
        if x == y:
            if len(x) > 2 * ctx_chars + 5:
                head = x[:ctx_chars].rsplit(" ", 1)[0] if k else ""
                tail = x[-ctx_chars:].split(" ", 1)[-1] if k < len(segs) - 1 else ""
                x = f"{head} … {tail}"
            out.append(x)
            continue
        out.append((f"[-{x}-]" if x else "") + (f"{{+{y}+}}" if y else ""))
    return "".join(out).strip()


def _explained(segs: list[tuple[str, str]], revs: list[dict[str, Any]]) -> bool:
    """A bekezdés minden változott darabja megtalálható-e a követett (új vagy most lezárt) változások szövegében —
    ha nem, valaki változáskövetés NÉLKÜL is írt bele."""
    pool = " ".join(r["text"] for r in revs)
    return all(not frag.strip() or frag.strip() in pool for x, y in segs if x != y for frag in (x, y))


def _similar(x: str, y: str) -> float:
    return difflib.SequenceMatcher(None, _tokens(x), _tokens(y), autojunk=False).ratio()


def changes(file_id: str, base_file_id: str, offset: int = 0, max_chars: int = 20_000) -> dict[str, Any]:
    from app.tools.wordcraft import _page_items
    new_pkg, old_pkg = _load(file_id), _load(base_file_id)
    ns, os_ = _scan(_body(new_pkg)), _scan(_body(old_pkg))
    nt, ot = ns["texts"], os_["texts"]
    key = lambda r: (r["type"], r["author"], r["text"])
    rn, ro = _revisions(ns), _revisions(os_)
    pool = Counter(key(r) for r in ro)
    fresh: dict[int, list] = defaultdict(list)          # új bekezdés-sorszám → a most megjelent követett változások
    for r in rn:
        if pool[key(r)]:
            pool[key(r)] -= 1
        else:
            fresh[r["paragraph"]].append({k: r[k] for k in ("type", "author", "text")})
    pool = Counter(key(r) for r in rn)
    gone: dict[int, list] = defaultdict(list)           # régi sorszám → a már nem függő (elfogadott/elvetett) változások
    for r in ro:
        if pool[key(r)]:
            pool[key(r)] -= 1
        else:
            gone[r["paragraph"]].append({k: r[k] for k in ("type", "author", "text")})
    cap = lambda s: s if len(s) <= 2000 else s[:2000] + "…"
    items: list[dict[str, Any]] = []

    def pair(i: int, j: int) -> None:
        tr, st = fresh.get(j + 1, []), gone.get(i + 1, [])
        if ot[i] == nt[j] and not tr:
            return
        segs = _segments(ot[i], nt[j])
        it: dict[str, Any] = {"paragraph": j + 1, "before": cap(ot[i]), "after": cap(nt[j]), "diff": _diff_str(segs)}
        if tr:
            it["tracked"] = tr[:20]
        if st:
            it["settled"] = st[:20]
        if ot[i] != nt[j] and not _explained(segs, tr + st):
            it["untracked"] = True
        items.append(it)

    def added(jj: int) -> None:
        if nt[jj].strip():
            tr = fresh.get(jj + 1)
            items.append({"paragraph": jj + 1, "added": cap(nt[jj]), **({"tracked": tr[:20]} if tr else {"untracked": True})})

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ot, nt, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                if fresh.get(j1 + k + 1):
                    pair(i1 + k, j1 + k)
            continue
        j = j1                     # a cserélt blokkban a régi bekezdéshez a leghasonlóbb új tartozik (sorrendben)
        for i in range(i1, i2):
            best = max(range(j, j2), key=lambda jj: _similar(ot[i], nt[jj]), default=None)
            if best is not None and _similar(ot[i], nt[best]) >= 0.35:
                for jj in range(j, best):
                    added(jj)
                pair(i, best)
                j = best + 1
            elif ot[i].strip():
                it = {"removed": cap(ot[i]), "was_paragraph": i + 1}
                if gone.get(i + 1):
                    it["settled"] = gone[i + 1][:20]
                if not _explained([(ot[i], "")], gone.get(i + 1, [])):
                    it["untracked"] = True
                items.append(it)
        for jj in range(j, j2):
            added(jj)

    nc, oc = _comments(new_pkg), _comments(old_pkg)
    ck = lambda c: (c["author"], c["text"])
    pool = Counter(ck(c) for c in oc.values())
    new_threads, replies = [], []
    for c in nc.values():
        if pool[ck(c)]:
            pool[ck(c)] -= 1
            continue
        if c["root"] == c["id"]:
            a = _anchor(ns, c["id"])
            new_threads.append({"id": c["id"], "author": c["author"], "date": c["date"], "comment": c["text"],
                                "anchor": _anchor_text(ns, a) if a else None, "paragraph": a[0] + 1 if a else None})
        else:
            root = nc.get(c["root"]) or {}
            replies.append({"id": c["id"], "author": c["author"], "date": c["date"], "text": c["text"],
                            "thread": c["root"], "reply_to": (root.get("text") or "")[:160]})
    old_open = Counter(ck(c) for c in oc.values() if c["root"] == c["id"] and not c["done"])
    resolved = [{"id": c["id"], "author": c["author"], "comment": c["text"][:160]} for c in nc.values()
                if c["root"] == c["id"] and c["done"] and old_open[ck(c)]]
    pool = Counter(ck(c) for c in nc.values())
    removed = []
    for c in oc.values():
        if pool[ck(c)]:
            pool[ck(c)] -= 1
        else:
            removed.append({"author": c["author"], "comment": c["text"][:160]})
    by_author: dict[str, int] = Counter(t["author"] for v in fresh.values() for t in v)
    page, nxt = _page_items(items, offset, max_chars)
    res: dict[str, Any] = {
        "file_id": file_id, "base_file_id": base_file_id,
        "summary": {"paragraphs_changed": sum(1 for x in items if "before" in x),
                    "paragraphs_added": sum(1 for x in items if "added" in x),
                    "paragraphs_removed": sum(1 for x in items if "removed" in x),
                    "untracked_edits": sum(1 for x in items if x.get("untracked")),
                    "new_tracked_changes_by": dict(by_author),
                    "new_comments": len(new_threads), "new_replies": len(replies), "threads_resolved": len(resolved)},
        "paragraphs": page, "paragraphs_total": len(items), "offset": offset, "next_offset": nxt,
    }
    if offset == 0:
        res["comments"] = {"new": new_threads[:60], "replies": replies[:60], "resolved": resolved[:60],
                           "removed": removed[:60]}
    res["legend"] = ("diff: [-removed-]{+added+}; tracked = new tracked changes in that paragraph; untracked = "
                     "edited with track changes OFF; settled = earlier tracked changes now accepted or rejected.")
    return res


# ---------- a buborék (saját ügynök): „address the comments” ----------

_WANT = re.compile(r"comment|komment|megjegyz|Kommentar|commentaire|comentario|commento|opmerking", re.I)
_NOT = re.compile(r"\b(add|insert|delete|remove|hide|show|new comment)\b|töröl|hozzáad|szúrj|adj hozzá|új megjegyzés"
                  r"|rejt|lösch|hinzu", re.I)

_DRAFT = """You help a professional answer a reviewer's comments on their Word document. For EACH open comment below,
draft the change the reviewer asks for, and a short reply to the reviewer.
The user's instruction: {request}
Open comments (JSON; "anchor" is the highlighted text, "paragraph_text" the whole paragraph as it reads now):
{threads}
{errors}
Reply with ONE JSON object:
{{"items": [{{"comment": "<id>", "replace": {{"find": "<text copied EXACTLY from anchor or paragraph_text>", "with": "<new text>"}}, "reply": "<one short sentence: what you changed>"}}],
 "summary": "<one short sentence in the language of the instruction; no comment ids>"}}
Rules: copy "find" character for character from the paragraph (keep it short but unique in that paragraph); write the
new text in the language of the document; for a whole-paragraph rewrite use "paragraph": "<the full new paragraph>"
instead of "replace". If a comment needs facts you do not have (e.g. "check the amount in the contract") or only asks a
question, do NOT invent anything: leave out "replace" and reply briefly what the author must check. A reviewer's
explicit suggestion ("I think it is X", "rather: Y") may be applied. The reply must say exactly what you did — never
claim a change you did not make, nor deny one you made. Replies go in the language of the comment. Never change text
no comment asks about."""


def wants_address(file_id: str, request: str) -> bool:
    """„Address the comments”, „javítsd a megjegyzéseket”… egy olyan docx-en, amin van nyitott megjegyzés — de nem
    „adj hozzá egy megjegyzést” vagy „töröld őket”."""
    if not _WANT.search(request or "") or _NOT.search(request or ""):
        return False
    try:
        cs = _comments(_load(file_id))
    except Exception:
        return False
    return any(c["root"] == c["id"] and not c["done"] for c in cs.values())


async def default_author() -> str:
    """A bejelentkezett felhasználó megjelenített neve (Postgres), különben „Studio”."""
    try:
        from app import db, tenancy
        uid = tenancy.current_user_id()
        if db.enabled() and not tenancy.is_guest(uid):
            name = ((await db.user_get(uid)) or {}).get("name")
            if name and str(name).strip():
                return str(name).strip()
    except Exception as e:
        log.info("szerzőnév nem olvasható: %s", e)
    return "Studio"


async def _draft(threads: list[dict[str, Any]], request: str, errors: str = "") -> dict[str, Any]:
    from app.config import settings
    from app.llm.siliconflow import _call_model, _strip_json_fences
    slim = [{k: (t.get(k)[:1500] if isinstance(t.get(k), str) else t.get(k))
             for k in ("id", "author", "comment", "anchor", "paragraph_text", "replies") if t.get(k) is not None}
            for t in threads[:40]]
    prompt = _DRAFT.format(request=request.strip()[:1500], threads=json.dumps(slim, ensure_ascii=False),
                           errors=errors)
    last: Exception = RuntimeError("no answer")
    for model in (settings.AGENT_MODEL, settings.AGENT_MODEL_FALLBACK):
        try:
            raw = _strip_json_fences(await _call_model(model, [{"role": "user", "content": prompt}], thinking=False))
            d = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
            if isinstance(d.get("items"), list):
                return d
        except Exception as e:
            last = e
            log.warning("megjegyzés-javítás terve (%s): %s", model, e)
    raise RuntimeError(f"Could not draft the fixes: {last}")


async def draft_and_address(file_id: str, request: str, author: Optional[str] = None) -> dict[str, Any]:
    """A buborék útja: a nyitott megjegyzésekre a modell javítást + választ ír, egy próbakör kiszűri a nem talált
    find-okat (egyszer újrakérjük őket a hibával), aztán ugyanaz az address, mint az MCP-n."""
    author = author or await default_author()
    wl = await asyncio.to_thread(worklist, file_id, 0, 10 ** 7)
    threads = wl["threads"]
    if not threads:
        raise ValueError("This document has no open comments.")
    plan = await _draft(threads, request)
    items = [i for i in plan["items"] if isinstance(i, dict) and str(i.get("comment")) in {t["id"] for t in threads}]
    probe = await asyncio.to_thread(_address_sync, file_id, items, author, True)
    bad = {r["comment"]: r.get("error") for r in probe["results"] if r["status"] == "failed"}
    if bad:
        errs = "Your previous attempt failed for these comments — fix them:\n" + "\n".join(f"- {k}: {v}" for k, v in bad.items())
        retry = await _draft([t for t in threads if t["id"] in bad], request, errs)
        again = {str(i.get("comment")): i for i in retry.get("items") or [] if isinstance(i, dict)}
        items = [again.get(str(i.get("comment")), i) if str(i.get("comment")) in bad else i for i in items]
    res = await address(file_id, items, author)
    if not res.get("file_id"):
        raise RuntimeError("Could not apply any fix: " + "; ".join(r.get("error") or "" for r in res["results"])[:300])
    n = len(threads)
    fixed = sum(1 for r in res["results"] if r["status"] == "fixed")
    replied = sum(1 for r in res["results"] if r.get("reply_id"))
    failed = [r for r in res["results"] if r["status"] == "failed"]
    summary = (plan.get("summary") or "").strip()
    summary = (summary + " " if summary else "") + \
        f"({fixed}/{n} comments fixed as tracked changes by {author}, {replied} replied.)"
    if failed:
        summary += f" Not done: {', '.join(r['comment'] for r in failed)}."
    res["summary"] = summary
    return res
