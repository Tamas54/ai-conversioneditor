"""
Format detection and conversion routing.

The conversion matrix decides WHICH backend handles a given (src → tgt) pair:

         | pdf       docx      html      md        odt       pptx      xlsx
    pdf  | -         pdf2docx  -         -         lo        -         -
    docx | lo        -         pandoc    pandoc    lo        -         -
    html | weasy/cr  pandoc    -         pandoc    lo        -         -
    md   | pandoc+w  pandoc    pandoc    -         pandoc    -         -
    odt  | lo        lo        pandoc    pandoc    -         -         -
    pptx | lo        -         -         -         -         -         -
    xlsx | lo        -         lo        -         -         -         -

Legend:
    lo       = LibreOffice headless
    pandoc   = Pandoc (text-flow conversions)
    pandoc+w = Pandoc → HTML → WeasyPrint (or Chromium for JS-heavy)
    weasy/cr = WeasyPrint default; Chromium when use_browser=True
    pdf2docx = pdf2docx library (PDF → DOCX reflow)
"""
from __future__ import annotations

from pathlib import Path

# Canonical formats supported
CANONICAL = {"pdf", "docx", "doc", "html", "htm", "md", "odt", "pptx", "xlsx", "rtf", "txt"}

# Aliases
ALIASES = {
    "markdown": "md",
    "msword": "doc",
    "openxml": "docx",
}

# Source LibreOffice document class — picks which export filter family applies.
# When converting, the source's class determines which filter name to use.
SOURCE_CLASS_OF = {
    # Writer class (text documents)
    "docx": "writer", "doc": "writer", "odt": "writer", "rtf": "writer",
    "html": "writer", "htm": "writer", "txt": "writer", "md": "writer",
    # Impress class (presentations)
    "pptx": "impress", "ppt": "impress", "odp": "impress",
    # Calc class (spreadsheets)
    "xlsx": "calc", "xls": "calc", "ods": "calc", "csv": "calc",
    # Draw class (PDFs open as Draw in LO)
    "pdf": "draw",
}

# (source_class, target_ext) → soffice "ext:filter-name" spec.
# Different source classes need different filter names for the same target —
# e.g. Writer→HTML uses "HTML (StarWriter)", Impress→HTML uses "impress_html_Export".
LO_TARGET_FILTER = {
    # --- Writer source ---
    ("writer", "pdf"): "pdf:writer_pdf_Export",
    ("writer", "docx"): "docx:MS Word 2007 XML",
    ("writer", "doc"): "doc:MS Word 97",
    ("writer", "odt"): "odt:writer8",
    ("writer", "rtf"): "rtf:Rich Text Format",
    ("writer", "html"): "html:HTML (StarWriter)",
    ("writer", "txt"): "txt:Text",
    # --- Impress source ---
    ("impress", "pdf"): "pdf:impress_pdf_Export",
    ("impress", "pptx"): "pptx:Impress MS PowerPoint 2007 XML",
    ("impress", "ppt"): "ppt:MS PowerPoint 97",
    ("impress", "odp"): "odp:impress8",
    ("impress", "html"): "html:impress_html_Export",
    ("impress", "png"): "png:impress_png_Export",
    # --- Calc source ---
    ("calc", "pdf"): "pdf:calc_pdf_Export",
    ("calc", "xlsx"): "xlsx:Calc MS Excel 2007 XML",
    ("calc", "xls"): "xls:MS Excel 97",
    ("calc", "ods"): "ods:calc8",
    ("calc", "csv"): "csv:Text - txt - csv (StarCalc)",
    ("calc", "html"): "html:calc_html_Export",
    # --- Draw source (PDFs) ---
    ("draw", "pdf"): "pdf:draw_pdf_Export",
    ("draw", "html"): "html:draw_html_Export",
    ("draw", "png"): "png:draw_png_Export",
}

# Legacy alias kept for any old callers; resolves filter as if source were Writer.
LO_FILTERS = {tgt: spec for (cls, tgt), spec in LO_TARGET_FILTER.items() if cls == "writer"}


def lo_filter_for(src_fmt: str, tgt_fmt: str) -> str | None:
    src_cls = SOURCE_CLASS_OF.get(src_fmt, "writer")
    return LO_TARGET_FILTER.get((src_cls, tgt_fmt))


def normalize_format(fmt: str) -> str:
    fmt = fmt.lower().lstrip(".")
    return ALIASES.get(fmt, fmt)


def detect_from_path(path: Path) -> str:
    return normalize_format(path.suffix)


def route(src: str, tgt: str, use_browser: bool = False) -> str:
    """Return the backend name for src → tgt conversion."""
    src = normalize_format(src)
    tgt = normalize_format(tgt)
    if src == tgt:
        return "noop"

    text_flow = {"md", "html", "htm", "txt", "rtf"}

    # HTML → PDF: WeasyPrint by default, Chromium if requested
    if src in {"html", "htm"} and tgt == "pdf":
        return "chromium" if use_browser else "weasyprint"

    # MD → PDF: Pandoc → HTML → WeasyPrint (single internal step)
    if src == "md" and tgt == "pdf":
        return "md_to_pdf"

    # PDF → DOCX: pdf2docx (preserves layout best of any PDF→Word path)
    if src == "pdf" and tgt == "docx":
        return "pdf2docx"

    # PDF → DOCX/HTML/etc.: route through DOCX intermediate. LO opens PDF
    # as a Draw document; reliable text-class conversions go via_docx.
    if src == "pdf" and tgt != "pdf":
        return "via_docx"

    src_cls = SOURCE_CLASS_OF.get(src, "writer")
    tgt_cls = SOURCE_CLASS_OF.get(tgt, "writer")

    # HTML → DOCX/ODT: prefer LibreOffice (better CSS/layout preservation than Pandoc)
    if src in {"html", "htm"} and tgt in {"docx", "odt"}:
        return "libreoffice"

    # MD-as-source: pandoc handles md cleanly
    if src == "md" and tgt in text_flow | {"docx", "odt"}:
        return "pandoc"

    # MD → other formats (pptx/odp/xlsx/etc.): via DOCX intermediate
    if src == "md":
        return "via_md_docx"

    # text_flow source → text_flow target (excluding md): LO handles this,
    # since LO has explicit filters for txt/html/rtf/odt
    if src in text_flow and tgt in text_flow | {"docx", "odt"}:
        return "libreoffice" if (src_cls, tgt) in LO_TARGET_FILTER else "pandoc"

    # Writer-class source → md: pandoc preserves text-flow semantics
    if src in {"docx", "odt"} and tgt == "md":
        return "pandoc"

    # Writer-class source → txt: LO has Text filter, pandoc does not
    if src in {"docx", "odt"} and tgt == "txt":
        return "libreoffice"

    # Direct LO conversion for everything where source class supports the target
    if (src_cls, tgt) in LO_TARGET_FILTER:
        return "libreoffice"

    # Cross-class (e.g. PPTX → DOCX, XLSX → DOCX, etc.): go via PDF intermediate
    if src_cls in {"writer", "impress", "calc"} and tgt_cls in {"writer", "impress", "calc"}:
        return "via_pdf"

    raise ValueError(f"Unsupported conversion: {src} → {tgt}")
