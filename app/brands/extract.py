"""
Brand extraction — analyze an arbitrary brand-foundation document and
produce a draft Brand spec.

Pipeline:
  1. Programmatic XML scan: top colors (frequency-weighted), fonts, named
     paragraph styles. Cheap and exact.
  2. Render first 1-3 pages as PNG and ask the vision model (Kimi K2.6
     multimodal) to describe the visual identity: dark cover? eyebrow?
     section labels? heading underlines? typography flavor?
  3. Synthesize a draft Brand spec dict (JSON) the user/agent can review,
     tweak, and pass to brand_register to add to the catalog.
"""
from __future__ import annotations

import collections
import logging
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

from app.storage import (
    input_path,
    new_file_id,
    output_path,
    public_url,
)
from app.tools.formats import detect_from_path

log = logging.getLogger("aice.brand.extract")


def _resolve(file_id: str) -> Path:
    p = output_path(file_id)
    if p.exists():
        return p
    return input_path(file_id)


# ============================================================
# Programmatic XML scan
# ============================================================


def _scan_docx(src: Path) -> dict[str, Any]:
    """Scan a DOCX zip's XML for color frequency, font usage, named styles."""
    parts: dict[str, str] = {}
    try:
        with zipfile.ZipFile(str(src)) as z:
            for path in ("word/document.xml", "word/styles.xml",
                         "word/theme/theme1.xml", "word/header1.xml",
                         "word/footer1.xml"):
                try:
                    parts[path] = z.read(path).decode("utf-8", errors="replace")
                except KeyError:
                    pass
    except (zipfile.BadZipFile, KeyError) as e:
        log.warning("could not unzip %s: %s", src, e)
        return {"error": f"unzip failed: {e}"}

    doc_xml = parts.get("word/document.xml", "")
    styles_xml = parts.get("word/styles.xml", "")
    theme_xml = parts.get("word/theme/theme1.xml", "")

    # Color frequency: w:color w:val="XXXXXX" and w:fill="XXXXXX"
    color_counts: collections.Counter = collections.Counter()
    for m in re.finditer(
        r'(?:w:fill|w:color\s+w:val|w:fillColor)\s*=\s*"([0-9A-Fa-f]{6})"',
        doc_xml,
    ):
        color_counts[m.group(1).upper()] += 1

    # Theme-defined srgbClrs
    theme_colors = re.findall(r'<a:srgbClr\s+val="([0-9A-Fa-f]{6})"', theme_xml)

    # Font usage from rFonts in document body
    font_counts: collections.Counter = collections.Counter()
    for m in re.finditer(
        r'w:rFonts[^/>]*?(?:w:ascii|w:hAnsi|w:cs)="([^"]+)"', doc_xml,
    ):
        font_counts[m.group(1)] += 1

    # Theme fonts (latin typeface)
    theme_fonts = re.findall(
        r'<a:latin\s+typeface="([^"]+)"', theme_xml,
    )

    # Named paragraph styles (from styles.xml)
    style_ids = re.findall(r'w:styleId="([^"]+)"', styles_xml)

    return {
        "top_colors": [
            {"hex": "#" + c, "count": n}
            for c, n in color_counts.most_common(10)
        ],
        "theme_colors": ["#" + c for c in theme_colors[:8]],
        "top_fonts": [
            {"name": f, "count": n}
            for f, n in font_counts.most_common(6)
        ],
        "theme_fonts": list(dict.fromkeys(theme_fonts))[:5],
        "named_styles": style_ids[:40],
    }


def _scan_pdf(src: Path) -> dict[str, Any]:
    """Scan a PDF for fonts and approximate color frequency (sampled from text)."""
    import fitz

    doc = fitz.open(str(src))
    font_counts: collections.Counter = collections.Counter()
    color_counts: collections.Counter = collections.Counter()
    for page in doc:
        text_dict = page.get_text("dict")
        for block in text_dict.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    fname = span.get("font", "")
                    if fname:
                        font_counts[fname.split("+")[-1]] += 1
                    color = span.get("color", 0)
                    hex_c = f"{color:06X}"
                    color_counts[hex_c] += 1
    n_pages = doc.page_count
    doc.close()
    return {
        "page_count": n_pages,
        "top_fonts": [
            {"name": f, "count": n}
            for f, n in font_counts.most_common(6)
        ],
        "top_colors": [
            {"hex": "#" + c, "count": n}
            for c, n in color_counts.most_common(10)
        ],
    }


# ============================================================
# Brand spec synthesis from raw extracted data
# ============================================================


def _propose_brand_spec(extracted: dict[str, Any], page_descriptions: list[dict],
                        suggested_name: str) -> dict[str, Any]:
    """Build a draft Brand spec dict from the raw extracted data + descriptions.
    Heuristic: pick the strongest non-white/non-black color as primary, the
    next-strongest contrasting one as accent, etc."""
    top_colors = extracted.get("top_colors", []) or extracted.get("theme_colors", [])

    def _hex_only(item):
        if isinstance(item, dict):
            return item["hex"]
        return item

    # Filter out neutrals (white/black/near-greys)
    def _is_neutral(hex_c: str) -> bool:
        s = hex_c.lstrip("#")
        if len(s) != 6:
            return True
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        # Treat very light or very dark as "neutral candidates" UNLESS deeply saturated
        if r > 240 and g > 240 and b > 240:
            return True
        if r < 20 and g < 20 and b < 20:
            return True
        # Greys: low chroma (max-min difference small)
        return (max(r, g, b) - min(r, g, b)) < 12

    palette = []
    for c in top_colors:
        h = _hex_only(c)
        if not _is_neutral(h):
            palette.append(h)

    primary = palette[0] if palette else "#0E1726"
    accent = next((h for h in palette[1:] if h.lower() != primary.lower()), "#C66A00")

    fonts = [f["name"] if isinstance(f, dict) else f for f in extracted.get("top_fonts", [])]
    serif_candidates = [f for f in fonts if any(
        s in f.lower() for s in ["serif", "georgia", "garamond", "cambria", "times"])]
    sans_candidates = [f for f in fonts if any(
        s in f.lower() for s in ["aptos", "calibri", "arial", "helvet", "inter", "segoe"])]
    heading_font = serif_candidates[0] if serif_candidates else (
        fonts[0] if fonts else "Georgia")
    body_font = sans_candidates[0] if sans_candidates else (
        fonts[1] if len(fonts) > 1 else "Calibri")

    # Section label heuristic from named styles
    has_section_label_style = any(
        "section" in s.lower() and "label" in s.lower()
        for s in extracted.get("named_styles", [])
    )

    return {
        "name": suggested_name,
        "label": suggested_name.replace("_", " ").title(),
        "description": "Auto-extracted draft. Edit before registering.",
        "document_kind": "policy_brief",
        "colors": {
            "primary": primary,
            "accent": accent,
            "body_bg": "#FFFFFF",
            "panel_bg": "#F7F8FA",
            "highlight_bg": "#FFF9EC",
            "text_primary": primary,
            "text_muted": "#6B7280",
            "text_subtle": "#AAB3C2",
        },
        "fonts": {
            "heading": heading_font,
            "body": body_font,
            "heading_fallback": "Times New Roman",
            "body_fallback": "Calibri",
            "eyebrow": body_font,
        },
        "motifs": {
            "cover_dark_bg": True,
            "cover_serif_headline": True,
            "cover_meta_block": True,
            "inner_top_eyebrow": True,
            "section_label_format": "{number:02d} / {title}",
            "section_label_caps": True,
            "heading_underline": True,
            "accent_bar_position": "none",
            "footer_text_pattern": "{document_label} · {page}",
            "page_numbers": True,
        },
        "config": {
            "organization_name": "[organization name from page descriptions]",
            "cover_meta_fields": ["DÁTUM", "KOCKÁZATI SZINT", "HORIZONT", "DOKUMENTUM"],
            "cover_eyebrow_template": "{document_kind_label} · {brief_code}",
            "default_document_kind_label": "DOKUMENTUM",
        },
        "_extracted_raw": {
            "top_colors": top_colors[:6],
            "top_fonts": fonts[:5],
            "named_styles_sample": extracted.get("named_styles", [])[:15],
            "section_label_style_detected": has_section_label_style,
        },
        "_page_descriptions": page_descriptions,
    }


# ============================================================
# Public API
# ============================================================


async def brand_extract(
    pool, file_id: str, *,
    sample_pages: int = 2,
    suggested_name: str = "new_brand",
) -> dict[str, Any]:
    """Analyze a brand-foundation document (DOCX or PDF) and produce a draft
    Brand spec. The result is intended to be reviewed by the agent or user,
    then passed to brand_register to add to the catalog.

    Steps:
      1. Programmatic scan of XML / PDF metadata: colors, fonts, styles
      2. Render first `sample_pages` pages as PNG via LO+PyMuPDF
      3. (If vision available) describe each rendered page via Kimi K2.6
         multimodal — captures motifs that XML scan can't see
      4. Synthesize a draft Brand spec
    """
    from app.tools.convert import convert as _convert

    src = _resolve(file_id)
    fmt = detect_from_path(src)
    t0 = time.time()

    extracted: dict[str, Any] = {"format": fmt, "source_file_id": file_id}

    # Phase 1: programmatic extraction
    if fmt == "docx":
        extracted.update(_scan_docx(src))
    elif fmt == "pdf":
        extracted.update(_scan_pdf(src))
    else:
        return {"error": f"brand_extract supports DOCX or PDF (got {fmt})"}

    # Phase 2: render sample pages
    pdf_path = src
    if fmt != "pdf":
        pdf_out = await _convert(pool, file_id=file_id, target_format="pdf")
        pdf_path = _resolve(pdf_out["file_id"])

    import fitz
    doc = fitz.open(str(pdf_path))
    n_to_render = min(sample_pages, doc.page_count)
    page_pngs: list[dict] = []
    for i in range(n_to_render):
        pix = doc[i].get_pixmap(matrix=fitz.Matrix(110 / 72, 110 / 72))
        png_fid = new_file_id("png")
        with open(output_path(png_fid), "wb") as f:
            f.write(pix.tobytes("png"))
        page_pngs.append({
            "page_index": i,
            "image_file_id": png_fid,
            "url": public_url(png_fid),
        })
    doc.close()

    # Phase 3: vision describe each rendered page
    page_descriptions: list[dict] = []
    try:
        from app.tools import vision_ops
        for p in page_pngs:
            try:
                d = await vision_ops.describe_image(
                    p["image_file_id"],
                    question=(
                        "This is a page from a branded analytical document template. "
                        "Describe the VISUAL BRAND IDENTITY in 4-6 short bullet points: "
                        "(1) page background color & overall tone (dark/light), "
                        "(2) accent color & where it appears (eyebrow, rules, accents), "
                        "(3) typography flavor (serif headline? sans body?), "
                        "(4) layout motifs (eyebrow at top? section label like 'NN / TITLE'? "
                        "heading underline? cover meta-block?), "
                        "(5) header/footer pattern, "
                        "(6) any logos, charts, or visual elements present. "
                        "Be concise and specific — this feeds an automated brand spec extractor."
                    ),
                )
                page_descriptions.append({
                    "page_index": p["page_index"],
                    "image_file_id": p["image_file_id"],
                    "description": d.get("description", ""),
                })
            except Exception as e:
                log.warning("vision describe failed on page %s: %s", p["page_index"], e)
                page_descriptions.append({
                    "page_index": p["page_index"],
                    "image_file_id": p["image_file_id"],
                    "error": str(e)[:200],
                })
    except Exception as e:
        log.warning("vision_ops not available: %s", e)

    # Phase 4: synthesize draft Brand spec
    proposed = _propose_brand_spec(extracted, page_descriptions, suggested_name)

    return {
        "source_file_id": file_id,
        "format": fmt,
        "rendered_pages": page_pngs,
        "page_descriptions": page_descriptions,
        "scan": {k: v for k, v in extracted.items() if k != "format"},
        "proposed_brand_spec": proposed,
        "ms_elapsed": int((time.time() - t0) * 1000),
        "next_step_hint": (
            "Review proposed_brand_spec, edit colors/fonts/config as needed, "
            "then call brand_register(name, spec_dict) to add to the catalog."
        ),
    }


def brand_register_runtime(spec: dict[str, Any]) -> dict[str, Any]:
    """Register a brand spec at runtime in the in-memory BRANDS dict.
    NOTE: this is RUNTIME ONLY — the brand exists until the server restarts.
    For persistence, the spec should be saved to a Python file in app/brands/."""
    from app.brands.base import Brand, BrandColors, BrandFonts, BrandMotifs
    from app.brands.registry import BRANDS

    name = spec.get("name", "")
    if not name or "/" in name or " " in name:
        return {"error": "brand 'name' is required and must be lowercase_underscore"}

    colors_dict = spec.get("colors", {})
    fonts_dict = spec.get("fonts", {})
    motifs_dict = spec.get("motifs", {})

    try:
        colors = BrandColors(
            primary=colors_dict.get("primary", "#0E1726"),
            accent=colors_dict.get("accent", "#C66A00"),
            body_bg=colors_dict.get("body_bg", "#FFFFFF"),
            panel_bg=colors_dict.get("panel_bg", "#F7F8FA"),
            highlight_bg=colors_dict.get("highlight_bg", "#FFF9EC"),
            text_primary=colors_dict.get("text_primary", "#0E1726"),
            text_muted=colors_dict.get("text_muted", "#6B7280"),
            text_subtle=colors_dict.get("text_subtle", "#AAB3C2"),
        )
        fonts = BrandFonts(
            heading=fonts_dict.get("heading", "Georgia"),
            body=fonts_dict.get("body", "Calibri"),
            heading_fallback=fonts_dict.get("heading_fallback", "Times New Roman"),
            body_fallback=fonts_dict.get("body_fallback", "Calibri"),
            eyebrow=fonts_dict.get("eyebrow"),
        )
        motifs = BrandMotifs(
            cover_dark_bg=motifs_dict.get("cover_dark_bg", True),
            cover_serif_headline=motifs_dict.get("cover_serif_headline", True),
            cover_meta_block=motifs_dict.get("cover_meta_block", True),
            inner_top_eyebrow=motifs_dict.get("inner_top_eyebrow", True),
            section_label_format=motifs_dict.get("section_label_format", "{number:02d} / {title}"),
            section_label_caps=motifs_dict.get("section_label_caps", True),
            heading_underline=motifs_dict.get("heading_underline", True),
            accent_bar_position=motifs_dict.get("accent_bar_position", "none"),
            footer_text_pattern=motifs_dict.get("footer_text_pattern", "{document_label} · {page}"),
            page_numbers=motifs_dict.get("page_numbers", True),
        )
        brand = Brand(
            name=name,
            label=spec.get("label", name.replace("_", " ").title()),
            description=spec.get("description", ""),
            document_kind=spec.get("document_kind", "policy_brief"),
            colors=colors,
            fonts=fonts,
            motifs=motifs,
            config=spec.get("config", {}),
        )
    except Exception as e:
        return {"error": f"failed to build Brand from spec: {e}"}

    BRANDS[name] = brand
    return {
        "registered": True,
        "name": name,
        "label": brand.label,
        "total_brands": len(BRANDS),
    }
