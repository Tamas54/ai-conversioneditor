"""
Brand catalog — format-agnostic visual identity definitions.

A Brand declares colors, fonts, and design motifs. Renderers in
brands/renderers/{pptx,docx,html,pdf}.py know how to apply a Brand to
their respective format. This way one brand spec drives consistent output
across all document kinds.
"""
from app.brands.base import Brand, BrandColors, BrandFonts, BrandMotifs
from app.brands.registry import BRANDS, get_brand, list_brands

__all__ = [
    "Brand",
    "BrandColors",
    "BrandFonts",
    "BrandMotifs",
    "BRANDS",
    "get_brand",
    "list_brands",
]
