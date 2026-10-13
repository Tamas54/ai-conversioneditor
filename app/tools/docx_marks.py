"""Pontos helyre tett jelölések egy DOCX-ben, a dokumentum XML-jén: idézet megkeresése, megjegyzés, követett csere,
könyvjelző, követett bekezdés-beszúrás és -törlés. Ugyanazok az elemek, amiket a Word és a WordCraft ír
(w:ins / w:del / commentRangeStart…End / bookmarkStart…End) — a szerkesztő megjegyzés-sávja és az Accept/Reject
gombjai ugyanúgy kezelik, mintha ember javasolta volna.

A bekezdések sorrendje = a törzs minden w:p-je dokumentumsorrendben (táblázatcellák és szövegdobozok is, az
mc:Fallback másolat nélkül). Az ellenőrzők ugyanezt a listát látják (`texts()`), így a „¶ 12” mindkét oldalon ugyanaz.

Miért nem a WordCraft parancsaival (select.text + edit.pasteText)? A könyvjelzőre ugrás (edit.goto bookmark) csak a
kurzort teszi oda, a mezők eredményében a select.text nem talál, és egy el nem talált idézet az egész futást leállítja
— itt bekezdésre pontosan, darabonként dől el, mi ment be és mi nem (mérve 10-14).
"""
from __future__ import annotations

import re
import time
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
COMMENTS_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
COMMENTS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"


def w(tag: str) -> str:
    return f"{{{W}}}{tag}"


_QUOTES = str.maketrans({"’": "'", "‘": "'", "‚": "'", "“": '"', "”": '"', "„": '"', "»": '"', "«": '"',
                         "–": "-", "—": "-", "‑": "-", " ": " ", " ": " ", " ": " "})


def _norm_map(s: str) -> tuple[str, list[int]]:
    """Kis eltérésekre tűrő alak (idézőjel, gondolatjel, nem törő szóköz, több szóköz) + visszamutató indexek."""
    out, idx, prev_space = [], [], False
    for i, ch in enumerate(s.translate(_QUOTES)):
        if ch.isspace():
            if prev_space:
                continue
            ch, prev_space = " ", True
        else:
            prev_space = False
        out.append(ch.lower())
        idx.append(i)
    return "".join(out), idx


class RevisionError(ValueError):
    """Egy jelölés nem tehető be (az idézet nincs meg, vagy már követett változásban áll) — a többi ettől még megy."""


class Docx:
    def __init__(self, path: Path, author: str = "Studio", date: Optional[str] = None):
        self.path = Path(path)
        with zipfile.ZipFile(self.path) as z:
            self.infos = z.infolist()
            self.parts = {i.filename: z.read(i.filename) for i in self.infos}
        self.root = etree.fromstring(self.parts["word/document.xml"])
        self.body = self.root.find(w("body"))
        self.author = author
        self.date = date or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._comments = etree.fromstring(self.parts["word/comments.xml"]) if "word/comments.xml" in self.parts else None
        ids = [int(x) for x in self.root.xpath("//w:ins/@w:id | //w:del/@w:id | //w:bookmarkStart/@w:id", namespaces={"w": W})
               if str(x).isdigit()]
        self._next_id = max(ids, default=0) + 100
        self._paras: Optional[list] = None

    # ---------- olvasás ----------

    def paragraphs(self) -> list:
        if self._paras is None:
            self._paras = [p for p in self.body.iter(w("p")) if not any(a.tag == f"{{{MC}}}Fallback" for a in p.iterancestors())]
        return self._paras

    def _own(self, p, el) -> bool:
        """Az elem ennek a bekezdésnek a sajátja-e (nem egy benne ülő szövegdoboz bekezdéséé)."""
        for a in el.iterancestors(w("p")):
            return a is p
        return False

    def segments(self, p) -> list[tuple[Any, int, str]]:
        """[(w:t, kezdő karakter, szöveg)] — a bekezdés látható szövege darabokban (törölt szöveg nélkül)."""
        out, pos = [], 0
        for t in p.iter(w("t")):
            if not self._own(p, t):
                continue
            s = t.text or ""
            out.append((t, pos, s))
            pos += len(s)
        return out

    def text(self, p) -> str:
        return "".join(s for _, _, s in self.segments(p))

    def texts(self) -> list[str]:
        return [self.text(p) for p in self.paragraphs()]

    def style(self, p) -> str:
        ps = p.find(f"{w('pPr')}/{w('pStyle')}")
        return ps.get(w("val")) if ps is not None else ""

    # ---------- keresés ----------

    def find(self, quote: str, hint: Optional[int] = None) -> Optional[tuple[int, int, int]]:
        """Az idézet helye: (bekezdés, kezdet, vég). Előbb a megadott bekezdésben, aztán a hozzá legközelebbiben; pontos
        egyezés, ennek híján idézőjel/gondolatjel/szóköz-tűrő egyezés."""
        quote = (quote or "").strip()
        if not quote:
            return None
        texts = self.texts()
        order = list(range(len(texts)))
        if hint is not None and 0 <= hint < len(texts):
            order.sort(key=lambda i: abs(i - hint))
        for i in order:
            k = texts[i].find(quote)
            if k >= 0:
                return i, k, k + len(quote)
        nq, _ = _norm_map(quote)
        for i in order:
            nt, idx = _norm_map(texts[i])
            k = nt.find(nq)
            if k >= 0:
                end = idx[k + len(nq) - 1] + 1
                return i, idx[k], end
        return None

    # ---------- darabolás ----------

    def _split(self, t, k: int) -> None:
        """A w:t-t tartalmazó futást k. karakternél kettévágja (a formázás mindkét felén ugyanaz)."""
        r = t.getparent()
        if r is None or r.tag != w("r") or not 0 < k < len(t.text or ""):
            return
        new_r = etree.Element(w("r"), dict(r.attrib))
        rpr = r.find(w("rPr"))
        if rpr is not None:
            new_r.append(deepcopy(rpr))
        kids = list(r)
        t2 = etree.SubElement(new_r, w("t"))
        t2.text, t2.attrib[XML_SPACE] = t.text[k:], "preserve"
        for c in kids[kids.index(t) + 1:]:
            new_r.append(c)
        t.text = t.text[:k]
        t.set(XML_SPACE, "preserve")
        r.addnext(new_r)

    def _cut(self, p, pos: int) -> None:
        for t, a, s in self.segments(p):
            if a < pos < a + len(s):
                self._split(t, pos - a)
                return

    def runs(self, p, start: int, end: int) -> list:
        """A [start, end) szakasz futásai — előtte a határokon kettévágva, hogy a szakasz egész futásokból álljon."""
        self._cut(p, end)
        self._cut(p, start)
        out = []
        for t, a, s in self.segments(p):
            if s and a >= start and a + len(s) <= end:
                r = t.getparent()
                if r.tag == w("r") and (not out or out[-1] is not r):
                    out.append(r)
        return out

    def _rev(self, tag: str):
        el = etree.Element(w(tag))
        el.set(w("id"), str(self._next_id))
        el.set(w("author"), self.author)
        el.set(w("date"), self.date)
        self._next_id += 1
        return el

    # ---------- jelölések ----------

    def replace(self, p, start: int, end: int, new: str) -> None:
        """Követett csere: a régi szöveg törlésként, az új beszúrásként (a régi első futásának formázásával)."""
        runs = self.runs(p, start, end) if end > start else []
        if end > start and not runs:
            raise RevisionError("that text isn't plain text here (a field or a picture)")
        for r in runs:
            if r.getparent().tag in (w("ins"), w("del")) or any(a.tag in (w("ins"), w("del")) for a in r.iterancestors()):
                raise RevisionError("that text is already part of a tracked change — accept or reject it first")
        ins = self._rev("ins")
        nr = etree.SubElement(ins, w("r"))
        src = runs[0] if runs else None
        if src is None:                         # puszta beszúrás: a beszúrás helye előtti futás formázása
            for t, a, s in self.segments(p):
                if a < start <= a + len(s):
                    src = t.getparent()
        if src is not None and src.find(w("rPr")) is not None:
            nr.append(deepcopy(src.find(w("rPr"))))
        nt = etree.SubElement(nr, w("t"))
        nt.text, nt.attrib[XML_SPACE] = new, "preserve"
        if not runs:
            self._cut(p, start)
            after = next((t.getparent() for t, a, s in self.segments(p) if a + len(s) == start), None)
            if after is not None:
                after.addnext(ins)
            else:
                first = next((t.getparent() for t, a, s in self.segments(p)), None)
                (first.addprevious(ins) if first is not None else p.append(ins))
            return
        last = None
        group: list = []
        for r in runs + [None]:                 # egy w:del szülőnként (hivatkozásban ülő futás külön)
            if r is not None and (not group or r.getparent() is group[0].getparent()):
                group.append(r)
                continue
            d = self._rev("del")
            group[0].addprevious(d)
            for g in group:
                for t in g.iter(w("t")):
                    t.tag = w("delText")
                for t in g.iter(w("instrText")):
                    t.tag = w("delInstrText")
                d.append(g)
            last = d
            group = [r] if r is not None else []
        if new:
            last.addnext(ins)

    def comment(self, p, start: int, end: int, text: str, author: Optional[str] = None) -> int:
        """Megjegyzés az idézetre (a szerkesztő margóján a buborék pontosan ezt a szakaszt jelöli)."""
        runs = self.runs(p, start, end)
        if not runs:
            raise RevisionError("that text isn't plain text here (a field or a picture)")
        if self._comments is None:
            self._comments = etree.Element(w("comments"), nsmap={"w": W})
        cid = max([int(c.get(w("id")) or 0) for c in self._comments.findall(w("comment"))], default=-1) + 1
        c = etree.SubElement(self._comments, w("comment"))
        c.set(w("id"), str(cid))
        c.set(w("author"), author or self.author)
        c.set(w("date"), self.date)
        c.set(w("initials"), "".join(x[0] for x in (author or self.author).split()[:2]).upper() or "S")
        for line in (text or "").split("\n"):
            cp = etree.SubElement(c, w("p"))
            cr = etree.SubElement(cp, w("r"))
            ct = etree.SubElement(cr, w("t"))
            ct.text, ct.attrib[XML_SPACE] = line, "preserve"
        rs = etree.Element(w("commentRangeStart"))
        rs.set(w("id"), str(cid))
        re_ = etree.Element(w("commentRangeEnd"))
        re_.set(w("id"), str(cid))
        ref = etree.Element(w("r"))
        rpr = etree.SubElement(ref, w("rPr"))
        etree.SubElement(rpr, w("rStyle")).set(w("val"), "CommentReference")
        etree.SubElement(ref, w("commentReference")).set(w("id"), str(cid))
        runs[0].addprevious(rs)
        runs[-1].addnext(re_)
        re_.addnext(ref)
        return cid

    def bookmark(self, p, start: int, end: int, name: str) -> None:
        runs = self.runs(p, start, end)
        if not runs:
            raise RevisionError("that text isn't plain text here (a field or a picture)")
        bid = str(self._next_id)
        self._next_id += 1
        bs = etree.Element(w("bookmarkStart"))
        bs.set(w("id"), bid)
        bs.set(w("name"), name)
        be = etree.Element(w("bookmarkEnd"))
        be.set(w("id"), bid)
        runs[0].addprevious(bs)
        runs[-1].addnext(be)

    def bookmarks(self, prefix: str = "") -> dict[str, tuple[int, int, int]]:
        """{név: (bekezdés, kezdet, vég)} — az egy bekezdésen belüli könyvjelzők; a vég a látható szövegben (a törölt
        szöveg nem számít, így egy elfogadatlan csere után az új érték van benne)."""
        out: dict[str, tuple[int, int, int]] = {}
        for i, p in enumerate(self.paragraphs()):
            pos = 0
            open_: dict[str, tuple[str, int]] = {}
            for el in p.iter(w("t"), w("bookmarkStart"), w("bookmarkEnd")):
                if not self._own(p, el):
                    continue
                if el.tag == w("t"):
                    pos += len(el.text or "")
                elif el.tag == w("bookmarkStart"):
                    name = el.get(w("name")) or ""
                    if name.startswith(prefix):
                        open_[el.get(w("id"))] = (name, pos)
                elif el.get(w("id")) in open_:
                    name, a = open_.pop(el.get(w("id")))
                    out[name] = (i, a, pos)
        return out

    def insert_paragraph_after(self, anchor, text: str, like=None):
        """Követett új bekezdés az anchor után (a `like` bekezdés stílusával és első futásának formázásával)."""
        like = like if like is not None else anchor
        np_ = etree.Element(w("p"))
        ppr = like.find(w("pPr"))
        ppr = deepcopy(ppr) if ppr is not None else etree.Element(w("pPr"))
        for x in ppr.findall(w("sectPr")) + ppr.findall(w("pPrChange")):
            ppr.remove(x)
        rpr = ppr.find(w("rPr"))
        if rpr is None:
            rpr = etree.SubElement(ppr, w("rPr"))
        for x in rpr.findall(w("ins")) + rpr.findall(w("del")):
            rpr.remove(x)
        rpr.insert(0, self._rev("ins"))          # a bekezdésjel is beszúrás
        np_.append(ppr)
        ins = self._rev("ins")
        r = etree.SubElement(ins, w("r"))
        first = next((t.getparent() for t, _, s in self.segments(like) if s), None)
        if first is not None and first.find(w("rPr")) is not None:
            r.append(deepcopy(first.find(w("rPr"))))
        t = etree.SubElement(r, w("t"))
        t.text, t.attrib[XML_SPACE] = text, "preserve"
        np_.append(ins)
        anchor.addnext(np_)
        self._paras = None
        return np_

    def delete_paragraph(self, p) -> None:
        """Követett törlés: a szöveg és a bekezdésjel is törlésként (elfogadáskor a bekezdés eltűnik)."""
        segs = self.segments(p)
        if segs:
            self.replace(p, 0, segs[-1][1] + len(segs[-1][2]), "")
        ppr = p.find(w("pPr"))
        if ppr is None:
            ppr = etree.Element(w("pPr"))
            p.insert(0, ppr)
        rpr = ppr.find(w("rPr"))
        if rpr is None:
            rpr = etree.Element(w("rPr"))
            anchor = ppr.find(w("sectPr")) if ppr.find(w("sectPr")) is not None else ppr.find(w("pPrChange"))
            (anchor.addprevious(rpr) if anchor is not None else ppr.append(rpr))
        rpr.insert(0, self._rev("del"))

    # ---------- mentés ----------

    def save(self, out: Path) -> None:
        self.parts["word/document.xml"] = etree.tostring(self.root, xml_declaration=True, encoding="UTF-8", standalone=True)
        names = [i.filename for i in self.infos]
        if self._comments is not None:
            self.parts["word/comments.xml"] = etree.tostring(self._comments, xml_declaration=True, encoding="UTF-8", standalone=True)
            if "word/comments.xml" not in names:
                names.append("word/comments.xml")
                ct = etree.fromstring(self.parts["[Content_Types].xml"])
                if not any(o.get("PartName") == "/word/comments.xml" for o in ct):
                    o = etree.SubElement(ct, f"{{{CT}}}Override")
                    o.set("PartName", "/word/comments.xml")
                    o.set("ContentType", COMMENTS_CT)
                self.parts["[Content_Types].xml"] = etree.tostring(ct, xml_declaration=True, encoding="UTF-8", standalone=True)
                rels_name = "word/_rels/document.xml.rels"
                rels = etree.fromstring(self.parts[rels_name])
                if not any(r.get("Type") == COMMENTS_REL for r in rels):
                    n = max([int(m.group(1)) for r in rels if (m := re.match(r"rId(\d+)$", r.get("Id", "")))], default=0)
                    r = etree.SubElement(rels, f"{{{PR}}}Relationship")
                    r.set("Id", f"rId{n + 1}")
                    r.set("Type", COMMENTS_REL)
                    r.set("Target", "comments.xml")
                self.parts[rels_name] = etree.tostring(rels, xml_declaration=True, encoding="UTF-8", standalone=True)
        tmp = Path(str(out) + ".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for n in names:
                z.writestr(n, self.parts[n])
        tmp.replace(out)


def accept_all_text(path: Path) -> list[str]:
    """A bekezdések szövege úgy, ahogy minden követett változás elfogadása után lesz (teszthez és összevetéshez)."""
    d = Docx(path)
    out = []
    for p in d.paragraphs():
        rpr_del = p.find(f"{w('pPr')}/{w('rPr')}/{w('del')}")
        if rpr_del is not None and not d.text(p):
            continue
        out.append(d.text(p))
    return out
