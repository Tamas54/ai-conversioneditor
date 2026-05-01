"""
Visual chart redraw — Tier-S #6.

Read an existing chart image via Kimi K2.6 vision, extract structured data
(type, axes, categories, series, values), and re-render it via matplotlib
with a different palette / brand. Useful when a chart in a doc has the wrong
brand colors, or when you want to apply your house style across docs.

Pipeline:
  1. Kimi K2.6 vision: read chart → strict JSON {type, title, x_label,
     y_label, categories[], series[]}
  2. Optionally swap palette (explicit list, or from brand catalog)
  3. chart_image.generate_chart_image() → fresh PNG with new colors
  4. Return new image_file_id

Note: this does NOT yet replace the chart inside a DOCX/PPTX — that's a
separate insert_image step the agent does after seeing the new image.
Doing in-place chart replacement requires reaching into OOXML <pic:pic>
elements with their relationships, which is out of scope for v1.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from app.brands import get_brand
from app.llm import vision as v
from app.storage import input_path, output_path
from app.tools import chart_image
from app.tools.formats import detect_from_path

log = logging.getLogger("aice.chart_redraw")

IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif", "bmp"}


def _resolve_image(file_id: str) -> Path:
    p = output_path(file_id)
    if not p.exists():
        p = input_path(file_id)
    if not p.exists():
        raise FileNotFoundError(f"file_id not found: {file_id}")
    if detect_from_path(p) not in IMAGE_EXTS:
        raise ValueError(f"file_id is not an image (got {p.suffix})")
    return p


_EXTRACT_PROMPT = """\
Read this chart image and extract its structure as STRICT JSON.

Output ONLY this JSON object (no prose, no markdown fences):
{
  "chart_type": "column" | "bar" | "line" | "line_markers" | "area" | "pie" | "doughnut" | "scatter",
  "title": "string or null",
  "x_label": "string or null",
  "y_label": "string or null",
  "categories": ["category1", "category2", ...],
  "series": [
    {"name": "Series name", "values": [number, number, ...]}
  ],
  "value_format": "integer" | "percent" | "currency" | "decimal",
  "notes": "any uncertainty about values"
}

Rules:
- chart_type: pick the closest match. If horizontal bars, use "bar". If vertical bars, "column". For pie with hole, "doughnut".
- categories must be in left-to-right (or top-to-bottom for "bar") order as drawn.
- For each series, values must be in the same order as categories.
- If you can't read a value precisely, give your best estimate and note it in `notes`.
- For pie/doughnut, there's always exactly ONE series, and `values` are the slice sizes.
- Strip percentage signs / currency from values — return raw numbers only.
"""


async def _read_chart_structure(image_path: Path) -> dict[str, Any]:
    """Use Kimi K2.6 vision to extract structured chart data."""
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": _EXTRACT_PROMPT},
        {"type": "image_url", "image_url": {"url": v.image_to_data_url(image_path)}},
    ]}]
    raw = await v._call(msgs, response_format={"type": "json_object"}, max_tokens=2048)

    # Strip fences if any
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)

    try:
        data = json.loads(s)
    except json.JSONDecodeError as e:
        raise ValueError(f"vision returned non-JSON: {e}; raw[:200]={raw[:200]}")

    # Validate required fields
    if "chart_type" not in data or "categories" not in data or "series" not in data:
        raise ValueError(f"vision missing required fields: keys={list(data)}")

    return data


def _palette_from_brand(brand_name: str) -> list[str]:
    """Pull brand palette: primary + accent + accent_alt + neutral grays."""
    b = get_brand(brand_name)
    if not b:
        raise ValueError(f"brand not found: {brand_name}")
    c = b.colors
    return [
        c.primary, c.accent,
        getattr(c, "accent_alt", None) or "#5D7A1A",
        getattr(c, "text_muted", None) or "#6B7280",
        getattr(c, "text_subtle", None) or "#AAB3C2",
        c.primary, c.accent,  # cycle
    ]


async def chart_redraw(
    image_file_id: str,
    *,
    palette: Optional[list[str]] = None,
    brand: Optional[str] = None,
    chart_type_override: Optional[str] = None,
    title_override: Optional[str] = None,
    width_px: int = 1600,
    height_px: int = 900,
    also_svg: bool = False,
) -> dict[str, Any]:
    """Read chart from image, re-render with new palette/brand.

    Args:
      image_file_id: source chart image
      palette: explicit hex color list (takes precedence over brand)
      brand: brand name from catalog (e.g. 'hni_policy_brief')
      chart_type_override: force a different chart type than detected
      title_override: replace the chart title
      width_px, height_px: output dimensions

    Returns:
      {
        image_file_id: ..., url: ..., size_bytes: ...,
        chart_type: detected_type,
        extracted_structure: {...},   # what Kimi read
        ms_elapsed: ...
      }
    """
    src = _resolve_image(image_file_id)
    t0 = time.time()

    # 1. Extract structure via vision
    structure = await _read_chart_structure(src)

    # 2. Resolve palette
    if palette is None and brand is not None:
        palette = _palette_from_brand(brand)

    # 3. Re-render
    chart_type = chart_type_override or structure["chart_type"]
    title = title_override if title_override is not None else structure.get("title")
    x_label = structure.get("x_label")
    y_label = structure.get("y_label")
    categories = structure["categories"]
    series = structure["series"]

    out = await chart_image.generate_chart_image(
        chart_type=chart_type,
        categories=categories,
        series=series,
        title=title,
        palette=palette,
        x_label=x_label,
        y_label=y_label,
        width_px=width_px,
        height_px=height_px,
        show_grid=True,
        show_data_labels=False,
        also_svg=also_svg,
    )

    result = {
        "image_file_id": out["image_file_id"],
        "url": out["url"],
        "size_bytes": out["size_bytes"],
        "chart_type": chart_type,
        "extracted_structure": structure,
        "ms_elapsed": int((time.time() - t0) * 1000),
    }
    if "svg_file_id" in out:
        result["svg_file_id"] = out["svg_file_id"]
        result["svg_url"] = out["svg_url"]
        result["svg_size_bytes"] = out["svg_size_bytes"]
    return result
