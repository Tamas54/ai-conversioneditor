"""PowerPoint-csomag → Apple Keynote is megnyitja (és szabványos marad).

A PowerPoint elnéző, a Keynote nem: a python-pptx kimenetét „The file format is invalid” hibával utasítja el.
Javítások (csak ami tényleg kell, minden más bájtra ugyanaz marad):

  1. notesMasterIdLst   a python-pptx a jegyzetmestert csak kapcsolatként veszi fel, a presentation.xml-be nem
                        írja be — jegyzetes paklinál (a pptx_build a fotók forrását is a jegyzetbe teszi) a Keynote
                        elutasítja. (A handoutMaster ugyanígy.)
  2. notes clrMapOvr    jegyzetdia színleképezés nélkül → a Keynote elutasítja (pandoc #11935, Keynote 15.4).
  3. sldSz type         a 4:3-as alapsablon `type="screen4x3"`-at hagy a 16:9-es méret mellett — ellentmondás;
                        a 16:9 közelit pontosan 12192000×6858000-re igazítjuk.
  4. printerSettings    a sablon Windows nyomtató-csonkja (.bin) — kivéve, kapcsolattal, tartalomtípussal.
  5. diagram-tengely    a python-pptx diagramsablonjai negatív c:axId-t írnak (a séma 0…2^31-1) → pozitív,
                        diagramonként ütközésmentesen, a crossAx-hivatkozásokkal együtt.
  6. image/jpg          nem létező MIME-típus → image/jpeg.

A ZIP-et sorrendtartóan írjuk újra ([Content_Types].xml elöl), a médiát adatfolyamként másolva. Ha nincs mit
javítani, a fájlhoz nem nyúlunk. Hiba esetén sem bukik a mentés: a fájl marad, ahogy volt.
"""
from __future__ import annotations

import io
import logging
import os
import posixpath
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import IO, Union

from lxml import etree

log = logging.getLogger("aice.keynote")

EXTS = {"pptx", "pptm", "ppsx", "ppsm", "potx", "potm"}

_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PR = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_OFFICE_DOC = _R + "/officeDocument"
_NOTES_SLIDE_CT = "application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"
_CHART_CT = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
_PRINTER_CT = "application/vnd.openxmlformats-officedocument.presentationml.printerSettings"
# a presentation.xml gyerekeinek sémasorrendje (CT_Presentation) — az új listák a helyükre kerülnek
_PRES_ORDER = ["sldMasterIdLst", "notesMasterIdLst", "handoutMasterIdLst", "sldIdLst", "sldSz", "notesSz",
               "smartTags", "embeddedFontLst", "custShowLst", "photoAlbum", "custDataLst", "kinsoku",
               "defaultTextStyle", "modifyVerifier", "extLst"]
# sldSz type → szabványos méret (EMU); ha a valós méret más, a type ellentmond neki
_SLD_TYPES = {"screen4x3": (9144000, 6858000), "screen16x9": (9144000, 5143500), "screen16x10": (9144000, 5715000),
              "letter": (9144000, 6858000), "A4": (9906000, 6858000), "35mm": (10287000, 6858000),
              "overhead": (9144000, 6858000), "banner": (7315200, 914400), "ledger": (12179300, 9134475),
              "A3": (14400213, 10800160), "B4ISO": (10826750, 8120063), "B5ISO": (7169150, 5376863),
              "B4JIS": (11447463, 8585200), "B5JIS": (8077200, 6058000), "hagakiCard": (3600450, 5329238)}
_WIDE = (12192000, 6858000)
_AX_MAX = 2147483647          # c:axId: unsignedInt, de az Office (és a Keynote) csak 2^31-1-ig fogadja

Target = Union[str, os.PathLike, IO[bytes]]


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _xml(root) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _rels_name(part: str) -> str:
    d, f = posixpath.split(part)
    return posixpath.join(d, "_rels", f + ".rels")


def _resolve(part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(part), target))


def _plan(zf: zipfile.ZipFile) -> tuple[dict[str, bytes], set[str], list[str]]:
    """→ (átírandó részek új tartalma, törlendő részek, mit javított)."""
    names = set(zf.namelist())
    new: dict[str, bytes] = {}
    drop: set[str] = set()
    done: list[str] = []
    if "[Content_Types].xml" not in names or "_rels/.rels" not in names:
        return new, drop, done
    root_rels = etree.fromstring(zf.read("_rels/.rels"))
    pres = next((_resolve("", r.get("Target", "")) for r in root_rels.iter(_q(_PR, "Relationship"))
                 if r.get("Type") == _OFFICE_DOC), "ppt/presentation.xml")
    if pres not in names:
        return new, drop, done
    ct = etree.fromstring(zf.read("[Content_Types].xml"))
    defaults = {d.get("Extension", "").lower(): d for d in ct.iter(_q(_CT, "Default"))}
    overrides = {o.get("PartName", "").lstrip("/"): o for o in ct.iter(_q(_CT, "Override"))}

    def ctype(part: str) -> str:
        if part in overrides:
            return overrides[part].get("ContentType", "")
        d = defaults.get(part.rsplit(".", 1)[-1].lower())
        return d.get("ContentType", "") if d is not None else ""

    ct_changed = False
    # 6. image/jpg → image/jpeg
    for el in list(defaults.values()) + list(overrides.values()):
        if el.get("ContentType", "").lower() in ("image/jpg", "image/pjpeg"):
            el.set("ContentType", "image/jpeg")
            ct_changed = True
            done.append("image/jpg content type")

    # 4. nyomtató-csonk ki
    prels_name = _rels_name(pres)
    prels = etree.fromstring(zf.read(prels_name)) if prels_name in names else None
    prels_changed = False
    if prels is not None:
        for r in list(prels.iter(_q(_PR, "Relationship"))):
            if r.get("Type", "").endswith("/printerSettings") and r.get("TargetMode") != "External":
                part = _resolve(pres, r.get("Target", ""))
                prels.remove(r)
                prels_changed = True
                drop.add(part)
                drop.add(_rels_name(part))
                if part in overrides:
                    ct.remove(overrides.pop(part))
                    ct_changed = True
        if drop:
            done.append("printer settings stub")
            left = [n for n in names - drop if n.lower().endswith(".bin")]
            d = defaults.get("bin")
            if d is not None and d.get("ContentType") == _PRINTER_CT and not left:
                ct.remove(d)
                ct_changed = True

    # 1. + 3. presentation.xml
    proot = etree.fromstring(zf.read(pres))
    pres_changed = False
    if prels is not None:
        for kind, lst, item in (("notesMaster", "notesMasterIdLst", "notesMasterId"),
                                ("handoutMaster", "handoutMasterIdLst", "handoutMasterId")):
            rid = next((r.get("Id") for r in prels.iter(_q(_PR, "Relationship"))
                        if r.get("Type", "").endswith("/" + kind)
                        and _resolve(pres, r.get("Target", "")) in names), None)
            if rid and proot.find(_q(_P, lst)) is None:
                el = etree.Element(_q(_P, lst))
                etree.SubElement(el, _q(_P, item)).set(_q(_R, "id"), rid)
                pos = _PRES_ORDER.index(lst)
                after = [c for c in proot if etree.QName(c).localname in _PRES_ORDER[:pos]]
                if after:
                    after[-1].addnext(el)
                else:
                    proot.insert(0, el)
                pres_changed = True
                done.append(f"{lst} registered")
    sz = proot.find(_q(_P, "sldSz"))
    if sz is not None:
        try:
            cx, cy = int(sz.get("cx", "0")), int(sz.get("cy", "0"))
        except ValueError:
            cx = cy = 0
        if cx and abs(cx - _WIDE[0]) <= 2000 and abs(cy - _WIDE[1]) <= 2000 and (cx, cy) != _WIDE:
            sz.set("cx", str(_WIDE[0]))
            sz.set("cy", str(_WIDE[1]))
            pres_changed = True
            done.append("16:9 slide size snapped")
            cx, cy = _WIDE
        typ = sz.get("type")
        if typ and typ != "custom":
            want = _SLD_TYPES.get(typ)
            if want is None or abs(want[0] - cx) > want[0] // 100 or abs(want[1] - cy) > want[1] // 100:
                del sz.attrib["type"]
                pres_changed = True
                done.append(f"slide size type {typ} removed")
    if pres_changed:
        new[pres] = _xml(proot)
    if prels_changed:
        new[prels_name] = _xml(prels)

    # 2. + 5. jegyzetdiák és diagramok
    notes_fixed = charts_fixed = 0
    for n in sorted(names - drop):
        if not n.endswith(".xml") or not n.startswith(posixpath.dirname(pres)):
            continue
        typ = ctype(n)
        if typ == _NOTES_SLIDE_CT:
            root = etree.fromstring(zf.read(n))
            if root.find(_q(_P, "clrMapOvr")) is None:
                cmo = etree.Element(_q(_P, "clrMapOvr"))
                etree.SubElement(cmo, _q(_A, "masterClrMapping"))
                csld = root.find(_q(_P, "cSld"))
                if csld is not None:
                    csld.addnext(cmo)
                else:
                    root.append(cmo)
                new[n] = _xml(root)
                notes_fixed += 1
        elif typ == _CHART_CT:
            raw = zf.read(n)
            if b'val="-' not in raw:
                continue
            root = etree.fromstring(raw)
            els = [el for tag in ("axId", "crossAx") for el in root.iter(_q(_C, tag))]
            used = {el.get("val") for el in els}
            remap: dict[str, str] = {}
            for el in els:
                v = el.get("val", "")
                if not (v.startswith("-") and v[1:].isdigit()):
                    continue
                if v not in remap:      # diagramon belül egyértelmű, a hivatkozások (crossAx) ugyanazt kapják
                    k = abs(int(v)) % _AX_MAX or 1
                    while str(k) in used:
                        k = k % _AX_MAX + 1
                    remap[v] = str(k)
                    used.add(str(k))
                el.set("val", remap[v])
            if remap:
                new[n] = _xml(root)
                charts_fixed += 1
    if notes_fixed:
        done.append(f"color map on {notes_fixed} notes slide(s)")
    if charts_fixed:
        done.append(f"axis ids in {charts_fixed} chart(s)")
    if ct_changed:
        new["[Content_Types].xml"] = _xml(ct)
    return new, drop, done


def _rewrite(zf: zipfile.ZipFile, out: IO[bytes], new: dict[str, bytes], drop: set[str]) -> None:
    infos = [i for i in zf.infolist() if i.filename not in drop]
    infos.sort(key=lambda i: i.filename != "[Content_Types].xml")     # stabil: a többi sorrendje marad
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zo:
        for i in infos:
            ni = zipfile.ZipInfo(i.filename, date_time=i.date_time)
            ni.compress_type = i.compress_type if i.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) \
                else zipfile.ZIP_DEFLATED
            ni.external_attr = i.external_attr
            if i.filename in new:
                zo.writestr(ni, new[i.filename])
            else:
                with zf.open(i) as src, zo.open(ni, "w", force_zip64=i.file_size > 0x7FFFFFFF) as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)


def fix(target: Target) -> list[str]:
    """Egy .pptx (útvonal vagy írható/olvasható bájtfolyam) Keynote-biztossá tétele, helyben. → mit javított."""
    try:
        if isinstance(target, (str, os.PathLike)):
            p = Path(target)
            with zipfile.ZipFile(p) as zf:
                new, drop, done = _plan(zf)
                if not done:
                    return []
                fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".keynote-", suffix=".pptx")
                try:
                    with os.fdopen(fd, "wb") as fh:
                        _rewrite(zf, fh, new, drop)
                    os.replace(tmp, p)
                except BaseException:
                    Path(tmp).unlink(missing_ok=True)
                    raise
            return done
        stream = target
        stream.seek(0)
        data = stream.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            new, drop, done = _plan(zf)
            if not done:
                return []
            buf = io.BytesIO()
            _rewrite(zf, buf, new, drop)
        stream.seek(0)
        stream.truncate()
        stream.write(buf.getvalue())
        return done
    except Exception as e:      # a javítás kényelmi — a kész paklit nem buktatja
        log.warning("keynote-safe pass skipped: %s", e)
        return []


def fix_if_slides(path: Union[str, os.PathLike]) -> list[str]:
    """Bármilyen kimenetre hívható: csak a PowerPoint-csomagokhoz nyúl."""
    return fix(path) if Path(path).suffix.lower().lstrip(".") in EXTS else []


def install() -> None:
    """A python-pptx minden mentése (pptx_build, sablon, ügynök, videós dia, szerkesztések) Keynote-biztos legyen."""
    from pptx.presentation import Presentation
    if getattr(Presentation.save, "_keynote_safe", False):
        return
    orig = Presentation.save

    def save(self, file):
        orig(self, file)
        if isinstance(file, (str, os.PathLike)) or all(hasattr(file, a) for a in ("seek", "read", "write", "truncate")):
            fix(file)

    save._keynote_safe = True       # type: ignore[attr-defined]
    save.__doc__ = orig.__doc__
    Presentation.save = save        # type: ignore[method-assign]
