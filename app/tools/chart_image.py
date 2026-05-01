"""
Render a chart from data into a PNG image (for embedding in DOCX/HTML/PDF).

For PPTX, prefer pptx_builder.pptx_add_chart_slide (native, editable).
This module is the cross-format alternative: matplotlib → PNG → insert_image.
"""
from __future__ import annotations

import io
import logging
import time
from pathlib import Path
from typing import Any, Optional

from app.storage import new_file_id, output_path, public_url

log = logging.getLogger("aice.chart_image")


_DEFAULT_PALETTE = ["#1F3A5F", "#C66A00", "#5D7A1A", "#8B1F1F",
                    "#7F8C8D", "#16A085"]


async def generate_chart_image(
    chart_type: str,
    categories: list[str],
    series: list[dict[str, Any]],
    *,
    title: Optional[str] = None,
    palette: Optional[list[str]] = None,
    x_label: Optional[str] = None,
    y_label: Optional[str] = None,
    width_px: int = 1600,
    height_px: int = 900,
    dpi: int = 200,
    show_grid: bool = True,
    show_data_labels: bool = False,
    legend_position: str = "best",
    also_svg: bool = False,
) -> dict[str, Any]:
    """Render a chart as PNG via matplotlib. Returns image_file_id ready for
    insert_image (DOCX) or HTML <img> embedding.

    chart_type ∈ {'column', 'bar', 'line', 'line_markers', 'area',
                  'pie', 'doughnut', 'scatter'}

    also_svg: when True, also writes a vector SVG copy alongside the PNG and
    returns its file_id under `svg_file_id`. Use SVG for HTML output (crisp
    at any zoom) or as a vector master to convert outside this tool. Note
    that python-docx's add_picture / our insert_image accept ONLY raster
    formats (PNG/JPG/BMP) — for DOCX embedding always use the PNG file_id.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    palette = palette or _DEFAULT_PALETTE
    t0 = time.time()

    fig, ax = plt.subplots(
        figsize=(width_px / dpi, height_px / dpi),
        dpi=dpi,
    )
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    n_series = len(series)
    n_cats = len(categories)
    x = np.arange(n_cats)

    if chart_type in ("column", "bar"):
        bar_width = 0.8 / max(1, n_series)
        for i, s in enumerate(series):
            color = palette[i % len(palette)]
            offset = (i - (n_series - 1) / 2) * bar_width
            values = s.get("values", [])
            if chart_type == "column":
                bars = ax.bar(x + offset, values, bar_width,
                              color=color, label=s.get("name"))
            else:  # bar (horizontal)
                bars = ax.barh(x + offset, values, bar_width,
                               color=color, label=s.get("name"))
            if show_data_labels:
                for bar, v in zip(bars, values):
                    if chart_type == "column":
                        ax.annotate(f"{v}", (bar.get_x() + bar.get_width() / 2, v),
                                    ha="center", va="bottom", fontsize=8)
                    else:
                        ax.annotate(f"{v}", (v, bar.get_y() + bar.get_height() / 2),
                                    ha="left", va="center", fontsize=8)
        if chart_type == "column":
            ax.set_xticks(x)
            ax.set_xticklabels(categories)
        else:
            ax.set_yticks(x)
            ax.set_yticklabels(categories)

    elif chart_type in ("line", "line_markers"):
        marker = "o" if chart_type == "line_markers" else None
        for i, s in enumerate(series):
            color = palette[i % len(palette)]
            ax.plot(x, s.get("values", []),
                    color=color, marker=marker, linewidth=2.2,
                    markersize=6 if marker else 0,
                    label=s.get("name"))
            if show_data_labels:
                for xi, v in zip(x, s.get("values", [])):
                    ax.annotate(f"{v}", (xi, v), textcoords="offset points",
                                xytext=(0, 6), ha="center", fontsize=8)
        ax.set_xticks(x)
        ax.set_xticklabels(categories)

    elif chart_type == "area":
        for i, s in enumerate(series):
            color = palette[i % len(palette)]
            ax.fill_between(x, s.get("values", []), color=color, alpha=0.55,
                            label=s.get("name"))
            ax.plot(x, s.get("values", []), color=color, linewidth=1.5)
        ax.set_xticks(x)
        ax.set_xticklabels(categories)

    elif chart_type in ("pie", "doughnut"):
        s = series[0] if series else {"values": []}
        wedges, texts, autotexts = ax.pie(
            s.get("values", []),
            labels=categories,
            colors=[palette[i % len(palette)] for i in range(n_cats)],
            autopct="%1.0f%%",
            startangle=90,
            wedgeprops=dict(width=0.45 if chart_type == "doughnut" else 1.0,
                            edgecolor="white", linewidth=2),
            textprops=dict(fontsize=10),
        )
        for at in autotexts:
            at.set_color("white")
            at.set_fontweight("bold")
        ax.set_aspect("equal")

    elif chart_type == "scatter":
        for i, s in enumerate(series):
            color = palette[i % len(palette)]
            xs = s.get("x") or list(range(len(s.get("values", []))))
            ys = s.get("values", [])
            ax.scatter(xs, ys, color=color, s=60, alpha=0.75, label=s.get("name"))

    else:
        raise ValueError(f"unsupported chart_type: {chart_type!r}")

    # Common formatting
    if title:
        ax.set_title(title, fontsize=13, fontweight="bold",
                     loc="left", pad=14, color="#0E1726")
    if x_label:
        ax.set_xlabel(x_label, fontsize=10)
    if y_label:
        ax.set_ylabel(y_label, fontsize=10)

    if chart_type not in ("pie", "doughnut"):
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_color("#AAB3C2")
        ax.spines["bottom"].set_color("#AAB3C2")
        ax.tick_params(colors="#6B7280", labelsize=9)
        if show_grid:
            ax.grid(axis="y" if chart_type in ("column", "line", "line_markers", "area") else "x",
                    color="#E5E7EB", linewidth=0.7, alpha=0.8)
            ax.set_axisbelow(True)

    if n_series > 1 and chart_type not in ("pie", "doughnut"):
        ax.legend(loc=legend_position, frameon=False, fontsize=9)

    plt.tight_layout()

    # Always produce PNG (insert_image / OOXML accept only raster).
    png_buf = io.BytesIO()
    fig.savefig(png_buf, format="png", dpi=dpi, bbox_inches="tight",
                facecolor="white")
    png_bytes = png_buf.getvalue()
    png_fid = new_file_id("png")
    output_path(png_fid).write_bytes(png_bytes)

    svg_fid: Optional[str] = None
    svg_size: Optional[int] = None
    if also_svg:
        svg_buf = io.BytesIO()
        # SVG is resolution-independent; matplotlib still respects bbox_inches.
        fig.savefig(svg_buf, format="svg", bbox_inches="tight",
                    facecolor="white")
        svg_bytes = svg_buf.getvalue()
        svg_fid = new_file_id("svg")
        output_path(svg_fid).write_bytes(svg_bytes)
        svg_size = len(svg_bytes)

    plt.close(fig)

    result = {
        "image_file_id": png_fid,
        "url": public_url(png_fid),
        "size_bytes": len(png_bytes),
        "chart_type": chart_type,
        "ms_elapsed": int((time.time() - t0) * 1000),
    }
    if svg_fid:
        result["svg_file_id"] = svg_fid
        result["svg_url"] = public_url(svg_fid)
        result["svg_size_bytes"] = svg_size
    return result
