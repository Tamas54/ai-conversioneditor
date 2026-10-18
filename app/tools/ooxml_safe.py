"""Word/Excel-csomag → sémahelyes (az iPhone Pages/Numbers/Quick Look és a Word szigorú olvasói is szeressék).

A motorjaink egy része (pdf2docx, a pandoc referencia-sablonja, openpyxl) olyan XML-t ír, amit a Word elnéz,
de a séma nem: rossz sorrendű tulajdonság-elemek (w:b a w:sz után…), „#3366CC” szín, „16.0” egész helyett,
w:shd típus nélkül, rövid w:nsid. Itt csak ezeket javítjuk — a tartalomhoz nem nyúlunk:

  sorrend   pPr, rPr, style, sectPr, settings, tblPr, tcPr, trPr, szegélyek, számozási szint — a séma
            sorrendje szerint (ugyanaz, amit a python-docx/Word használ); az ismeretlen (bővítmény) elemek
            az előttük álló ismert elem után maradnak
  értékek   w:color/w:fill „#RRGGBB” → „RRGGBB”; „16.0” → „16”; w:shd val nélkül → clear; w:nsid/w:tmpl 8 jegyre;
            w:rFonts-ban CSS-betűlista („Aptos;Calibri;Segoe UI;Arial;sans-serif” — a LibreOffice HTML-importja) →
            az első család (a séma legfeljebb 31 karaktert enged)
  xlsx      a styles.xml betűtípus-elemei az Excel sorrendjében

Ha nincs mit javítani, a fájlhoz nem nyúlunk; hiba esetén a kimenet marad, ahogy volt.
"""
from __future__ import annotations

import io
import logging
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Union

from lxml import etree

log = logging.getLogger("aice.ooxml")

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
X = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOCX_EXTS = {"docx", "docm", "dotx", "dotm"}
XLSX_EXTS = {"xlsx", "xlsm", "xltx", "xltm"}

_SEQ_W = {
    "pPr": "pStyle keepNext keepLines pageBreakBefore framePr widowControl numPr suppressLineNumbers pBdr shd tabs "
           "suppressAutoHyphens kinsoku wordWrap overflowPunct topLinePunct autoSpaceDE autoSpaceDN bidi adjustRightInd "
           "snapToGrid spacing ind contextualSpacing mirrorIndents suppressOverlap jc textDirection textAlignment "
           "textboxTightWrap outlineLvl divId cnfStyle rPr sectPr pPrChange",
    "rPr": "ins del moveFrom moveTo rStyle rFonts b bCs i iCs caps smallCaps strike dstrike outline shadow emboss imprint "
           "noProof snapToGrid vanish webHidden color spacing w kern position sz szCs highlight u effect bdr shd fitText "
           "vertAlign rtl cs em lang eastAsianLayout specVanish oMath rPrChange",
    "style": "name aliases basedOn next link autoRedefine hidden uiPriority semiHidden unhideWhenUsed qFormat locked "
             "personal personalCompose personalReply rsid pPr rPr tblPr trPr tcPr tblStylePr",
    "sectPr": "headerReference footerReference footnotePr endnotePr type pgSz pgMar paperSrc pgBorders lnNumType "
              "pgNumType cols formProt vAlign noEndnote titlePg textDirection bidi rtlGutter docGrid printerSettings "
              "sectPrChange",
    "tblPr": "tblStyle tblpPr tblOverlap bidiVisual tblStyleRowBandSize tblStyleColBandSize tblW jc tblCellSpacing tblInd "
             "tblBorders shd tblLayout tblCellMar tblLook tblCaption tblDescription tblPrChange",
    "tcPr": "cnfStyle tcW gridSpan hMerge vMerge tcBorders shd noWrap tcMar textDirection tcFitText vAlign hideMark "
            "headers cellIns cellDel cellMerge tcPrChange",
    "trPr": "cnfStyle divId gridBefore gridAfter wBefore wAfter cantSplit trHeight tblHeader tblCellSpacing jc hidden "
            "ins del trPrChange",
    "tcBorders": "top start left bottom end right insideH insideV tl2br tr2bl",
    "tblBorders": "top start left bottom end right insideH insideV",
    "pBdr": "top left start bottom right end between bar",
    "lvl": "start numFmt lvlRestart pStyle isLgl suff lvlText lvlPicBulletId legacy lvlJc pPr rPr",
    "settings": "writeProtection view zoom removePersonalInformation removeDateAndTime doNotDisplayPageBoundaries "
                "displayBackgroundShape printPostScriptOverText printFractionalCharacterWidth printFormsData "
                "embedTrueTypeFonts embedSystemFonts saveSubsetFonts saveFormsData mirrorMargins alignBordersAndEdges "
                "bordersDoNotSurroundHeader bordersDoNotSurroundFooter gutterAtTop hideSpellingErrors "
                "hideGrammaticalErrors activeWritingStyle proofState formsDesign attachedTemplate linkStyles "
                "stylePaneFormatFilter stylePaneSortMethod documentType mailMerge revisionView trackRevisions "
                "doNotTrackMoves doNotTrackFormatting documentProtection autoFormatOverride styleLockTheme "
                "styleLockQFSet defaultTabStop autoHyphenation consecutiveHyphenLimit hyphenationZone "
                "doNotHyphenateCaps showEnvelope summaryLength clickAndTypeStyle defaultTableStyle evenAndOddHeaders "
                "bookFoldRevPrinting bookFoldPrinting bookFoldPrintingSheets drawingGridHorizontalSpacing "
                "drawingGridVerticalSpacing displayHorizontalDrawingGridEvery displayVerticalDrawingGridEvery "
                "doNotUseMarginsForDrawingGridOrigin drawingGridHorizontalOrigin drawingGridVerticalOrigin "
                "doNotShadeFormData noPunctuationKerning characterSpacingControl printTwoOnOne strictFirstAndLastChars "
                "noLineBreaksAfter noLineBreaksBefore savePreviewPicture doNotValidateAgainstSchema saveInvalidXml "
                "ignoreMixedContent alwaysShowPlaceholderText doNotDemarcateInvalidXml saveXmlDataOnly "
                "useXSLTWhenSaving saveThroughXslt showXMLTags alwaysMergeEmptyNamespace updateFields hdrShapeDefaults "
                "footnotePr endnotePr compat docVars rsids mathPr attachedSchema themeFontLang clrSchemeMapping "
                "doNotIncludeSubdocsInStats doNotAutoCompressPictures forceUpgrade captions readModeInkLockDown "
                "smartTagType schemaLibrary shapeDefaults doNotEmbedSmartTags decimalSymbol listSeparator",
}
_ORDER_W = {k: {t: i for i, t in enumerate(v.split())} for k, v in _SEQ_W.items()}
_ORDER_X = {"font": {t: i for i, t in enumerate(
    "b i strike condense extend outline shadow u vertAlign sz color name family charset scheme".split())}}
_SEQ_NS = ("http://schemas.openxmlformats.org/officeDocument/2006/math",        # settings: m:mathPr
           "http://schemas.openxmlformats.org/schemaLibrary/2006/main")          # settings: sl:schemaLibrary
_MULTI = {"activeWritingStyle", "attachedSchema", "smartTagType", "headerReference", "footerReference"}
_FLOAT_INT = re.compile(r"-?\d+\.0+")
_HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")


def _reorder(el, order: dict[str, int], ns: str) -> bool:
    kids = list(el)
    if len(kids) < 2:
        return False
    keys, last = [], -1.0
    for i, c in enumerate(kids):
        q = etree.QName(c) if isinstance(c.tag, str) else None
        if q is not None and q.namespace in (ns, *_SEQ_NS) and q.localname in order:
            last = float(order[q.localname])
            keys.append((last, i))
        else:                                   # bővítmény / megjegyzés: az előtte álló ismert elem után marad
            keys.append((last + 0.5, i))
    want = [kids[i] for _, i in sorted(keys)]
    if want == kids:
        return False
    for c in kids:
        el.remove(c)
    el.extend(want)
    return True


def _dedupe(el) -> int:
    """Kétszer szereplő egyszeri elem (pl. a pandoc két w:proofState-je): az elsőbe olvad, a többi törlődik."""
    seen: dict[str, object] = {}
    n = 0
    for c in list(el):
        if not isinstance(c.tag, str) or etree.QName(c).localname in _MULTI:
            continue
        if c.tag in seen:
            first = seen[c.tag]
            if len(c) == 0 and len(first) == 0:        # csak attribútumos elem: összevonható
                for k, v in c.attrib.items():
                    if first.get(k) is None:
                        first.set(k, v)
                el.remove(c)
                n += 1
        else:
            seen[c.tag] = c
    return n


def _fix_word_part(root) -> int:
    n = 0
    wq = f"{{{W}}}"
    for el in list(root.iter(f"{wq}*")):          # előre listázva: az átrendezés ne zavarja a bejárást
        local = etree.QName(el).localname
        if local in _ORDER_W:
            if local == "settings":
                n += _dedupe(el)
            n += _reorder(el, _ORDER_W[local], W)
        for k, v in list(el.attrib.items()):
            if not k.startswith(wq):
                continue
            ak = k[len(wq):]
            if ak in ("color", "fill") and _HEX_COLOR.fullmatch(v):
                el.set(k, v[1:].upper()); n += 1
            elif _FLOAT_INT.fullmatch(v):
                el.set(k, v.split(".")[0]); n += 1
        if local == "rFonts":
            for a in ("ascii", "hAnsi", "cs", "eastAsia"):
                v = el.get(f"{wq}{a}")
                if v and (len(v) > 31 or ";" in v or "," in v):
                    first = re.split(r"[;,]", v)[0].strip().strip("'\"")[:31]
                    el.set(f"{wq}{a}", first or "Calibri"); n += 1
        if local == "shd" and el.get(f"{wq}val") is None:
            el.set(f"{wq}val", "clear"); n += 1
        if local in ("nsid", "tmpl"):
            v = el.get(f"{wq}val") or ""
            if v and len(v) < 8 and re.fullmatch(r"[0-9A-Fa-f]+", v):
                el.set(f"{wq}val", v.upper().zfill(8)); n += 1
    return n


def _fix_xlsx_styles(root) -> int:
    n = 0
    for el in list(root.iter(f"{{{X}}}font")):
        n += _reorder(el, _ORDER_X["font"], X)
    return n


def _targets(names: list[str], kind: str) -> list[str]:
    if kind == "docx":
        return [n for n in names if re.fullmatch(r"word/(document|styles|numbering|settings|footnotes|endnotes|comments|"
                                                 r"header\d*|footer\d*|glossary/document)\.xml", n)]
    return [n for n in names if n == "xl/styles.xml"]


def _fix_lang(zf: zipfile.ZipFile, new: dict[str, bytes]) -> None:
    """A docx alapnyelve a szöveg nyelve legyen: a pandoc/LibreOffice/PdfCraft „en-US”-t ír, és akkor a Word,
    Pages, WordCraft egy magyar levél minden szavát pirossal húzza alá (UI-próba 10-18). Csak biztos felismerésnél,
    és csak az „en-US”/hiányzó alapot írja át — kifejezett más nyelvhez nem nyúl."""
    from app.tools import textlang
    names = set(zf.namelist())
    if "word/document.xml" not in names or "word/styles.xml" not in names:
        return
    doc = etree.fromstring(new.get("word/document.xml") or zf.read("word/document.xml"))
    lang = textlang.detect(" ".join(t.text or "" for t in doc.iter(f"{{{W}}}t")))
    if not lang or lang == "en-US":
        return
    styles = etree.fromstring(new.get("word/styles.xml") or zf.read("word/styles.xml"))
    q = lambda t: f"{{{W}}}{t}"
    dd = styles.find(q("docDefaults"))
    if dd is None:
        dd = etree.Element(q("docDefaults")); styles.insert(0, dd)
    rpd = dd.find(q("rPrDefault"))
    if rpd is None:
        rpd = etree.Element(q("rPrDefault")); dd.insert(0, rpd)
    rpr = rpd.find(q("rPr"))
    if rpr is None:
        rpr = etree.SubElement(rpd, q("rPr"))
    el = rpr.find(q("lang"))
    if el is None:
        el = etree.SubElement(rpr, q("lang"))
        _reorder(rpr, _ORDER_W["rPr"], W)                 # sémasorrend: a lang a helyére
    if el.get(q("val")) not in (None, "en-US"):
        return
    el.set(q("val"), lang)
    new["word/styles.xml"] = etree.tostring(styles, xml_declaration=True, encoding="UTF-8", standalone=True)
    runs = [e for e in doc.iter(q("lang")) if e.get(q("val")) == "en-US"]    # kifejezett en-US a futásokon is
    for e in runs:
        e.set(q("val"), lang)
    if runs:
        new["word/document.xml"] = etree.tostring(doc, xml_declaration=True, encoding="UTF-8", standalone=True)


def fix(target: Union[str, os.PathLike]) -> list[str]:
    """Egy .docx/.xlsx helyben javítva. → a javított részek listája."""
    from app.tools.keynote_safe import _rewrite
    p = Path(target)
    ext = p.suffix.lower().lstrip(".")
    kind = "docx" if ext in DOCX_EXTS else "xlsx" if ext in XLSX_EXTS else ""
    if not kind:
        return []
    try:
        with zipfile.ZipFile(p) as zf:
            new: dict[str, bytes] = {}
            for name in _targets(zf.namelist(), kind):
                raw = zf.read(name)
                root = etree.fromstring(raw)
                if (_fix_word_part(root) if kind == "docx" else _fix_xlsx_styles(root)):
                    new[name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            if kind == "docx":
                _fix_lang(zf, new)
            if not new:
                return []
            fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".ooxml-", suffix=p.suffix)
            try:
                with os.fdopen(fd, "wb") as fh:
                    _rewrite(zf, fh, new, set())
                os.replace(tmp, p)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        return sorted(new)
    except Exception as e:      # a javítás kényelmi — a kész fájlt nem buktatja
        log.warning("ooxml-safe pass skipped for %s: %s", p.name, e)
        return []


def install() -> None:
    """A python-docx és az openpyxl minden mentése (szerkesztések, kitöltés, sablonok) sémahelyes legyen."""
    from docx.document import Document
    from openpyxl.workbook.workbook import Workbook
    for cls in (Document, Workbook):
        if getattr(cls.save, "_ooxml_safe", False):
            continue
        orig = cls.save

        def save(self, filename, *a, _orig=orig, **k):
            _orig(self, filename, *a, **k)
            if isinstance(filename, (str, os.PathLike)):
                fix(filename)

        save._ooxml_safe = True      # type: ignore[attr-defined]
        save.__doc__ = orig.__doc__
        cls.save = save              # type: ignore[method-assign]
