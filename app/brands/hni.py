"""
HNI Policy Brief brand — first entry in the design catalog.

Source: extracted from /home/tamas1/Downloads/HNI_univerzalis_elemzesi_sablon.docx
(Harmonikus Növekedésért Intézet 'Univerzális elemzési sablon, HNI-TPL-01').

Visual language: editorial-grade policy brief.
  - Deep navy cover with serif headline, orange accent label
  - Inner pages: top eyebrow caps + 'NN / TITLE' orange section label,
    Georgia serif H1 with thin orange/gold underline, Aptos sans body
  - Footer: '{document_label} · {page}'
  - Tables: navy header + cream/light-gray row bands
"""
from __future__ import annotations

from app.brands.base import Brand, BrandColors, BrandFonts, BrandMotifs


HNI_POLICY_BRIEF = Brand(
    name="hni_policy_brief",
    label="HNI Policy Brief",
    description=(
        "Harmonikus Növekedésért Intézet editorial-grade policy brief brand. "
        "Deep navy + warm orange accents, Georgia serif headings + Aptos body. "
        "Use for analyses, scenarios, country/sector profiles, decision memos."
    ),
    document_kind="policy_brief",
    colors=BrandColors(
        primary="#0E1726",       # deep navy — covers, table headers
        accent="#C66A00",        # warm orange — eyebrows, section labels, H1 underline
        body_bg="#FFFFFF",
        panel_bg="#F7F8FA",      # subtle gray-blue for cards
        highlight_bg="#FFF9EC",  # cream — KPI / callout blocks
        text_primary="#0E1726",  # body text mirrors primary navy
        text_muted="#6B7280",    # eyebrows, captions
        text_subtle="#AAB3C2",   # rules, dividers, very faint accents
    ),
    fonts=BrandFonts(
        heading="Georgia",
        heading_fallback="Times New Roman",
        body="Aptos",
        body_fallback="Calibri",
        eyebrow="Aptos",
    ),
    motifs=BrandMotifs(
        cover_dark_bg=True,
        cover_serif_headline=True,
        cover_meta_block=True,
        inner_top_eyebrow=True,
        section_label_format="{number:02d} / {title}",
        section_label_caps=True,
        heading_underline=True,
        accent_bar_position="none",   # the eyebrow + label do the work; no bar needed
        footer_text_pattern="{document_label} · {page}",
        page_numbers=True,
    ),
    config={
        "organization_name": "HARMONIKUS NÖVEKEDÉSÉRT INTÉZET",
        "cover_meta_fields": ["DÁTUM", "KOCKÁZATI SZINT", "HORIZONT", "DOKUMENTUM"],
        "cover_eyebrow_template": "{document_kind_label} · {brief_code}",
        "default_document_kind_label": "ELEMZÉSI ANYAG",
    },
)
