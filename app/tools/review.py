"""Lektorálási összefoglaló egy DOCX-ből: követett változások és megjegyzések, tömören (pár száz token).

    {"revisions": [{type: ins|del|format, author, date, text, context}], "comments": [{id, author, date,
     text, anchor}], "by_author": {név: {ins, del, format, comments}}, "truncated": bool}

A szöveget közvetlenül az OOXML-ből olvassa (word/document.xml, word/comments.xml), így bármely
szerkesztő mentését érti (WordCraft, Word, LibreOffice).
"""
from __future__ import annotations

import zipfile
from collections import defaultdict
from typing import Any

from lxml import etree

from app.tools.crafts import existing

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
_MAX_ITEMS = 150
_CTX = 90


def _q(tag: str) -> str:
    return f"{{{W}}}{tag}"


def _text(el) -> str:
    return "".join(t.text or "" for t in el.iter(_q("t"), _q("delText")))


def _para_text(p) -> str:
    # a bekezdés „végleges” szövege (törlések nélkül) — kontextusnak
    out = []
    for t in p.iter(_q("t")):
        if not any(a.tag == _q("del") for a in t.iterancestors()):
            out.append(t.text or "")
    return "".join(out)


def summary(file_id: str) -> dict[str, Any]:
    path = existing(file_id)
    if not path.suffix.lower() in (".docx", ".docm", ".dotx"):
        raise ValueError("read=review DOCX-re működik")
    with zipfile.ZipFile(path) as z:
        doc = etree.fromstring(z.read("word/document.xml"))
        comments_xml = z.read("word/comments.xml") if "word/comments.xml" in z.namelist() else None

    revisions: list[dict[str, Any]] = []
    by_author: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for el in doc.iter(_q("ins"), _q("del"), _q("rPrChange"), _q("pPrChange")):
        kind = {"ins": "ins", "del": "del"}.get(etree.QName(el).localname, "format")
        author = el.get(_q("author")) or "?"
        by_author[author][kind] += 1
        if len(revisions) >= _MAX_ITEMS:
            continue
        p = next((a for a in el.iterancestors() if a.tag == _q("p")), None)
        ctx = _para_text(p) if p is not None else ""
        txt = _text(el) if kind != "format" else _text(el.getparent()) if el.getparent() is not None else ""
        revisions.append({"type": kind, "author": author, "date": el.get(_q("date")), "text": txt[:200],
                          "context": (ctx[:_CTX] + "…") if len(ctx) > _CTX else ctx})

    # megjegyzések + a horgony szövege (commentRangeStart … commentRangeEnd)
    anchors: dict[str, list[str]] = defaultdict(list)
    open_ids: set[str] = set()
    for el in doc.iter():
        if el.tag == _q("commentRangeStart"):
            open_ids.add(el.get(_q("id")))
        elif el.tag == _q("commentRangeEnd"):
            open_ids.discard(el.get(_q("id")))
        elif el.tag == _q("t") and open_ids:
            for cid in open_ids:
                anchors[cid].append(el.text or "")
    comments = []
    if comments_xml:
        for c in etree.fromstring(comments_xml).iter(_q("comment")):
            cid, author = c.get(_q("id")), c.get(_q("author")) or "?"
            by_author[author]["comments"] += 1
            if len(comments) < _MAX_ITEMS:
                anchor = "".join(anchors.get(cid, []))
                comments.append({"id": cid, "author": author, "date": c.get(_q("date")),
                                 "text": _text(c).strip()[:400], "anchor": anchor[:120]})
    total = sum(sum(v.values()) for v in by_author.values())
    return {"file_id": file_id, "revisions": revisions, "comments": comments,
            "by_author": {a: dict(v) for a, v in by_author.items()},
            "truncated": len(revisions) + len(comments) < total}
