"""
HTML renderer for a Brand.

Takes a source document (DOCX, MD, or HTML) and produces a branded HTML page:
  - CSS variables from brand colors / fonts
  - Cover block (dark navy + serif headline + 4-col meta) when brief_meta has cover content
  - Top eyebrow (org name) on every page
  - 'NN / TITLE' orange section labels detected and styled
  - H1 with accent underline rule
  - Tables with navy header + alternating row bands
  - Footer with document_label · page-context

Cross-format: HTML → DOCX (pandoc), HTML → PDF (WeasyPrint/Chromium) all
preserve brand because the rendered HTML carries inline CSS.
"""
from __future__ import annotations

import logging
import re
import time
from html import escape
from pathlib import Path
from typing import Any, Optional

from app.brands.base import Brand
from app.storage import (
    input_path,
    new_file_id,
    output_path,
    public_url,
)

log = logging.getLogger("aice.brand.html")


def _resolve(file_id: str) -> Path:
    p = output_path(file_id)
    if p.exists():
        return p
    return input_path(file_id)


def _brand_css(brand: Brand) -> str:
    """Generate CSS for the brand. Uses CSS custom properties so derivative
    documents (HTML→PDF via WeasyPrint, etc.) can override or extend."""
    return f"""
:root {{
  --brand-primary:    {brand.colors.primary};
  --brand-accent:     {brand.colors.accent};
  --brand-body-bg:    {brand.colors.body_bg};
  --brand-panel-bg:   {brand.colors.panel_bg};
  --brand-highlight:  {brand.colors.highlight_bg};
  --brand-text:       {brand.colors.text_primary};
  --brand-muted:      {brand.colors.text_muted};
  --brand-subtle:     {brand.colors.text_subtle};
  --font-heading:     {brand.fonts.heading_with_fallback()}, serif;
  --font-body:        {brand.fonts.body_with_fallback()}, system-ui, sans-serif;
}}

* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; }}

body {{
  font-family: var(--font-body);
  color: var(--brand-text);
  background: var(--brand-body-bg);
  font-size: 11pt;
  line-height: 1.5;
}}

.page {{
  max-width: 56rem;
  margin: 0 auto;
  padding: 0 2rem 4rem;
}}

.eyebrow-bar {{
  border-bottom: 1px solid var(--brand-subtle);
  margin: 0 0 2rem;
  padding: 1.25rem 0 0.6rem;
}}
.eyebrow {{
  font-family: var(--font-body);
  font-size: 8.5pt;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  font-weight: 700;
  color: var(--brand-subtle);
  margin: 0;
}}

.cover {{
  background: var(--brand-primary);
  color: var(--brand-body-bg);
  padding: 4rem 3rem;
  margin: 0 0 3rem;
  border-radius: 0;
}}
.cover .org {{
  font-size: 8.5pt;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  font-weight: 700;
  color: var(--brand-subtle);
  margin: 0 0 3rem;
}}
.cover .eyebrow-orange {{
  color: var(--brand-accent);
  font-size: 10.5pt;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  font-weight: 700;
  margin: 0 0 0.4rem;
}}
.cover .sub-eyebrow {{
  color: var(--brand-subtle);
  font-size: 8.5pt;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  margin: 0 0 2.5rem;
}}
.cover h1 {{
  font-family: var(--font-heading);
  font-size: 42pt;
  font-weight: 700;
  line-height: 1.05;
  margin: 0 0 1.5rem;
  border: 0;
  padding: 0;
  color: var(--brand-body-bg);
}}
.cover .lead {{
  font-size: 14pt;
  margin: 0 0 3rem;
  color: var(--brand-body-bg);
}}
.cover .gold-rule {{
  border: 0;
  border-top: 2px solid var(--brand-accent);
  margin: 0 0 1.5rem;
}}
.cover .meta-block {{
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 1.5rem;
}}
.cover .meta-block .label {{
  font-size: 8pt;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--brand-subtle);
  margin: 0 0 0.3rem;
  font-weight: 700;
}}
.cover .meta-block .value {{
  font-size: 11pt;
  font-weight: 700;
  color: var(--brand-body-bg);
}}

.section-label {{
  font-family: var(--font-body);
  font-size: 10pt;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  font-weight: 700;
  color: var(--brand-accent);
  margin: 2.5rem 0 0.4rem;
}}

h1 {{
  font-family: var(--font-heading);
  font-size: 24pt;
  font-weight: 700;
  line-height: 1.15;
  margin: 0.4rem 0 0.5rem;
  color: var(--brand-text);
  padding-bottom: 0.4rem;
  border-bottom: 2px solid var(--brand-accent);
  display: inline-block;
}}
h2 {{
  font-family: var(--font-heading);
  font-size: 16pt;
  font-weight: 700;
  margin: 2rem 0 0.5rem;
  color: var(--brand-text);
}}
h3 {{
  font-family: var(--font-body);
  font-size: 11pt;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-weight: 700;
  margin: 1.5rem 0 0.4rem;
  color: var(--brand-accent);
}}

p {{ margin: 0 0 0.8rem; }}
ul, ol {{ margin: 0 0 1rem 1.5rem; padding: 0; }}
li {{ margin-bottom: 0.3rem; }}

blockquote {{
  background: var(--brand-highlight);
  border-left: 4px solid var(--brand-accent);
  margin: 1rem 0;
  padding: 1rem 1.5rem;
  font-family: var(--font-heading);
  font-style: italic;
  color: var(--brand-text);
}}

table {{
  border-collapse: collapse;
  width: 100%;
  margin: 1rem 0;
  font-size: 10pt;
}}
th {{
  background: var(--brand-primary);
  color: var(--brand-body-bg);
  text-align: left;
  padding: 8px 12px;
  font-weight: 700;
  font-size: 10pt;
}}
td {{
  padding: 8px 12px;
  border-bottom: 1px solid #E5E7EB;
  vertical-align: top;
}}
tr:nth-child(odd) td {{ background: var(--brand-highlight); }}
tr:nth-child(even) td {{ background: var(--brand-panel-bg); }}

.kpi-card {{
  background: var(--brand-panel-bg);
  border-left: 3px solid var(--brand-accent);
  padding: 1rem 1.25rem;
  margin: 0.5rem 0;
}}

.figure-caption {{
  color: var(--brand-muted);
  font-size: 9pt;
  font-style: italic;
  margin: 0.3rem 0 1.5rem;
}}

footer {{
  margin: 4rem 0 0;
  padding: 1rem 0;
  border-top: 1px solid var(--brand-subtle);
  font-size: 8.5pt;
  color: var(--brand-subtle);
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}}
"""


_SECTION_LABEL_RE = re.compile(r"^\s*(\d{1,2})\s*[\/·.\-]\s*(.+?)\s*$")


def _tag_section_labels(html_body: str) -> str:
    """Find <p>NN / TITLE</p> paragraphs and convert them to <p class="section-label">.
    Operates on the raw HTML body string."""
    def repl(m: re.Match) -> str:
        inner = m.group(1).strip()
        if _SECTION_LABEL_RE.match(inner):
            return f'<p class="section-label">{inner}</p>'
        return m.group(0)
    return re.sub(r"<p>([^<]+)</p>", repl, html_body)


def _extract_body_inner(html_text: str) -> str:
    """Strip outer <!doctype>, <html>, <head>, <body> wrappers if present."""
    m = re.search(r"<body[^>]*>(.*?)</body>", html_text, re.S | re.I)
    if m:
        return m.group(1)
    return html_text


def _cover_html(brand: Brand, brief_meta: dict) -> str:
    if not brief_meta or not brief_meta.get("title"):
        return ""

    title = escape(brief_meta.get("title", ""))
    subtitle = escape(brief_meta.get("subtitle", ""))
    org_name = escape(brand.config.get("organization_name", ""))

    eyebrow_template = brand.config.get("cover_eyebrow_template", "{document_kind_label}")
    document_kind_label = brief_meta.get("document_kind_label",
                                         brand.config.get("default_document_kind_label", "DOKUMENTUM"))
    brief_code = brief_meta.get("brief_code", "")
    try:
        eyebrow_text = eyebrow_template.format(
            document_kind_label=document_kind_label, brief_code=brief_code,
        ).strip(" ·")
    except (KeyError, IndexError):
        eyebrow_text = document_kind_label
    eyebrow_text = escape(eyebrow_text.upper())

    sub_eyebrow_html = ""
    if brief_meta.get("sub_eyebrow"):
        sub_eyebrow_html = (
            f'<p class="sub-eyebrow">{escape(brief_meta["sub_eyebrow"].upper())}</p>'
        )

    lead_html = f'<p class="lead">{subtitle}</p>' if subtitle else ""

    meta_html = ""
    if brand.motifs.cover_meta_block:
        fields = brand.config.get("cover_meta_fields",
                                  ["DÁTUM", "KOCKÁZATI SZINT", "HORIZONT", "DOKUMENTUM"])
        meta_values = brief_meta.get("meta_values", {})
        cells = []
        for f in fields:
            label = escape(f.upper())
            value = escape(str(meta_values.get(f, f"[{f.upper()}]")))
            cells.append(
                f'<div><div class="label">{label}</div>'
                f'<div class="value">{value}</div></div>'
            )
        meta_html = f'<div class="meta-block">{"".join(cells)}</div>'

    return f"""
<section class="cover">
  <p class="org">{org_name}</p>
  <p class="eyebrow-orange">{eyebrow_text}</p>
  {sub_eyebrow_html}
  <h1>{title}</h1>
  {lead_html}
  <hr class="gold-rule">
  {meta_html}
</section>
"""


def _footer_html(brand: Brand, brief_meta: Optional[dict]) -> str:
    document_label = ""
    if brief_meta:
        document_label = (brief_meta.get("document_label")
                          or brief_meta.get("brief_code")
                          or "")
    if not document_label:
        return ""
    return f"<footer>{escape(document_label)}</footer>"


def _eyebrow_bar_html(brand: Brand, has_cover: bool) -> str:
    if has_cover:
        return ""  # already in the cover
    org = brand.config.get("organization_name", "")
    if not org or not brand.motifs.inner_top_eyebrow:
        return ""
    return f'<div class="eyebrow-bar"><p class="eyebrow">{escape(org)}</p></div>'


def _extract_docx_images(docx_path: Path) -> dict[str, bytes]:
    """Read all media files from a DOCX zip's word/media/ folder."""
    import zipfile
    images: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(str(docx_path)) as z:
            for name in z.namelist():
                if name.startswith("word/media/") and not name.endswith("/"):
                    images[Path(name).name] = z.read(name)
    except (zipfile.BadZipFile, KeyError):
        pass
    return images


def _inline_images(html: str, images: dict[str, bytes]) -> str:
    """Replace <img src="...filename..."> with self-contained data: URIs.
    Makes the resulting HTML portable (single file, no media dependencies)."""
    import base64

    mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml",
                "bmp": "image/bmp"}

    def repl(m: re.Match) -> str:
        src = m.group(1)
        name = Path(src).name
        if name not in images:
            return m.group(0)
        data = images[name]
        ext = Path(name).suffix.lower().lstrip(".") or "png"
        mime = mime_map.get(ext, "image/png")
        b64 = base64.b64encode(data).decode()
        return f'<img src="data:{mime};base64,{b64}"'

    return re.sub(r'<img\s+src="([^"]+)"', repl, html)


async def _get_html_body(src: Path) -> str:
    """Convert source (DOCX/MD/HTML) into HTML body content with inlined images."""
    suffix = src.suffix.lower().lstrip(".")
    if suffix in ("html", "htm"):
        return _extract_body_inner(src.read_text(encoding="utf-8"))
    if suffix in ("docx", "doc", "odt", "rtf", "md"):
        import pypandoc
        body = pypandoc.convert_file(
            str(src), "html",
            extra_args=["--no-highlight", "--wrap=none"],
        )
        # Inline DOCX media so the result is self-contained
        if suffix == "docx":
            images = _extract_docx_images(src)
            if images:
                body = _inline_images(body, images)
        return body
    raise ValueError(f"can't generate HTML from {suffix!r}")


async def apply_brand_to_html(
    file_id: str, brand: Brand, brief_meta: Optional[dict] = None,
) -> dict:
    """Generate a branded HTML page from the source. Source can be DOCX, MD,
    or already HTML. Produces a self-contained HTML file with inline CSS so
    downstream conversions (HTML→PDF via WeasyPrint, HTML→DOCX via pandoc)
    pick up the brand styling."""
    src = _resolve(file_id)
    t0 = time.time()

    body = await _get_html_body(src)
    body = _tag_section_labels(body)

    has_cover = bool(brief_meta and brief_meta.get("title"))
    css = _brand_css(brand)
    cover = _cover_html(brand, brief_meta or {})
    eyebrow_bar = _eyebrow_bar_html(brand, has_cover)
    footer = _footer_html(brand, brief_meta)

    page_title = ""
    if brief_meta:
        page_title = brief_meta.get("title", "")
    if not page_title:
        page_title = brand.label

    html = f"""<!DOCTYPE html>
<html lang="hu">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(page_title)}</title>
<style>{css}</style>
</head>
<body>
{cover}
<div class="page">
{eyebrow_bar}
{body}
{footer}
</div>
</body>
</html>
"""

    fid = new_file_id("html")
    dst = output_path(fid)
    dst.write_text(html, encoding="utf-8")
    return {
        "file_id": fid,
        "url": public_url(fid),
        "brand": brand.name,
        "size_kb": dst.stat().st_size // 1024,
        "ms_elapsed": int((time.time() - t0) * 1000),
    }
