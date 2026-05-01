"""
Brand registry — lookup table for available brands.

Add new brands here. Renderers (and agent tools) reference brands by name.
"""
from __future__ import annotations

from typing import Optional

from app.brands.base import Brand
from app.brands.hni import HNI_POLICY_BRIEF


BRANDS: dict[str, Brand] = {
    HNI_POLICY_BRIEF.name: HNI_POLICY_BRIEF,
}


def get_brand(name: str) -> Optional[Brand]:
    return BRANDS.get(name)


def list_brands() -> list[dict]:
    """Compact list for the agent's brand_list tool."""
    return [
        {
            "name": b.name,
            "label": b.label,
            "description": b.description,
            "document_kind": b.document_kind,
            "primary_color": b.colors.primary,
            "accent_color": b.colors.accent,
        }
        for b in BRANDS.values()
    ]
