"""A Word kiegészítő megjegyzés-részei egy motor (WordCraft) mentése után is maradjanak meg.

A WordCraft a szálakat (commentsExtended) és a lezárást megtartja, de a Word 2016+ részeit eldobja:
    commentsIds          tartós megjegyzés-azonosító (durableId) — a modern megjegyzések identitása
    commentsExtensible   UTC-időbélyeg a tartós azonosítóhoz
    people               a szerzők (Word-fiók, megjelenítés)
A bíráló (aki Wordben dolgozik) így pontosan azt kapja vissza, amit küldött. Ha a forrásban megvoltak, a kimenetbe
visszakerülnek, a kimenet saját bekezdés-azonosítóira átírva (a megjegyzéseket azonosító, ennek híján szerző + szöveg
szerint párosítva). Hiba esetén a fájl marad, ahogy a motor írta.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import zipfile
from pathlib import Path

from lxml import etree

log = logging.getLogger("aice.docx_extras")

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
W16CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
W16CEX = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
PARTS = {
    "word/commentsIds.xml": ("application/vnd.openxmlformats-officedocument.wordprocessingml.commentsIds+xml",
                             "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds"),
    "word/commentsExtensible.xml": ("application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtensible+xml",
                                    "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible"),
    "word/people.xml": ("application/vnd.openxmlformats-officedocument.wordprocessingml.people+xml",
                        "http://schemas.microsoft.com/office/2011/relationships/people"),
}


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _comment_pids(xml: bytes) -> dict[str, tuple[str, str]]:
    """megjegyzés-id → (az utolsó bekezdés w14:paraId-je, szerző + szöveg kulcs)."""
    root = etree.fromstring(xml)
    out = {}
    for c in root.iter(_q(W, "comment")):
        ps = [p for p in c.iter(_q(W, "p")) if p.get(_q(W14, "paraId"))]
        if not ps:
            continue
        text = "".join(t.text or "" for t in c.iter(_q(W, "t")))
        out[c.get(_q(W, "id"))] = (ps[-1].get(_q(W14, "paraId")), f"{c.get(_q(W, 'author'))}\x00{text}")
    return out


def restore(src: Path, out: Path) -> list[str]:
    """→ a visszaállított részek nevei (üres: nem kellett / nem lehetett)."""
    try:
        with zipfile.ZipFile(src) as zs:
            have = set(zs.namelist())
            missing_src = [n for n in PARTS if n in have]
            if not missing_src or "word/comments.xml" not in have:
                return []
            src_parts = {n: zs.read(n) for n in missing_src}
            src_comments = _comment_pids(zs.read("word/comments.xml"))
        with zipfile.ZipFile(out) as zo:
            names = zo.namelist()
            todo = [n for n in missing_src if n not in names]
            if not todo or "word/comments.xml" not in names:
                return []
            out_comments = _comment_pids(zo.read("word/comments.xml"))
            ct = etree.fromstring(zo.read("[Content_Types].xml"))
            rels = etree.fromstring(zo.read("word/_rels/document.xml.rels"))
            infos = zo.infolist()
            data = {i.filename: zo.read(i.filename) for i in infos}
        by_key = {v[1]: v[0] for v in out_comments.values()}
        pid_map = {}
        for cid, (pid, key) in src_comments.items():
            new = out_comments.get(cid, (None, None))
            if new[1] == key:
                pid_map[pid] = new[0]
            elif key in by_key:
                pid_map[pid] = by_key[key]
        kept_durables: set[str] = set()
        new_parts: dict[str, bytes] = {}
        if "word/commentsIds.xml" in todo:
            root = etree.fromstring(src_parts["word/commentsIds.xml"])
            for e in list(root.iter(_q(W16CID, "commentId"))):
                pid = pid_map.get(e.get(_q(W16CID, "paraId")))
                if pid is None:
                    root.remove(e)
                    continue
                e.set(_q(W16CID, "paraId"), pid)
                kept_durables.add(e.get(_q(W16CID, "durableId")))
            new_parts["word/commentsIds.xml"] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        if "word/commentsExtensible.xml" in todo:
            root = etree.fromstring(src_parts["word/commentsExtensible.xml"])
            if kept_durables:
                for e in list(root.iter(_q(W16CEX, "commentExtensible"))):
                    if e.get(_q(W16CEX, "durableId")) not in kept_durables:
                        root.remove(e)
            new_parts["word/commentsExtensible.xml"] = etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                                                                       standalone=True)
        if "word/people.xml" in todo:
            new_parts["word/people.xml"] = src_parts["word/people.xml"]
        ids = [int(m.group(1)) for x in rels for m in [re.match(r"rId(\d+)$", x.get("Id") or "")] if m]
        nxt = max(ids or [0])
        for name in new_parts:
            ctype, rtype = PARTS[name]
            if not any(o.get("PartName") == "/" + name for o in ct):
                o = etree.SubElement(ct, _q(CT, "Override"))
                o.set("PartName", "/" + name)
                o.set("ContentType", ctype)
            if not any(r.get("Type") == rtype for r in rels):
                nxt += 1
                r = etree.SubElement(rels, _q(PR, "Relationship"))
                r.set("Id", f"rId{nxt}")
                r.set("Type", rtype)
                r.set("Target", name.split("/", 1)[1])
        data["[Content_Types].xml"] = etree.tostring(ct, xml_declaration=True, encoding="UTF-8", standalone=True)
        data["word/_rels/document.xml.rels"] = etree.tostring(rels, xml_declaration=True, encoding="UTF-8", standalone=True)
        fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=".extras-", suffix=".docx")
        try:
            with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as z:
                for i in infos:
                    z.writestr(zipfile.ZipInfo(i.filename, date_time=i.date_time), data[i.filename], zipfile.ZIP_DEFLATED)
                for name, b in new_parts.items():
                    z.writestr(name, b)
            os.replace(tmp, out)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return sorted(new_parts)
    except Exception as e:      # kényelmi helyreállítás — a motor kimenetét nem buktatja
        log.warning("docx extras not restored for %s: %s", Path(out).name, e)
        return []
