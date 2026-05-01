"""
Brand abstraction — colors, fonts, motifs.

A Brand is a pure-data definition of a visual identity. Renderers consume
it to produce concrete document chrome (cover slides, headers, accent bars,
section labels, footers, etc.) in their target format (PPTX, DOCX, HTML, PDF).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class BrandColors:
    """Color palette of a brand. All values are #RRGGBB hex strings."""
    primary: str            # main brand color (often used for cover bg)
    accent: str             # secondary accent (eyebrows, section labels, rules)
    body_bg: str = "#FFFFFF"
    panel_bg: str = "#F7F8FA"   # subtle panel/card backgrounds
    highlight_bg: str = "#FFF9EC"  # highlight blocks / cream callouts
    text_primary: str = "#0E1726"
    text_muted: str = "#6B7280"
    text_subtle: str = "#AAB3C2"


@dataclass(frozen=True)
class BrandFonts:
    """Typography of a brand. `_fallback` fields are used when the primary
    face isn't available in the rendering environment (most common with
    web/embedded fonts that aren't installed locally)."""
    heading: str
    body: str
    heading_fallback: str = "Times New Roman"
    body_fallback: str = "Calibri"
    eyebrow: Optional[str] = None  # all-caps small labels; defaults to body
    monospace: str = "Consolas"

    def heading_with_fallback(self) -> str:
        return f"{self.heading}, {self.heading_fallback}"

    def body_with_fallback(self) -> str:
        return f"{self.body}, {self.body_fallback}"

    def effective_eyebrow(self) -> str:
        return self.eyebrow or self.body


@dataclass(frozen=True)
class BrandMotifs:
    """Visual design motifs of a brand. Renderers interpret these as best
    they can in their format — e.g. accent_rule_thickness becomes a thin
    rectangle in PPTX, a border-bottom CSS rule in HTML, an underline
    paragraph in DOCX."""
    # Cover slide
    cover_dark_bg: bool = True            # cover uses primary color as bg
    cover_serif_headline: bool = True     # cover headline uses heading font
    cover_meta_block: bool = True         # 4-col meta block (DATE/RISK/HORIZON/DOC)
    # Inner pages
    inner_top_eyebrow: bool = True        # gray caps "ORG NAME" label
    section_label_format: str = "{number:02d} / {title}"
    section_label_caps: bool = True
    heading_underline: bool = True        # thin accent rule under H1
    accent_bar_position: str = "none"     # "left" | "top" | "none"
    footer_text_pattern: str = "{document_label} · {page}"
    page_numbers: bool = True


@dataclass(frozen=True)
class Brand:
    """Format-agnostic brand definition.
    Renderers in brands/renderers/* consume this to produce branded chrome
    in PPTX/DOCX/HTML/PDF."""
    name: str                    # machine-readable id, e.g. 'hni_policy_brief'
    label: str                   # human-readable, e.g. 'HNI Policy Brief'
    description: str             # 1-2 sentence summary
    document_kind: str           # 'policy_brief' | 'memo' | 'report' | ...
    colors: BrandColors
    fonts: BrandFonts
    motifs: BrandMotifs = field(default_factory=BrandMotifs)
    # Free-form per-brand config (e.g. cover meta-block field labels)
    config: dict = field(default_factory=dict)

    def to_summary(self) -> dict:
        """Compact dict view used by the agent's brand_describe tool."""
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "document_kind": self.document_kind,
            "colors": {
                "primary": self.colors.primary,
                "accent": self.colors.accent,
                "body_bg": self.colors.body_bg,
                "panel_bg": self.colors.panel_bg,
                "highlight_bg": self.colors.highlight_bg,
                "text_primary": self.colors.text_primary,
                "text_muted": self.colors.text_muted,
            },
            "fonts": {
                "heading": self.fonts.heading,
                "body": self.fonts.body,
                "eyebrow": self.fonts.effective_eyebrow(),
            },
            "motifs": {
                "cover_dark_bg": self.motifs.cover_dark_bg,
                "cover_serif_headline": self.motifs.cover_serif_headline,
                "cover_meta_block": self.motifs.cover_meta_block,
                "inner_top_eyebrow": self.motifs.inner_top_eyebrow,
                "section_label_format": self.motifs.section_label_format,
                "heading_underline": self.motifs.heading_underline,
                "accent_bar_position": self.motifs.accent_bar_position,
                "page_numbers": self.motifs.page_numbers,
            },
            "config": self.config,
        }
