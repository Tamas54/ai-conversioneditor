"""
Autonomous document-editing agent — tool-use loop with Kimi K2.6.

Architecture:
    user instruction + initial file_id
        →  agent loop:
              call LLM with tools
              for each returned tool_call:
                  - read tools: extract / count / read_section (don't change state)
                  - edit tools: find_replace / replace_heading / delete_section / ...
                                (return new file_id; agent's "current_fid" advances)
                  - finish: end loop, return summary
              feed tool results back as role:tool messages
        → final file_id + summary + trace + iterations

Why an agent loop instead of single-call planning:
    - Kimi can SEE results (matches_count, errors) and self-correct
    - Creative work: Kimi generates new text via repeated read → write cycles
    - Multi-step plans where step N depends on step N-1's outcome
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app import events
from app.config import settings
from app.storage import (
    input_path,
    output_path,
    public_url,
)
from app.brands import get_brand, list_brands
from app.brands.extract import brand_extract as _brand_extract, brand_register_runtime
from app.brands.renderers import (
    pptx_renderer as brand_pptx_renderer,
    docx_renderer as brand_docx_renderer,
    html_renderer as brand_html_renderer,
)
from app.tools import chart_image
from app.tools import chart_redraw
from app.tools import docx_builder
from app.tools import docx_revisions
from app.tools import edits as edit_ops
from app.tools import legal_vertical
from app.tools import mtmt_integration
from app.tools import parser_self_healing
from app.tools import term_consistency
from app.tools import pptx_builder
from app.tools import vision_ops
from app.tools import visual_feedback
from app.tools import xlsx_builder
from app.tools.convert import convert as t_convert
from app.tools.formats import detect_from_path

log = logging.getLogger("aice.agent")

MAX_ITERS = 12
WALL_BUDGET_S = 120
DOC_PREVIEW_CHARS = 6000  # initial preview shown in user message


# ============================================================
# Tool schemas (OpenAI function-calling format)
# ============================================================

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": (
                "Read the FULL plain-text content of the working document "
                "(reflects all edits so far). Always call this first to see "
                "the current state."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "count_matches",
            "description": (
                "Count how many times `pattern` appears in the document. "
                "Useful before find_replace to verify the pattern is unique "
                "or to see if it appears at all."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "regex": {"type": "boolean", "default": False},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_replace",
            "description": (
                "Replace EXACT substring occurrences in the document. "
                "Returns matches_count. If 0, the pattern was not found "
                "verbatim — read_document and try a different pattern. "
                "For PPTX, this affects slide body text and notes (NOT slide "
                "structural markers like '### Slide N:')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Exact text (or regex if regex=true)."},
                    "replacement": {"type": "string", "description": "New text. Generate this yourself for creative edits."},
                    "regex": {"type": "boolean", "default": False},
                },
                "required": ["pattern", "replacement"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_heading",
            "description": (
                "Rename a heading (DOCX/MD) or slide title (PPTX). "
                "old_title must match exactly (trimmed)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "old_title": {"type": "string"},
                    "new_title": {"type": "string"},
                },
                "required": ["old_title", "new_title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_section",
            "description": (
                "Delete a section by heading title. "
                "DOCX/MD: removes content from this heading until the next same/higher-level heading. "
                "PPTX: removes the entire slide whose title matches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "level": {"type": "integer", "description": "Optional heading level (1-6); ignored for PPTX."},
                },
                "required": ["heading"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "insert_after",
            "description": (
                "Insert content after an anchor heading (DOCX/MD) or "
                "into the body of a PPTX slide whose title matches anchor. "
                "Use this to add new paragraphs or bullet points. "
                "For multi-line content, separate lines with '\\n'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "anchor": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["anchor", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_pages",
            "description": (
                "Delete pages (PDF) or slides (PPTX) by 1-based range, e.g. '3', '1-5', '1,3,5-7'."
            ),
            "parameters": {
                "type": "object",
                "properties": {"page_range": {"type": "string"}},
                "required": ["page_range"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "style_paragraph",
            "description": (
                "Apply visual formatting (color, highlight, bold, italic, font size) "
                "to every paragraph whose text matches `anchor`. DOCX only. "
                "Color examples: '#1F3A5F', 'darkblue', 'gold'. "
                "Highlight examples: 'yellow', 'green', 'cyan'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "anchor": {"type": "string"},
                    "color": {"type": "string", "description": "Text color, hex like '#1F3A5F' or named ('darkblue','gold','red'). REQUIRED if user mentions color."},
                    "highlight": {"type": "string", "description": "Highlight/background color name (yellow|green|cyan|red|blue|magenta)"},
                    "bold": {"type": "boolean"},
                    "italic": {"type": "boolean"},
                    "underline": {"type": "boolean"},
                    "font_size": {"type": "number", "description": "Point size, e.g. 14"},
                    "font_family": {"type": "string", "description": "e.g. 'Calibri', 'Arial'"},
                },
                "required": ["anchor"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "style_all_headings",
            "description": (
                "Apply visual formatting to ALL headings at once (DOCX). "
                "Optionally filter to specific levels (e.g. levels=[1,2])."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "levels": {"type": "array", "items": {"type": "integer"}, "description": "Optional list of heading levels (1=Heading 1, 2=Heading 2, ...). Omit to affect all headings."},
                    "color": {"type": "string", "description": "Text color, hex like '#1F3A5F' or named. Include if user mentions color."},
                    "highlight": {"type": "string"},
                    "bold": {"type": "boolean"},
                    "italic": {"type": "boolean"},
                    "underline": {"type": "boolean"},
                    "font_size": {"type": "number"},
                    "font_family": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "insert_image",
            "description": (
                "Insert an image into the working DOCX after the anchor paragraph. "
                "image_file_id must point to an already-uploaded image (PNG/JPG). "
                "Optional svg_file_id attaches a vector SVG layer alongside the "
                "PNG (Word 2016+ / LO 7+ render the SVG as vector, older readers "
                "fall back to the PNG). Use this when you have a `chart_redraw "
                "also_svg=true` result — pass image_file_id=PNG, svg_file_id=SVG "
                "and the chart stays crisp at any zoom and in PDF export."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "anchor": {"type": "string"},
                    "image_file_id": {"type": "string"},
                    "svg_file_id": {
                        "type": "string",
                        "description": "Optional vector SVG to attach as the primary render layer. PNG remains the fallback.",
                    },
                    "width_inches": {"type": "number", "description": "Optional rendered width in inches."},
                },
                "required": ["anchor", "image_file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "convert_format",
            "description": (
                "Convert the working document to another format "
                "(pdf|docx|md|html|odt|txt|rtf). Use this at the end if "
                "the user requested a specific output format different from the input."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_format": {"type": "string"},
                    "use_browser": {"type": "boolean", "default": False, "description": "For HTML→PDF, use Chromium instead of WeasyPrint."},
                },
                "required": ["target_format"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_chart_image",
            "description": (
                "Render a chart from data into a PNG (for embedding in DOCX/HTML/PDF "
                "via insert_image — for PPTX prefer the native pptx_add_chart_slide). "
                "Brand-aware default palette ['#1F3A5F','#C66A00',...]. Supports "
                "column/bar/line/line_markers/area/pie/doughnut/scatter. "
                "Pass also_svg=true to ALSO produce a vector SVG copy "
                "(returned as svg_file_id) — useful for HTML output where SVG "
                "stays crisp at any zoom; PNG remains the embeddable format "
                "for DOCX since OOXML doesn't accept SVG natively."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chart_type": {"type": "string"},
                    "categories": {"type": "array", "items": {"type": "string"}},
                    "series": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "values": {"type": "array", "items": {"type": "number"}},
                            },
                            "required": ["name", "values"],
                        },
                    },
                    "title": {"type": "string"},
                    "palette": {"type": "array", "items": {"type": "string"}},
                    "x_label": {"type": "string"},
                    "y_label": {"type": "string"},
                    "show_data_labels": {"type": "boolean"},
                    "also_svg": {
                        "type": "boolean",
                        "description": "Also produce a vector SVG copy. Default false. Use when target is HTML / when user wants a vector master to download.",
                    },
                },
                "required": ["chart_type", "categories", "series"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "Generate an image from a text prompt via SiliconFlow image-gen "
                "(FLUX.2-pro by default). Returns image_file_id usable in "
                "insert_image / pptx_add_slide-with-image. Use for: missing "
                "figures, conceptual illustrations, decorative covers, icons, "
                "diagrams that don't need to be data-driven (use charts for those). "
                "Be specific in the prompt: subject, style ('flat vector', "
                "'editorial photography', 'minimalist line drawing'), color "
                "palette (mention brand colors), background ('on dark navy bg' or "
                "'transparent', though FLUX can't truly do transparent — say "
                "'isolated on white' instead)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "size": {"type": "string", "enum": ["1024x1024", "1792x1024", "1024x1792"]},
                    "model": {
                        "type": "string",
                        "description": "Default FLUX.2-pro. Alternatives: 'black-forest-labs/FLUX.1-schnell' (faster), 'Qwen/Qwen-Image'.",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ocr_image",
            "description": (
                "Read all visible text from an image (handwriting OK). "
                "image_file_id must point to an uploaded image (PNG/JPG/...). "
                "Returns the transcribed text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_file_id": {"type": "string"},
                    "language_hint": {"type": "string", "description": "Optional: 'Hungarian', 'English', etc."},
                },
                "required": ["image_file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_image",
            "description": (
                "Free-form vision Q&A on an image. Use for: 'is there a logo?', "
                "'what kind of chart is this?', 'who is in the picture?', etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_file_id": {"type": "string"},
                    "question": {"type": "string", "description": "What to ask about the image."},
                },
                "required": ["image_file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_table_from_image",
            "description": (
                "Vision-extract a table from an image. Returns {headers, rows} "
                "as structured data. Useful before image_to_xlsx if you want to "
                "review or transform the data first."
            ),
            "parameters": {
                "type": "object",
                "properties": {"image_file_id": {"type": "string"}},
                "required": ["image_file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "image_to_xlsx",
            "description": (
                "Convert a photo of a table into an Excel (.xlsx) file. "
                "Vision-extracts the table, then writes openpyxl xlsx. "
                "Returns the new xlsx file_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_file_id": {"type": "string"},
                    "sheet_name": {"type": "string"},
                },
                "required": ["image_file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pptx_plan",
            "description": (
                "MANDATORY first step when building a deck from a document. "
                "Commit to a slide structure BEFORE creating anything. "
                "Each entry: {title, content_type ∈ ['bullets','content','table'], "
                "and the relevant data (key_points / headers+rows)}. "
                "Returns the plan back as a checklist; then you build slide-by-slide."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "deck_title": {"type": "string"},
                    "deck_subtitle": {"type": "string"},
                    "slides": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "content_type": {"type": "string", "enum": ["bullets", "content", "table"]},
                                "key_points": {"description": "string for content / list[str] for bullets"},
                                "headers": {"type": "array", "items": {"type": "string"}, "description": "for table content_type"},
                                "rows": {"type": "array", "description": "for table content_type"},
                            },
                            "required": ["title", "content_type"],
                        },
                    },
                },
                "required": ["deck_title", "slides"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pptx_add_table_slide",
            "description": (
                "Add a slide with a real PPTX table shape (NOT body-text). Use "
                "this when comparing values across categories (e.g. scenarios × "
                "indicators). Auto-styled header row + alternating row bands."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "headers": {"type": "array", "items": {"type": "string"}},
                    "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                    "header_color": {"type": "string", "description": "hex/named, default darkblue"},
                },
                "required": ["title", "headers", "rows"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pptx_apply_theme",
            "description": (
                "Apply a unified visual theme across the deck: accent bar (left "
                "or top), footer text, page numbers, cohesive title/body colors "
                "and font. Cover slide stays clean (no chrome). Call this near "
                "the END of building, after all slides are added."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "accent_color": {"type": "string", "description": "hex/named, e.g. '#1F3A5F' or 'darkblue'"},
                    "accent_position": {"type": "string", "enum": ["left", "top", "none"]},
                    "footer_text": {"type": "string", "description": "Optional text shown bottom-left on every non-cover slide (e.g. organization name + brief number)"},
                    "page_numbers": {"type": "boolean"},
                    "title_color": {"type": "string"},
                    "body_color": {"type": "string"},
                    "font_family": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pptx_render_slide",
            "description": (
                "Render PPTX slide(s) to PNG so you can VISUALLY verify the layout "
                "with describe_image. Pass slide_index=N to render one slide, or "
                "omit to render all slides. Returns image_file_id(s) you can pass "
                "to describe_image to check for overflow / readability / balance."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slide_index": {"type": "integer", "description": "0-based; omit to render all."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pptx_create",
            "description": (
                "Create a brand-new PPTX presentation with a title slide. "
                "Returns the new file_id. Use this as the FIRST step when "
                "building a deck from scratch (e.g. when converting a doc/PDF "
                "to slides — you read the source first, then BUILD slides, "
                "you don't 'convert' to PPTX with convert_format)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "subtitle": {"type": "string"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pptx_add_slide",
            "description": (
                "Add a slide to an existing PPTX. Use this iteratively to build "
                "a deck slide by slide. The body text uses '\\n' to separate "
                "paragraphs/bullet points. Layouts: 'title_content' (default — "
                "title + body), 'section_header' (large divider title), "
                "'title_only', 'two_column'. Returns updated file_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "string", "description": "Slide body text. Multi-line → multiple paragraphs."},
                    "layout": {"type": "string", "enum": ["title", "title_content", "section_header", "two_column", "title_only"]},
                },
                "required": ["file_id", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pptx_add_bullets_slide",
            "description": (
                "Convenience: add a slide with title + bullet list. "
                "Each item in the bullets array becomes its own bullet point."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "title": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["file_id", "title", "bullets"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pptx_add_chart_slide",
            "description": (
                "Add a slide with a NATIVE PPTX chart (editable in PowerPoint). "
                "Use for visual data: trend lines, scenario comparisons, market shares. "
                "chart_type ∈ {'column', 'bar', 'line', 'pie', 'doughnut', 'area', "
                "'scatter', 'column_stacked', 'bar_stacked', 'line_markers'}. "
                "categories = x-axis labels (e.g. ['S1','S2','S3','S4']); "
                "series = [{name, values:[...]}, ...] one entry per data series."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "chart_type": {"type": "string"},
                    "categories": {"type": "array", "items": {"type": "string"}},
                    "series": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "values": {"type": "array", "items": {"type": "number"}},
                            },
                            "required": ["name", "values"],
                        },
                    },
                    "legend": {"type": "boolean", "default": True},
                    "legend_position": {"type": "string", "enum": ["top", "bottom", "left", "right"]},
                    "palette": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional series colors (hex). For HNI brand use ['#1F3A5F','#C66A00','#6B7280'].",
                    },
                    "title_axis_x": {"type": "string"},
                    "title_axis_y": {"type": "string"},
                    "show_data_labels": {"type": "boolean"},
                },
                "required": ["title", "chart_type", "categories", "series"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pptx_modify_textbox",
            "description": (
                "Find a textbox/shape on a PPTX slide by partial text match "
                "and modify its style and/or geometry — without rebuilding "
                "the slide. CRITICAL for the visual self-review loop: when "
                "a render shows text overlap, sizing problems, or colors "
                "off-brand, use this to surgically fix the offending textbox. "
                "Works on any custom-placed textbox (not just placeholders)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "find_text": {"type": "string", "description": "Substring to match (case-sensitive). First match wins."},
                    "slide_index": {"type": "integer", "description": "Limit to one slide (omit for all)"},
                    "new_text": {"type": "string", "description": "Replace entire shape text (use \\n for line breaks)"},
                    "font_size_pt": {"type": "number"},
                    "bold": {"type": "boolean"},
                    "italic": {"type": "boolean"},
                    "font_color": {"type": "string", "description": "Hex like '#FFFFFF'"},
                    "left_inches": {"type": "number"},
                    "top_inches": {"type": "number"},
                    "width_inches": {"type": "number"},
                    "height_inches": {"type": "number"},
                },
                "required": ["find_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pptx_set_slide_styles",
            "description": (
                "Apply visual styling to one slide (slide_index, 0-based) or "
                "to ALL slides (slide_index omitted). Pass only the parameters "
                "you want to set. Use to give the deck a cohesive look (e.g. "
                "all titles dark blue bold, body in Calibri)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "slide_index": {"type": "integer", "description": "0-based; omit to style all slides."},
                    "title_color": {"type": "string"},
                    "body_color": {"type": "string"},
                    "title_font_size": {"type": "number"},
                    "body_font_size": {"type": "number"},
                    "font_family": {"type": "string"},
                    "title_bold": {"type": "boolean"},
                    "title_italic": {"type": "boolean"},
                },
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pptx_slide_count",
            "description": "Read how many slides a PPTX has and what their titles are. Quick verification step.",
            "parameters": {
                "type": "object",
                "properties": {"file_id": {"type": "string"}},
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pptx_check_placeholders",
            "description": (
                "Scan a deck for leftover placeholder text (xxxx, lorem ipsum, "
                "'Click to add title', '[organization name]', {{handlebars}}, etc.). "
                "Run this AFTER building or templating a deck — visual review misses "
                "text that looks plausible but is template boilerplate. Returns "
                "per-slide hits with matched substring and surrounding context. "
                "Pass extra_patterns to add domain-specific markers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "extra_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional extra regex strings (case-insensitive) to flag.",
                    },
                },
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "brand_list",
            "description": (
                "List available brands in the design catalog. Each brand defines "
                "colors, fonts, and visual motifs that renderers apply consistently "
                "across PPTX/DOCX/HTML/PDF. Call this when the user mentions a brand, "
                "an organization (HNI, etc.), or a 'branded' document."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "brand_describe",
            "description": (
                "Inspect a brand's full spec (colors, fonts, motifs, config). "
                "Use to understand what a brand will produce before applying."
            ),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    # ============ DOCX BUILDER ============
    {
        "type": "function",
        "function": {
            "name": "docx_create",
            "description": "Create a new DOCX. Optional centered bold title at top. Returns file_id; chain into docx_add_* calls.",
            "parameters": {"type": "object", "properties": {"title": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_add_heading",
            "description": "Add a heading (level 1/2/3 → Word's Heading N styles, brand-stylable).",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "level": {"type": "integer"},
                    "alignment": {"type": "string", "enum": ["left", "center", "right", "justify"]},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_add_paragraph",
            "description": "Add a body paragraph with optional formatting (bold, italic, alignment, font_size_pt, font_family, color hex).",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "bold": {"type": "boolean"},
                    "italic": {"type": "boolean"},
                    "alignment": {"type": "string", "enum": ["left", "center", "right", "justify"]},
                    "font_size_pt": {"type": "number"},
                    "font_family": {"type": "string"},
                    "color": {"type": "string"},
                    "indent_cm": {"type": "number"},
                    "space_after_pt": {"type": "number"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_add_form_field",
            "description": "Add 'Label: ........................' form line. Set dots count (default 80) for fill-in length.",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "dots": {"type": "integer"},
                },
                "required": ["label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_add_numbered_list",
            "description": "Add a numbered list ('1. item / 2. item').",
            "parameters": {
                "type": "object",
                "properties": {"items": {"type": "array", "items": {"type": "string"}}},
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_add_bulleted_list",
            "description": "Add a bulleted list.",
            "parameters": {
                "type": "object",
                "properties": {"items": {"type": "array", "items": {"type": "string"}}},
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_add_signature_block",
            "description": "Add a signature line + small italic caption (default right-aligned). Use at end of letters/declarations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "alignment": {"type": "string", "enum": ["left", "center", "right"]},
                    "dots": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_add_table",
            "description": "Add a styled DOCX table with bold colored header + alternating row bands.",
            "parameters": {
                "type": "object",
                "properties": {
                    "headers": {"type": "array", "items": {"type": "string"}},
                    "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                    "header_color": {"type": "string"},
                },
                "required": ["headers", "rows"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_add_page_break",
            "description": "Insert a page break.",
            "parameters": {"type": "object", "properties": {}},
        },
    },

    # ============ XLSX BUILDER ============
    {
        "type": "function",
        "function": {
            "name": "xlsx_create",
            "description": "Create a new XLSX workbook with one sheet. Returns file_id; chain into xlsx_* calls.",
            "parameters": {
                "type": "object",
                "properties": {"sheet_name": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "xlsx_add_sheet",
            "description": "Add another sheet to the workbook.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "position": {"type": "integer"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "xlsx_write_block",
            "description": (
                "Bulk-write a 2D data array starting at anchor cell. "
                "If 'headers' provided, written as the first row. Numeric strings "
                "like '12,5%' or '1 234' auto-coerce."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sheet": {"type": "string"},
                    "data": {"type": "array", "description": "2D array of cell values"},
                    "anchor": {"type": "string", "description": "e.g. 'A1', 'B3'"},
                    "headers": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["sheet", "data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "xlsx_set_formula",
            "description": (
                "Set a FORMULA in a cell — e.g. cell='E2', formula='SUM(B2:D2)' or "
                "'B2*C2'. The leading '=' is added if missing. Use this instead of "
                "hardcoded calculated values whenever possible."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sheet": {"type": "string"},
                    "cell": {"type": "string"},
                    "formula": {"type": "string"},
                    "number_format": {"type": "string", "description": "e.g. '#,##0', '0.0%', '#,##0.00 [$USD]'"},
                },
                "required": ["sheet", "cell", "formula"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "xlsx_apply_style",
            "description": "Apply font/fill/alignment/border/number-format to a range (e.g. 'A1:D1' for headers).",
            "parameters": {
                "type": "object",
                "properties": {
                    "sheet": {"type": "string"},
                    "range_ref": {"type": "string"},
                    "bold": {"type": "boolean"},
                    "italic": {"type": "boolean"},
                    "font_size": {"type": "number"},
                    "font_family": {"type": "string"},
                    "font_color": {"type": "string"},
                    "fill_color": {"type": "string"},
                    "horizontal": {"type": "string", "enum": ["left", "center", "right"]},
                    "vertical": {"type": "string", "enum": ["top", "center", "bottom"]},
                    "number_format": {"type": "string"},
                    "border_all": {"type": "string"},
                    "wrap_text": {"type": "boolean"},
                },
                "required": ["sheet", "range_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "xlsx_merge_cells",
            "description": "Merge cells (e.g. 'A1:D1' for a centered title bar).",
            "parameters": {
                "type": "object",
                "properties": {"sheet": {"type": "string"}, "range_ref": {"type": "string"}},
                "required": ["sheet", "range_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "xlsx_set_column_widths",
            "description": "Set column widths, e.g. {'A': 18, 'B': 12}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sheet": {"type": "string"},
                    "widths": {"type": "object"},
                },
                "required": ["sheet", "widths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "xlsx_freeze_panes",
            "description": "Freeze rows/columns above/left of anchor (e.g. 'B2' freezes row 1 + column A).",
            "parameters": {
                "type": "object",
                "properties": {"sheet": {"type": "string"}, "anchor": {"type": "string"}},
                "required": ["sheet"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "xlsx_inspect",
            "description": "Read back the workbook contents (cells + formulas + merges) — verification step before finish.",
            "parameters": {
                "type": "object",
                "properties": {"sheet": {"type": "string"}, "max_rows": {"type": "integer"}, "max_cols": {"type": "integer"}},
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "brand_extract",
            "description": (
                "Analyze an arbitrary brand-foundation document (DOCX or PDF) "
                "and produce a draft Brand spec. Combines programmatic XML/PDF "
                "scan (color frequency, fonts, named styles) with vision "
                "description of rendered pages. Returns proposed_brand_spec dict "
                "ready for brand_register. Use when the user uploads a NEW brand "
                "template they want to add to the catalog."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "suggested_name": {"type": "string", "description": "lowercase_underscore identifier, e.g. 'acme_briefs'"},
                    "sample_pages": {"type": "integer", "default": 2},
                },
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "brand_register",
            "description": (
                "Register a brand spec dict in the runtime catalog. Use after "
                "brand_extract + agent review/edit of the proposed spec. The "
                "registered brand becomes immediately usable via brand_apply. "
                "NOTE: runtime-only registration; for persistence the spec should "
                "be saved to app/brands/<name>.py."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spec": {"type": "object", "description": "Brand spec dict from brand_extract.proposed_brand_spec"},
                },
                "required": ["spec"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_render_pages",
            "description": (
                "Render DOCX pages as PNG images for visual verification. "
                "Use after structural edits to check page breaks, overflow, "
                "orphan/widow lines, layout balance — pair with describe_image."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "page_indices": {"type": "array", "items": {"type": "integer"}, "description": "0-based; omit to render all"},
                    "dpi": {"type": "integer", "default": 110},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "html_render_screenshot",
            "description": "Render HTML as a full-page PNG screenshot via headless Chromium.",
            "parameters": {
                "type": "object",
                "properties": {
                    "viewport_width": {"type": "integer", "default": 1024},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_set_paragraph_format",
            "description": (
                "Apply DOCX paragraph flow controls to micro-fix layout issues "
                "detected via render+describe loop. Use to: force a heading "
                "onto a new page (page_break_before=true), keep a heading with "
                "the next paragraph (keep_with_next=true), prevent paragraph "
                "splitting (keep_lines_together=true), or adjust spacing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "anchor": {"type": "string", "description": "Exact paragraph text to target"},
                    "page_break_before": {"type": "boolean"},
                    "keep_with_next": {"type": "boolean"},
                    "keep_lines_together": {"type": "boolean"},
                    "space_before_pt": {"type": "number"},
                    "space_after_pt": {"type": "number"},
                },
                "required": ["anchor"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "brand_apply",
            "description": (
                "Apply a brand from the catalog to the current document. Auto-detects "
                "format. For policy-brief style brands on a PPTX deck, this rerenders "
                "the cover (dark bg + serif headline + accent eyebrow + meta block) "
                "and applies inner-page chrome (top eyebrow, NN/SECTION label, heading "
                "underline, footer). Pass brief_meta with title, subtitle, brief_code, "
                "document_kind_label, sub_eyebrow, meta_values dict, and optionally "
                "section_titles[] aligned with slide indices."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "brand": {"type": "string", "description": "Brand name (e.g. 'hni_policy_brief')"},
                    "brief_meta": {
                        "type": "object",
                        "description": "Cover and chrome content for the brand renderer",
                        "properties": {
                            "title": {"type": "string"},
                            "subtitle": {"type": "string"},
                            "brief_code": {"type": "string", "description": "e.g. 'HNI-PB-2026-04'"},
                            "document_kind_label": {"type": "string", "description": "e.g. 'POLICY BRIEF'"},
                            "sub_eyebrow": {"type": "string", "description": "e.g. 'Európai energiapiac · 2026 Q2'"},
                            "meta_values": {
                                "type": "object",
                                "description": "Cover meta-block values keyed by field label, e.g. {DÁTUM: '2026.04.23.', KOCKÁZATI SZINT: 'MAGAS', HORIZONT: '90 nap', DOKUMENTUM: 'HNI-PB-2026-04'}",
                            },
                            "section_titles": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Optional per-slide section titles (idx-aligned including cover)",
                            },
                            "document_label": {"type": "string", "description": "Footer label (defaults to brief_code)"},
                        },
                    },
                },
                "required": ["brand"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_track_replace_paragraph",
            "description": (
                "Replace a paragraph's text as a TRACKED CHANGE — original "
                "becomes <w:del>, new text becomes <w:ins>. Word's review pane "
                "will show this as a redline edit the human reviewer can "
                "accept/reject. Use for legal review, academic peer review, "
                "two-author collaboration where the agent's edits must be "
                "auditable. `anchor` is the EXACT current paragraph text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "anchor": {"type": "string", "description": "Exact text of paragraph to replace"},
                    "new_text": {"type": "string", "description": "Proposed replacement text"},
                    "author": {"type": "string", "description": "Author name shown in Word's review pane (default 'Kimi K2.6')"},
                },
                "required": ["anchor", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_track_insert_after",
            "description": (
                "Insert a new paragraph immediately after `anchor`, marked as "
                "<w:ins> tracked-change. The reviewer will see it underlined "
                "in the review pane and can accept or reject. Style is copied "
                "from the anchor paragraph for visual continuity."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "anchor": {"type": "string", "description": "Exact text of paragraph to insert after"},
                    "new_text": {"type": "string"},
                    "author": {"type": "string"},
                },
                "required": ["anchor", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_track_delete_paragraph",
            "description": (
                "Mark a paragraph for tracked deletion (<w:del>). Word's review "
                "pane shows it strikethrough — reviewer can accept (final "
                "removal) or reject (restore)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "anchor": {"type": "string", "description": "Exact text of paragraph to delete"},
                    "author": {"type": "string"},
                },
                "required": ["anchor"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_accept_all_revisions",
            "description": (
                "Collapse all tracked changes: insertions become permanent text, "
                "deletions are removed. Use after a human reviewer has signed off "
                "on all edits, or for fully automated pipelines."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_reject_all_revisions",
            "description": (
                "Reject all tracked changes: insertions are removed, deletions "
                "are restored. Resets the document to its pre-revision state."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_list_revisions",
            "description": (
                "Enumerate every <w:ins> and <w:del> in the document with id, "
                "author, date, and text snippet. Use to audit pending tracked "
                "changes before deciding to accept/reject."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_add_comment",
            "description": (
                "Anchor a Word comment to a specific paragraph. Comment "
                "appears in the right margin of Word/LibreOffice. Use to "
                "leave review remarks, explain proposed edits, or flag "
                "concerns for the human reviewer without modifying the text. "
                "Manages word/comments.xml + Content_Types + relationships "
                "automatically — works even if this is the first comment in "
                "the doc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "anchor": {"type": "string", "description": "Exact text of paragraph to attach comment to"},
                    "comment_text": {"type": "string"},
                    "author": {"type": "string"},
                    "initials": {"type": "string", "description": "Author initials shown in the review pane"},
                },
                "required": ["anchor", "comment_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_list_comments",
            "description": (
                "Enumerate all comments in the document with id, author, "
                "initials, date, and text. Use to triage review feedback "
                "before responding."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_reply_to_comment",
            "description": (
                "Add a threaded reply to an existing comment. The reply is "
                "linked to the parent via commentsExtended.xml (w15:paraIdParent) "
                "so Word's review pane shows nested conversation. Use when "
                "responding to legal-review or peer-review feedback."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "parent_comment_id": {"type": "integer", "description": "ID from docx_list_comments"},
                    "reply_text": {"type": "string"},
                    "author": {"type": "string"},
                    "initials": {"type": "string"},
                },
                "required": ["parent_comment_id", "reply_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docx_resolve_comment",
            "description": (
                "Mark a comment thread as resolved (w15:done='1') or reopen "
                "it. Resolved threads display collapsed/struck-through in "
                "Word's review pane. Use after the discussion concludes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "comment_id": {"type": "integer"},
                    "resolved": {"type": "boolean", "description": "true=resolve, false=reopen (default true)"},
                },
                "required": ["comment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "legal_contract_extract_clauses",
            "description": (
                "PLACEHOLDER (not yet implemented): break a contract into "
                "atomic clauses by heading hierarchy + cross-reference "
                "numbering. Returns a stub response — full impl pending."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "legal_compare_to_template",
            "description": (
                "PLACEHOLDER (not yet implemented): diff a draft contract "
                "against a known-good template, flag deviations with severity."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "template_id": {"type": "string", "description": "file_id of the reference template"},
                },
                "required": ["template_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "legal_gdpr_review",
            "description": (
                "PLACEHOLDER (not yet implemented): scan doc for GDPR "
                "compliance — lawful basis, subject rights, retention, "
                "international transfers, DPO, breach notification."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "legal_aiact_review",
            "description": (
                "PLACEHOLDER (not yet implemented): scan doc/spec against "
                "EU AI Act — prohibited practices, high-risk obligations, "
                "GPAI duties, transparency requirements."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "do_task",
            "description": (
                "RECURSIVE SUB-AGENT. Spawn a fresh agent loop to solve a "
                "self-contained sub-goal. Use ONLY for genuinely "
                "decomposable tasks where a sub-agent makes sense — e.g. "
                "'process all charts in this PDF', 'reformat each section "
                "to match brand X'. Don't call for trivial steps that you "
                "could do directly. The sub-agent gets its own iteration "
                "budget (default 8) and tool set. Cannot recursively call "
                "do_task itself (max depth 1)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "The natural-language task for the sub-agent"},
                    "file_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Files the sub-agent will work with (first becomes primary)",
                    },
                    "max_iters": {"type": "integer", "description": "Sub-agent iteration budget (default 8)"},
                },
                "required": ["goal", "file_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_citations",
            "description": (
                "Extract structured academic citations from any document "
                "(PDF/DOCX/HTML/MD) via Kimi K2.6. Returns "
                "[{authors, title, year, journal, volume, issue, pages, "
                "doi, isbn, kind, raw}] with deduplication by "
                "first-author+year+title-prefix. Useful for bibliography "
                "audit, citation-style normalization, missing-field "
                "detection. Works WITHOUT MTMT."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mtmt_search_author",
            "description": (
                "PLACEHOLDER (MTMT REST API access pending). Look up authors "
                "by name in the Magyar Tudományos Művek Tára. Skeleton only — "
                "actual integration requires verified API endpoints."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mtmt_verify_citation",
            "description": (
                "PLACEHOLDER (MTMT REST API access pending). Verify a "
                "citation against an MTMT record."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "citation": {"type": "object", "description": "Citation record from extract_citations"},
                    "author_id": {"type": "string"},
                },
                "required": ["citation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "term_consistency_check",
            "description": (
                "Cross-document term-consistency check. Either pass an "
                "explicit glossary [{canonical, allowed_variants}] OR rely "
                "on auto_detect=true (default) to let Kimi K2.6 identify "
                "important terms (legal definitions, proper nouns, technical "
                "terms). Then scans all listed documents for surface-form "
                "variations and flags inconsistent usage. Use for legal "
                "review (Megrendelő/Vevő/Ügyfél consistency), translation "
                "QA (same source term → same target), brand-name "
                "capitalization (iPhone vs Iphone)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "One or more document file_ids to scan",
                    },
                    "glossary": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "canonical": {"type": "string"},
                                "allowed_variants": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "kind": {"type": "string"},
                            },
                            "required": ["canonical"],
                        },
                        "description": "Optional explicit term list",
                    },
                    "auto_detect": {"type": "boolean", "description": "If true and no glossary, Kimi auto-detects terms (default true)"},
                },
                "required": ["file_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chart_redraw",
            "description": (
                "Read an existing chart from an image (PNG/JPG) via Kimi K2.6 "
                "vision, extract its structure (type, axes, categories, "
                "series, values), and re-render it via matplotlib with a "
                "new palette or brand. Useful for re-coloring third-party "
                "charts to match the house brand, or fixing chart-type "
                "issues (e.g. switching column→line). Returns a new image "
                "file_id that can be inserted via insert_image. "
                "Pass also_svg=true to ALSO get a vector SVG copy "
                "(svg_file_id) — useful for HTML output."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_file_id": {"type": "string", "description": "Source chart image"},
                    "palette": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Hex colors for the new chart (takes precedence over brand)",
                    },
                    "brand": {"type": "string", "description": "Brand name from catalog (e.g. 'hni_policy_brief')"},
                    "chart_type_override": {"type": "string", "description": "Force a chart type (column/bar/line/area/pie/doughnut/scatter)"},
                    "title_override": {"type": "string"},
                    "width_px": {"type": "integer"},
                    "height_px": {"type": "integer"},
                    "also_svg": {
                        "type": "boolean",
                        "description": "Also produce a vector SVG copy alongside the PNG.",
                    },
                },
                "required": ["image_file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_with_quality",
            "description": (
                "Self-healing document parser. Tries native extraction first "
                "(pdfplumber for PDF, python-docx for DOCX, python-pptx for "
                "PPTX) — fast and free. Scores the result on text quality "
                "heuristics (char density, replacement chars, printable ratio). "
                "If quality is below threshold (typically a scan-only PDF or "
                "broken CMap), automatically falls back to Kimi K2.6 vision "
                "OCR per page. Returns whichever method scored higher with "
                "full provenance metadata. Use this INSTEAD of read_document "
                "when you suspect the document might have extraction issues "
                "(scan-quality PDF, weird formatting, special characters)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "force_vision": {"type": "boolean", "description": "Skip native parser, go straight to Kimi vision"},
                    "quality_threshold": {"type": "number", "description": "Minimum native-parser score to skip vision fallback (default 0.55, range 0..1)"},
                    "max_vision_pages": {"type": "integer", "description": "Cap vision OCR to N pages (omit for all)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "legal_term_consistency",
            "description": (
                "PLACEHOLDER (not yet implemented): flag inconsistent use "
                "of defined terms within a document (e.g. Megrendelő vs. "
                "Vevő used interchangeably)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "glossary_file_id": {"type": "string", "description": "Optional reference glossary"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Call this when the task is complete (or cannot be completed). "
                "Provide a 1–2 sentence summary of what you did."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "confidence": {"type": "number", "description": "0..1, how sure you are the task was completed correctly."},
                },
                "required": ["summary"],
            },
        },
    },
]


SYSTEM_PROMPT = """\
You are an autonomous document editor. The user gives you a natural-language
instruction and a document; you complete the task by calling tools step by step.

Workflow:
  1. Call read_document FIRST to see the current state.
  2. Plan the changes. For creative tasks (rewriting, generating new text,
     polishing tone, summarizing), YOU generate the new text and pass it as
     the `replacement` / `content` / `new_title` parameter.
  3. Call edit tools one by one. After EACH edit that reports
     matches_count == 0 or an error, re-read the document and try a different
     approach (different pattern, different anchor) — do not blindly retry.
  4. After all edits, optionally call read_document one more time to verify.
  5. Call finish(summary, confidence) when done.

ERROR RETRY POLICY (HARD RULE):
  • If a tool call returns an error, DO NOT call the same tool with the same
    arguments again — that always produces the same error. Instead:
       - try a different approach (different pattern/anchor/format), OR
       - if no alternative works, call finish() with a description of what
         could and couldn't be completed.
  • Counterexamples to AVOID: 3 identical convert_format(target='pptx') calls
    that all return the same "no filter" error. After the FIRST failure of a
    given tool+args combo, switch strategy or give up.

VISUAL SELF-REVIEW LOOP (HARD RULE for PPTX/DOCX/HTML/PDF building):
  After ANY visual artifact build or edit, your work is NOT done until you
  have visually verified the output. Standard cycle:

    1. Make the change (build / edit / style)
    2. Render: pptx_render_slide / docx_render_pages / html_render_screenshot
    3. Self-review: describe_image on the rendered PNG with question like
       "Is there overlap, missing text, wrong alignment, or colors that
       don't match the brand? List any visual issues."
    4. If issues exist → fix them and GOTO step 2 (max 2 fix iterations)
    5. Then call finish()

  This applies whenever you build a cover, slide deck, document, or chart.
  Never claim a visual artifact is "done" without rendering + describe-checking
  it at least once. The render-and-verify pattern catches font overflow,
  textbox overlaps, missing images, brand-color mismatches that are invisible
  from inspecting the file alone.

TOOL CALL FORMAT:
  Use the structured tool_calls API field — DO NOT embed tool calls as text
  in the content field. Specifically: never write `<|tool_call_begin|>...`
  markers as plain text; use the proper function-calling channel.

Hard rules:
  • find_replace patterns must match the document text VERBATIM (no invented text).
  • For PPTX, the read_document output groups slides under "### Slide N: TITLE"
    markers — these markers are NOT part of the actual file content; never
    use them in find_replace patterns.
  • Prefer `replace_heading` over `find_replace` for headings/slide titles.
  • Prefer `delete_section` over `find_replace` to remove whole sections/slides.
  • Use `delete_pages` for PDF page ranges and PPTX slide indices.
  • Never invent file_ids or anchors that don't appear in the document.
  • You have a budget of about 10 tool calls; be efficient.

Styling rules (CRITICAL — follow these exactly):
  • When the user asks for COLOR + BOLD + SIZE + FONT in one go, pass ALL of
    them in a SINGLE tool call. e.g. style_all_headings(levels=[1], color='#1F3A5F',
    bold=true, font_size=14, font_family='Calibri') — not three separate calls.
  • Color values must be HEX strings like '#1F3A5F' or named ('darkblue', 'gold').
    Always include `color` if the user mentioned a color anywhere in the request.
  • If style_all_headings returned headings_styled >= 1, the formatting WAS
    applied. Do NOT also call style_paragraph for the same headings — that's
    redundant.
  • If a tool returns success (paragraphs_styled >= 1, file_id present,
    no error), DO NOT call the same tool with the same arguments again.
    Move on to the next thing or call finish().

Communicate via tool calls only — do not write user-visible prose; the user
sees your tool calls live on a dashboard. End with finish().

Vision capabilities:
  • If the user references an uploaded image (its file_id ends in .png/.jpg/.webp),
    you can call ocr_image (read text), describe_image (free-form Q&A),
    extract_table_from_image (structured rows), or image_to_xlsx (table photo → Excel).
  • For "fotózott táblázatból Excel" or "kézírás → editable doc" tasks, you'll
    typically use ocr_image / image_to_xlsx and then write the result into a
    new or existing document via insert_after / find_replace.

PDF strategy (IMPORTANT — preserves layout):
  • find_replace works DIRECTLY on PDF (in-place text replace via PyMuPDF,
    keeping fonts/positions/colors). Use it for typo fixes and small text
    changes — NO round-trip needed, layout is preserved.
  • read_document and count_matches also work directly on PDF.
  • Other tools (replace_heading, delete_section, insert_after, style_paragraph,
    insert_image) DO NOT work on PDF natively. If the user asks for a structural
    edit on a PDF, you must first call convert_format(target_format='docx'),
    do the edits, then convert_format(target_format='pdf') at the end. This will
    cause some layout reflow — warn in your finish summary.

IMAGE-TO-DOCUMENT strategy (CRITICAL when source is .jpg/.jpeg/.png/.webp/.bmp/.gif):
  When the user asks you to TRANSCRIBE, REPRODUCE, COPY, BUILD, or otherwise
  capture the image content into a real artifact (typical Hungarian verbs:
  "iratold le", "reprodukáld", "építsd meg", "csinálj egy <formátum>-et",
  "másold át <formátum>-be"), a real DOCUMENT must be produced — it is NOT
  enough to OCR/describe and only return the text in your finish summary.

  Required workflow:
    1. PARALLEL VISION READ — in ONE tool-calling round, call all needed
       READ-ONLY vision tools at once (parallelism is supported and ~halves
       wall time):
         • ocr_image — to capture the prose body text
         • extract_table_from_image — for any tabular data
         • chart_redraw — for any charts (extracts data + re-renders matplotlib).
           Pass also_svg=true: the rendered chart will be available as both
           PNG and SVG, and the SVG embed in DOCX makes the chart vector
           (crisp at any zoom, vector in PDF export). Always set also_svg=true
           when the target is DOCX or PDF.
         • describe_image — for layout/structure intuition (only if needed)
       DO NOT chain these one-by-one across iterations; issue them parallel.
       These tools READ the source image; they do not mutate any working
       document, so concurrent calls are safe.
    2. BUILD THE DOCUMENT — call docx_create(title=...) THEN
       docx_add_paragraph / docx_add_heading / docx_add_bulleted_list to
       fill in the OCR'd body, structured by what you read. Use headings
       for visible section markers. For tabular data, use docx_add_table.
       For charts, use chart_redraw with also_svg=true → insert_image
       with both image_file_id (PNG fallback) AND svg_file_id (vector
       master) for EVERY chart. Pass svg_file_id on EACH insert_image
       call — a deck with two charts needs both inserts to carry their
       respective svg_file_ids, otherwise that chart will be raster only.

       SEQUENCING RULE (CRITICAL — same as the PPTX one):
       docx_create + every subsequent docx_add_* / insert_image call is
       a CHAIN. Each one returns a NEW file_id, and the next call starts
       from that returned file (state.current_fid). NEVER make multiple
       docx_add_* / insert_image calls in the SAME tool-calling round —
       they all start from the same stale file_id and only the last
       survives, so you'll lose paragraphs/charts and end up with a
       partially-built doc OR two divergent docs masquerading as
       page28.docx in the file list. Issue them ONE PER ITERATION.
    3. The dispatcher's auto-convert hook ALREADY handles "in <format>"
       requests in the user instruction at the end — you do NOT need to
       call convert_format manually. Just leave the document in its native
       form (docx/xlsx/etc) and call finish().

  ANTI-PATTERN to avoid: OCR'ing the image, describing what you saw in your
  finish summary, then calling finish() without ever calling docx_create.
  This produces no artifact for the user — the post-loop hook will then
  convert the ORIGINAL IMAGE to the requested format, yielding a useless
  raster-PDF (just the photo on a page). When the user asks to "iratold le",
  they want a REAL document with the transcribed content, not a re-rendered
  photo.

PPTX strategy (CRITICAL — DO NOT use convert_format(target='pptx')):
  Building a presentation from a document is a CREATIVE task, not a mechanical
  format conversion. convert_format(target='pptx') from non-Impress source
  doesn't work in LibreOffice. Follow this exact 6-phase workflow:

    PHASE 1 — READ:
      read_document on the source.

    PHASE 2 — PLAN (MANDATORY — do not skip):
      Call pptx_plan(deck_title, deck_subtitle, slides=[...]). For each slide
      enumerate: title, content_type ∈ {'bullets','content','table'}, and
      key_points (or headers+rows for tables). This commits you to a
      structure before any slide is built. NO SLIDES UNTIL PLAN IS DONE.
      Use 'table' content_type for any slide that compares values across
      categories (e.g. scenarios × indicators) — a real PPTX table looks
      far better than bullet-text mimicking columns.

    PHASE 3 — CREATE:
      pptx_create(title=deck_title, subtitle=deck_subtitle).

    PHASE 4 — BUILD ONE SLIDE AT A TIME (sequential, NEVER parallel):
      For each slide in your plan, in order:
        a. Call the appropriate add tool:
             - 'bullets' → pptx_add_bullets_slide
             - 'content' → pptx_add_slide
             - 'table'   → pptx_add_table_slide
        b. (Optional) pptx_set_slide_styles(slide_index=last_index, ...)
           for per-slide accents (e.g. a hero/divider slide).
      CRITICAL: each add returns a NEW file_id; the next slide chains to it.
      NEVER make multiple add calls in the same tool-calling round — they
      all start from the same stale state and only the last survives.

    PHASE 5 — VISUAL VERIFY (highly recommended):
      Call pptx_render_slide(slide_index=N) to rasterize a slide, then
      describe_image(image_file_id=..., question='Does this slide layout
      look clean and readable? Is there overflow or cut-off text?'). Do
      this for slides you suspect might be cramped (long bullets, dense
      content). If problems are reported, fix with pptx_set_slide_styles
      (smaller font, etc.) or replace the slide content. You can also
      pptx_render_slide() with no slide_index to render the whole deck.

    PHASE 6 — POLISH & FINISH:
      Call pptx_apply_theme(accent_color=..., accent_position='left',
      footer_text=<organization or brief id>, page_numbers=true) — this
      adds visual chrome (accent bar, footer, page numbers) and cohesive
      title/body colors. The cover slide stays clean. THIS IS REQUIRED:
      a deck without theme chrome looks unfinished and amateur.
      Verify with pptx_slide_count, then finish().

  DESIGN PRINCIPLES (CRITICAL — apply during planning AND building):
    A monotonous deck full of the same title+bullet layout is the #1 failure
    mode. Actively use VARIED layouts — match content type to slide style:
      • cover / closer → 'title' or 'title_only' layout, large hero text
      • section dividers → 'section_header' layout, short and bold (use these
        every 2-3 content slides to give the deck rhythm)
      • short hierarchical lists → 'title_content' with bullets (5-9 max,
        one short sentence each — never dump long paragraphs)
      • compare-and-contrast (e.g. left vs right, before vs after) → 'two_column'
      • numerical comparisons across categories → 'pptx_add_table_slide' (NOT
        bullets pretending to be a table)
      • a single big number/quote → 'title_only' with a callout textbox
    Do NOT default to bullet-list for every slide. If you find yourself adding
    5+ consecutive bullet slides, BREAK with a divider, table, or callout.

    Color & tone: pick a palette that matches the document's domain
    (policy brief / financial → muted darkblue + gold accents; energetic →
    contrasting warm tones; technical → cool monochrome). One palette
    consistently — don't mix random colors. Keep dominant color 60-70%,
    accents 20-30%. Title color cohesive across the deck. Use dark
    backgrounds for cover/closer (via styling), light for content slides.

    Avoid: every slide centered, identical layouts repeated, walls of text,
    unicode bullet chars (•) — let the layout's bullet style do the work,
    body text > 6 lines per slide, generic blue-on-white aesthetic.

  CONTENT-DENSITY HARD LIMITS (CRITICAL — violating these makes the deck
  overflow / unreadable, the #1 quality failure mode):
    • Bullet slide: MAX 4-5 bullets, each ≤ 70 characters (one short
      sentence; no commas-soup, no semicolons stacking ideas).
    • Content slide: MAX 5-6 short lines, each ≤ 80 characters.
    • Table slide: ideally 5-7 columns × 4-6 data rows. Larger → SPLIT
      into two table slides ('Rész 1' / 'Rész 2').
    • Source phrases like "S1 · Optimista — Islamabad Protocol —
      pakisztáni-kínai mediáció sikerrel zárul, Brent 75, CPI 3,2%"
      must become 2-3 SHORT bullets OR a single TABLE row, NEVER a
      150-char mega-bullet.
    • If a bullet is over ~70 chars, split it; if a slide has > 5 bullets,
      split it. These are REQUIRED, not soft suggestions.
"""


# ============================================================
# Agent state
# ============================================================


@dataclass
class AgentState:
    current_fid: str
    original_fid: str
    original_format: str
    started_at: float = field(default_factory=time.time)
    iters: int = 0
    trace: list[dict[str, Any]] = field(default_factory=list)
    do_task_depth: int = 0  # incremented when spawning a sub-agent via do_task
    spawned_file_ids: list[str] = field(default_factory=list)  # for ephemeral marking


# ============================================================
# Tool dispatch
# ============================================================


async def _tool_read_document(state: AgentState, args: dict, pool) -> dict:
    res = await edit_ops.extract(state.current_fid, mode="text")
    text = res.get("text", "")
    truncated = False
    if len(text) > 30000:
        text = text[:30000]
        truncated = True
    return {"text": text, "length_chars": len(res.get("text", "")), "truncated": truncated, "format": detect_from_path(_resolve(state.current_fid))}


async def _tool_count_matches(state: AgentState, args: dict, pool) -> dict:
    import re as _re
    pattern = args["pattern"]
    regex = bool(args.get("regex", False))
    res = await edit_ops.extract(state.current_fid, mode="text")
    text = res.get("text", "")
    if regex:
        try:
            count = len(_re.findall(pattern, text))
        except _re.error as e:
            return {"error": f"invalid regex: {e}"}
    else:
        count = text.count(pattern)
    return {"pattern": pattern, "matches_count": count}


async def _tool_find_replace(state: AgentState, args: dict, pool) -> dict:
    pattern = args["pattern"]
    regex = bool(args.get("regex", False))
    out = await edit_ops.find_replace(
        state.current_fid, pattern, args["replacement"], regex
    )
    matches = out.get("matches", -1)
    if matches == 0:
        return {
            "matches_count": 0,
            "note": (
                "pattern was not found in any single paragraph (DOCX) / text node (MD/HTML). "
                "Note that find_replace is paragraph-bounded — patterns spanning multiple "
                "paragraphs/lines won't match. For inserting new content after a paragraph, "
                "use insert_after with the EXACT paragraph text as anchor."
            ),
        }
    state.current_fid = out["file_id"]
    return {"matches_count": matches, "file_id": state.current_fid, "ms": out.get("ms_elapsed")}


async def _tool_replace_heading(state: AgentState, args: dict, pool) -> dict:
    out = await edit_ops.replace_heading(state.current_fid, args["old_title"], args["new_title"])
    state.current_fid = out["file_id"]
    return {"file_id": state.current_fid, "ms": out.get("ms_elapsed")}


async def _tool_delete_section(state: AgentState, args: dict, pool) -> dict:
    out = await edit_ops.delete_section(state.current_fid, args["heading"], args.get("level"))
    state.current_fid = out["file_id"]
    return {"file_id": state.current_fid, "ms": out.get("ms_elapsed")}


async def _tool_insert_after(state: AgentState, args: dict, pool) -> dict:
    out = await edit_ops.insert_after(state.current_fid, args["anchor"], args["content"])
    state.current_fid = out["file_id"]
    return {"file_id": state.current_fid, "ms": out.get("ms_elapsed")}


async def _tool_delete_pages(state: AgentState, args: dict, pool) -> dict:
    out = await edit_ops.delete_pages(state.current_fid, args["page_range"])
    state.current_fid = out["file_id"]
    return {
        "file_id": state.current_fid,
        "pages_remaining": out.get("pages_remaining"),
        "ms": out.get("ms_elapsed"),
    }


async def _tool_style_paragraph(state: AgentState, args: dict, pool) -> dict:
    style = {k: v for k, v in args.items() if k in {
        "color","highlight","bold","italic","underline","font_size","font_family"}}
    out = await edit_ops.style_paragraph(state.current_fid, args["anchor"], **style)
    state.current_fid = out["file_id"]
    return {
        "file_id": state.current_fid,
        "paragraphs_styled": out.get("paragraphs_styled"),
        "ms": out.get("ms_elapsed"),
    }


async def _tool_style_all_headings(state: AgentState, args: dict, pool) -> dict:
    style = {k: v for k, v in args.items() if k in {
        "color","highlight","bold","italic","underline","font_size","font_family"}}
    levels = args.get("levels")
    out = await edit_ops.style_all_headings(state.current_fid, levels=levels, **style)
    state.current_fid = out["file_id"]
    return {
        "file_id": state.current_fid,
        "headings_styled": out.get("headings_styled"),
        "ms": out.get("ms_elapsed"),
    }


async def _tool_insert_image(state: AgentState, args: dict, pool) -> dict:
    out = await edit_ops.insert_image(
        state.current_fid,
        args["anchor"],
        args["image_file_id"],
        width_inches=args.get("width_inches"),
        svg_file_id=args.get("svg_file_id"),
    )
    state.current_fid = out["file_id"]
    return {
        "file_id": state.current_fid,
        "ms": out.get("ms_elapsed"),
        "vector_svg_attached": out.get("vector_svg_attached", False),
    }


async def _tool_convert_format(state: AgentState, args: dict, pool) -> dict:
    out = await t_convert(
        pool,
        file_id=state.current_fid,
        target_format=args["target_format"],
        use_browser=bool(args.get("use_browser", False)),
    )
    state.current_fid = out["file_id"]
    return {
        "file_id": state.current_fid,
        "backend": out.get("backend"),
        "ms": out.get("ms_elapsed"),
    }


async def _tool_pptx_plan(state: AgentState, args: dict, pool) -> dict:
    return await pptx_builder.pptx_plan(
        args["deck_title"], args.get("slides", []),
        deck_subtitle=args.get("deck_subtitle"),
    )


async def _tool_pptx_add_table_slide(state: AgentState, args: dict, pool) -> dict:
    out = await pptx_builder.pptx_add_table_slide(
        state.current_fid, args["title"],
        args.get("headers", []), args.get("rows", []),
        header_color=args.get("header_color", "#1F3A5F"),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_pptx_render_slide(state: AgentState, args: dict, pool) -> dict:
    return await pptx_builder.pptx_render_slide(
        pool, state.current_fid, slide_index=args.get("slide_index"),
    )


async def _tool_docx_create(state, args, pool):
    out = await docx_builder.docx_create(title=args.get("title"))
    state.current_fid = out["file_id"]
    return out


async def _tool_docx_add_heading(state, args, pool):
    out = await docx_builder.docx_add_heading(
        state.current_fid, args["text"],
        level=int(args.get("level", 1)),
        alignment=args.get("alignment"),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_docx_add_paragraph(state, args, pool):
    out = await docx_builder.docx_add_paragraph(
        state.current_fid, args["text"],
        bold=bool(args.get("bold", False)),
        italic=bool(args.get("italic", False)),
        alignment=args.get("alignment"),
        font_size_pt=args.get("font_size_pt"),
        font_family=args.get("font_family"),
        color=args.get("color"),
        space_after_pt=args.get("space_after_pt"),
        indent_cm=args.get("indent_cm"),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_docx_add_form_field(state, args, pool):
    out = await docx_builder.docx_add_form_field(
        state.current_fid, args["label"], dots=int(args.get("dots", 80)),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_docx_add_numbered_list(state, args, pool):
    out = await docx_builder.docx_add_numbered_list(
        state.current_fid, args.get("items", []),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_docx_add_bulleted_list(state, args, pool):
    out = await docx_builder.docx_add_bulleted_list(
        state.current_fid, args.get("items", []),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_docx_add_signature_block(state, args, pool):
    out = await docx_builder.docx_add_signature_block(
        state.current_fid,
        label=args.get("label", "aláírás"),
        alignment=args.get("alignment", "right"),
        dots=int(args.get("dots", 50)),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_docx_add_table(state, args, pool):
    out = await docx_builder.docx_add_table(
        state.current_fid,
        args.get("headers", []), args.get("rows", []),
        header_color=args.get("header_color", "#1F3A5F"),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_docx_add_page_break(state, args, pool):
    out = await docx_builder.docx_add_page_break(state.current_fid)
    state.current_fid = out["file_id"]
    return out


async def _tool_xlsx_create(state, args, pool):
    out = await xlsx_builder.xlsx_create(sheet_name=args.get("sheet_name", "Sheet1"))
    state.current_fid = out["file_id"]
    return out


async def _tool_xlsx_add_sheet(state, args, pool):
    out = await xlsx_builder.xlsx_add_sheet(
        state.current_fid, args["name"], position=args.get("position"),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_xlsx_write_block(state, args, pool):
    out = await xlsx_builder.xlsx_write_block(
        state.current_fid, args["sheet"], args.get("data", []),
        anchor=args.get("anchor", "A1"),
        headers=args.get("headers"),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_xlsx_set_formula(state, args, pool):
    out = await xlsx_builder.xlsx_set_formula(
        state.current_fid, args["sheet"], args["cell"], args["formula"],
        number_format=args.get("number_format"),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_xlsx_apply_style(state, args, pool):
    style_kwargs = {k: args[k] for k in (
        "bold","italic","font_size","font_family","font_color","fill_color",
        "horizontal","vertical","number_format","border_all","wrap_text",
    ) if k in args}
    out = await xlsx_builder.xlsx_apply_style(
        state.current_fid, args["sheet"], args["range_ref"], **style_kwargs,
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_xlsx_merge_cells(state, args, pool):
    out = await xlsx_builder.xlsx_merge_cells(
        state.current_fid, args["sheet"], args["range_ref"],
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_xlsx_set_column_widths(state, args, pool):
    out = await xlsx_builder.xlsx_set_column_widths(
        state.current_fid, args["sheet"], args.get("widths", {}),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_xlsx_freeze_panes(state, args, pool):
    out = await xlsx_builder.xlsx_freeze_panes(
        state.current_fid, args["sheet"], anchor=args.get("anchor", "B2"),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_xlsx_inspect(state, args, pool):
    return await xlsx_builder.xlsx_inspect(
        state.current_fid,
        sheet=args.get("sheet"),
        max_rows=int(args.get("max_rows", 25)),
        max_cols=int(args.get("max_cols", 12)),
    )


async def _tool_brand_extract(state: AgentState, args: dict, pool) -> dict:
    return await _brand_extract(
        pool, args.get("file_id") or state.current_fid,
        sample_pages=int(args.get("sample_pages", 2)),
        suggested_name=args.get("suggested_name", "new_brand"),
    )


async def _tool_brand_register(state: AgentState, args: dict, pool) -> dict:
    return brand_register_runtime(args.get("spec", {}))


async def _tool_docx_render_pages(state: AgentState, args: dict, pool) -> dict:
    return await visual_feedback.docx_render_pages(
        pool, state.current_fid,
        page_indices=args.get("page_indices"),
        dpi=int(args.get("dpi", 110)),
    )


async def _tool_html_render_screenshot(state: AgentState, args: dict, pool) -> dict:
    return await visual_feedback.html_render_screenshot(
        pool, state.current_fid,
        viewport_width=int(args.get("viewport_width", 1024)),
    )


async def _tool_docx_set_paragraph_format(state: AgentState, args: dict, pool) -> dict:
    out = await edit_ops.docx_set_paragraph_format(
        state.current_fid, args["anchor"],
        page_break_before=args.get("page_break_before"),
        keep_with_next=args.get("keep_with_next"),
        keep_lines_together=args.get("keep_lines_together"),
        space_before_pt=args.get("space_before_pt"),
        space_after_pt=args.get("space_after_pt"),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_brand_list(state: AgentState, args: dict, pool) -> dict:
    return {"brands": list_brands()}


async def _tool_brand_describe(state: AgentState, args: dict, pool) -> dict:
    name = args.get("name", "")
    b = get_brand(name)
    if b is None:
        return {"error": f"unknown brand: {name!r}. Use brand_list to see available."}
    return b.to_summary()


async def _tool_brand_apply(state: AgentState, args: dict, pool) -> dict:
    name = args.get("brand", "")
    brand = get_brand(name)
    if brand is None:
        return {"error": f"unknown brand: {name!r}. Use brand_list to see available."}
    src = _resolve(state.current_fid)
    fmt = detect_from_path(src)
    brief_meta = args.get("brief_meta", {}) or {}

    if fmt == "pptx":
        out = await brand_pptx_renderer.apply_brand_to_pptx(
            state.current_fid, brand, brief_meta=brief_meta,
        )
        state.current_fid = out["file_id"]
        return out

    if fmt == "docx":
        out = await brand_docx_renderer.apply_brand_to_docx(
            state.current_fid, brand, brief_meta=brief_meta,
        )
        state.current_fid = out["file_id"]
        return out

    if fmt in ("html", "htm", "md"):
        out = await brand_html_renderer.apply_brand_to_html(
            state.current_fid, brand, brief_meta=brief_meta,
        )
        state.current_fid = out["file_id"]
        return out

    if fmt == "pdf":
        # PDF path: convert to DOCX (pdf2docx), brand the DOCX, convert back to PDF.
        rt_docx = await t_convert(pool, file_id=state.current_fid, target_format="docx")
        branded = await brand_docx_renderer.apply_brand_to_docx(
            rt_docx["file_id"], brand, brief_meta=brief_meta,
        )
        rt_pdf = await t_convert(pool, file_id=branded["file_id"], target_format="pdf")
        state.current_fid = rt_pdf["file_id"]
        return {
            "file_id": rt_pdf["file_id"],
            "url": public_url(rt_pdf["file_id"]),
            "brand": brand.name,
            "intermediate_docx": branded["file_id"],
            "headings_underlined": branded.get("headings_underlined"),
            "section_labels_added": branded.get("section_labels_added"),
            "warnings": [
                "PDF input was round-tripped via DOCX for brand application; "
                "minor layout reflow possible.",
            ],
        }

    return {
        "error": f"brand_apply for format {fmt!r} not yet implemented "
                 f"(pptx/docx/pdf supported; html coming next).",
    }


async def _tool_pptx_apply_theme(state: AgentState, args: dict, pool) -> dict:
    kwargs = {k: v for k, v in args.items() if k in {
        "accent_color", "accent_position", "footer_text", "page_numbers",
        "title_color", "body_color", "font_family",
    }}
    out = await pptx_builder.pptx_apply_theme(state.current_fid, **kwargs)
    state.current_fid = out["file_id"]
    return out


async def _tool_pptx_create(state: AgentState, args: dict, pool) -> dict:
    out = await pptx_builder.pptx_create(args["title"], args.get("subtitle"))
    state.current_fid = out["file_id"]
    return out


# Note: the *_add_* / *_set_styles tools always operate on state.current_fid
# (the latest deck), ignoring any file_id Kimi specifies. This is necessary
# because each tool call produces a NEW file_id (decks are immutable), and
# chaining must thread through state. Otherwise multiple parallel adds would
# all start from the same stale file_id and only the last would survive.
async def _tool_pptx_add_slide(state: AgentState, args: dict, pool) -> dict:
    out = await pptx_builder.pptx_add_slide(
        state.current_fid, args["title"],
        body=args.get("body", ""),
        layout=args.get("layout", "title_content"),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_pptx_add_bullets_slide(state: AgentState, args: dict, pool) -> dict:
    out = await pptx_builder.pptx_add_bullets_slide(
        state.current_fid, args["title"], args.get("bullets", []),
        layout=args.get("layout", "title_content"),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_pptx_add_chart_slide(state: AgentState, args: dict, pool) -> dict:
    out = await pptx_builder.pptx_add_chart_slide(
        state.current_fid,
        args["title"],
        chart_type=args.get("chart_type", "column"),
        categories=args.get("categories", []),
        series=args.get("series", []),
        legend=args.get("legend", True),
        legend_position=args.get("legend_position", "bottom"),
        palette=args.get("palette"),
        title_axis_x=args.get("title_axis_x"),
        title_axis_y=args.get("title_axis_y"),
        show_data_labels=bool(args.get("show_data_labels", False)),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_pptx_modify_textbox(state: AgentState, args: dict, pool) -> dict:
    out = await pptx_builder.pptx_modify_textbox(
        state.current_fid,
        args["find_text"],
        slide_index=args.get("slide_index"),
        new_text=args.get("new_text"),
        font_size_pt=args.get("font_size_pt"),
        bold=args.get("bold"),
        italic=args.get("italic"),
        font_color=args.get("font_color"),
        left_inches=args.get("left_inches"),
        top_inches=args.get("top_inches"),
        width_inches=args.get("width_inches"),
        height_inches=args.get("height_inches"),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_pptx_set_slide_styles(state: AgentState, args: dict, pool) -> dict:
    style = {k: v for k, v in args.items() if k in {
        "title_color", "body_color", "title_font_size", "body_font_size",
        "font_family", "title_bold", "title_italic",
    }}
    out = await pptx_builder.pptx_set_slide_styles(
        state.current_fid, slide_index=args.get("slide_index"), **style,
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_pptx_slide_count(state: AgentState, args: dict, pool) -> dict:
    # Inspect the latest deck unless Kimi explicitly named a different file_id.
    target = args.get("file_id") or state.current_fid
    return await pptx_builder.pptx_slide_count(target)


async def _tool_pptx_check_placeholders(state: AgentState, args: dict, pool) -> dict:
    target = args.get("file_id") or state.current_fid
    return await pptx_builder.pptx_check_placeholders(
        target, extra_patterns=args.get("extra_patterns"),
    )


async def _tool_generate_chart_image(state: AgentState, args: dict, pool) -> dict:
    return await chart_image.generate_chart_image(
        args["chart_type"], args["categories"], args["series"],
        title=args.get("title"),
        palette=args.get("palette"),
        x_label=args.get("x_label"),
        y_label=args.get("y_label"),
        show_data_labels=bool(args.get("show_data_labels", False)),
        also_svg=bool(args.get("also_svg", False)),
    )


async def _tool_generate_image(state: AgentState, args: dict, pool) -> dict:
    return await vision_ops.generate_image(
        args["prompt"],
        model=args.get("model", "black-forest-labs/FLUX.2-pro"),
        size=args.get("size", "1024x1024"),
    )


async def _tool_ocr_image(state: AgentState, args: dict, pool) -> dict:
    return await vision_ops.ocr_image(
        args["image_file_id"], language_hint=args.get("language_hint")
    )


async def _tool_describe_image(state: AgentState, args: dict, pool) -> dict:
    return await vision_ops.describe_image(
        args["image_file_id"], question=args.get("question")
    )


async def _tool_extract_table_from_image(state: AgentState, args: dict, pool) -> dict:
    return await vision_ops.extract_table_from_image(args["image_file_id"])


async def _tool_image_to_xlsx(state: AgentState, args: dict, pool) -> dict:
    out = await vision_ops.image_to_xlsx(
        args["image_file_id"], sheet_name=args.get("sheet_name", "Sheet1")
    )
    # The xlsx is a NEW document — set it as current_fid so subsequent edits target it.
    state.current_fid = out["file_id"]
    return out


async def _tool_docx_track_replace_paragraph(state: AgentState, args: dict, pool) -> dict:
    out = await docx_revisions.docx_track_replace_paragraph(
        state.current_fid, args["anchor"], args["new_text"],
        author=args.get("author", "Kimi K2.6"),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_docx_track_insert_after(state: AgentState, args: dict, pool) -> dict:
    out = await docx_revisions.docx_track_insert_after(
        state.current_fid, args["anchor"], args["new_text"],
        author=args.get("author", "Kimi K2.6"),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_docx_track_delete_paragraph(state: AgentState, args: dict, pool) -> dict:
    out = await docx_revisions.docx_track_delete_paragraph(
        state.current_fid, args["anchor"],
        author=args.get("author", "Kimi K2.6"),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_docx_accept_all_revisions(state: AgentState, args: dict, pool) -> dict:
    out = await docx_revisions.docx_accept_all_revisions(state.current_fid)
    state.current_fid = out["file_id"]
    return out


async def _tool_docx_reject_all_revisions(state: AgentState, args: dict, pool) -> dict:
    out = await docx_revisions.docx_reject_all_revisions(state.current_fid)
    state.current_fid = out["file_id"]
    return out


async def _tool_docx_list_revisions(state: AgentState, args: dict, pool) -> dict:
    return await docx_revisions.docx_list_revisions(state.current_fid)


async def _tool_docx_add_comment(state: AgentState, args: dict, pool) -> dict:
    out = await docx_revisions.docx_add_comment(
        state.current_fid, args["anchor"], args["comment_text"],
        author=args.get("author", "Kimi K2.6"),
        initials=args.get("initials", "K"),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_docx_list_comments(state: AgentState, args: dict, pool) -> dict:
    return await docx_revisions.docx_list_comments(state.current_fid)


async def _tool_docx_reply_to_comment(state: AgentState, args: dict, pool) -> dict:
    out = await docx_revisions.docx_reply_to_comment(
        state.current_fid,
        int(args["parent_comment_id"]),
        args["reply_text"],
        author=args.get("author", "Kimi K2.6"),
        initials=args.get("initials", "K"),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_docx_resolve_comment(state: AgentState, args: dict, pool) -> dict:
    out = await docx_revisions.docx_resolve_comment(
        state.current_fid,
        int(args["comment_id"]),
        resolved=bool(args.get("resolved", True)),
    )
    state.current_fid = out["file_id"]
    return out


async def _tool_legal_contract_extract_clauses(state: AgentState, args: dict, pool) -> dict:
    return await legal_vertical.legal_contract_extract_clauses(state.current_fid)


async def _tool_legal_compare_to_template(state: AgentState, args: dict, pool) -> dict:
    return await legal_vertical.legal_compare_to_template(
        state.current_fid, args["template_id"],
    )


async def _tool_legal_gdpr_review(state: AgentState, args: dict, pool) -> dict:
    return await legal_vertical.legal_gdpr_review(state.current_fid)


async def _tool_legal_aiact_review(state: AgentState, args: dict, pool) -> dict:
    return await legal_vertical.legal_aiact_review(state.current_fid)


async def _tool_legal_term_consistency(state: AgentState, args: dict, pool) -> dict:
    return await legal_vertical.legal_term_consistency(
        state.current_fid, glossary_file_id=args.get("glossary_file_id"),
    )


async def _tool_do_task(state: AgentState, args: dict, pool) -> dict:
    """Recursive sub-agent. Refuses at depth >= 1 to prevent runaway recursion."""
    if state.do_task_depth >= 1:
        return {
            "error": (
                "do_task refused: max recursion depth reached. "
                "You're already inside a sub-agent — do the work directly."
            ),
        }
    file_ids = args.get("file_ids", [])
    if not file_ids:
        return {"error": "do_task requires at least one file_id"}
    primary_fid = file_ids[0]
    extra_files = file_ids[1:]
    extra_note = (
        f"\n\nADDITIONAL FILES (use as context — file_ids): {extra_files}"
        if extra_files else ""
    )
    sub_instruction = f"{args['goal']}{extra_note}"

    sub_result = await run_agent(
        pool=pool,
        file_id=primary_fid,
        instruction=sub_instruction,
        max_iters=int(args.get("max_iters", 8)),
        do_task_depth=state.do_task_depth + 1,
    )
    # Bring the sub-agent's final file into our state so subsequent tool
    # calls in the parent agent can chain off it
    if sub_result.get("file_id"):
        state.current_fid = sub_result["file_id"]

    return {
        "sub_file_id": sub_result.get("file_id"),
        "summary": sub_result.get("summary"),
        "confidence": sub_result.get("confidence"),
        "iters_used": sub_result.get("iters"),
        "warnings": sub_result.get("warnings", []),
        "trace_step_count": len(sub_result.get("trace", [])),
    }


async def _tool_extract_citations(state: AgentState, args: dict, pool) -> dict:
    return await mtmt_integration.extract_citations(state.current_fid)


async def _tool_mtmt_search_author(state: AgentState, args: dict, pool) -> dict:
    return await mtmt_integration.mtmt_search_author(
        args["name"], limit=int(args.get("limit", 10)),
    )


async def _tool_mtmt_verify_citation(state: AgentState, args: dict, pool) -> dict:
    return await mtmt_integration.mtmt_verify_citation(
        args["citation"], author_id=args.get("author_id"),
    )


async def _tool_term_consistency_check(state: AgentState, args: dict, pool) -> dict:
    file_ids = args.get("file_ids") or [state.current_fid]
    return await term_consistency.term_consistency_check(
        file_ids,
        glossary=args.get("glossary"),
        auto_detect=bool(args.get("auto_detect", True)),
    )


async def _tool_chart_redraw(state: AgentState, args: dict, pool) -> dict:
    return await chart_redraw.chart_redraw(
        args["image_file_id"],
        palette=args.get("palette"),
        brand=args.get("brand"),
        chart_type_override=args.get("chart_type_override"),
        title_override=args.get("title_override"),
        width_px=int(args.get("width_px", 1600)),
        height_px=int(args.get("height_px", 900)),
        also_svg=bool(args.get("also_svg", False)),
    )


async def _tool_parse_with_quality(state: AgentState, args: dict, pool) -> dict:
    out = await parser_self_healing.parse_with_quality(
        state.current_fid,
        force_vision=bool(args.get("force_vision", False)),
        quality_threshold=float(args.get("quality_threshold", 0.55)),
        max_vision_pages=args.get("max_vision_pages"),
    )
    text = out.get("text", "")
    truncated = False
    if len(text) > 30000:
        out["text"] = text[:30000]
        out["truncated"] = True
        truncated = True
    return out


TOOL_HANDLERS: dict[str, Callable[[AgentState, dict, Any], Awaitable[dict]]] = {
    "read_document": _tool_read_document,
    "count_matches": _tool_count_matches,
    "find_replace": _tool_find_replace,
    "replace_heading": _tool_replace_heading,
    "delete_section": _tool_delete_section,
    "insert_after": _tool_insert_after,
    "delete_pages": _tool_delete_pages,
    "style_paragraph": _tool_style_paragraph,
    "style_all_headings": _tool_style_all_headings,
    "insert_image": _tool_insert_image,
    "convert_format": _tool_convert_format,
    "generate_image": _tool_generate_image,
    "generate_chart_image": _tool_generate_chart_image,
    "ocr_image": _tool_ocr_image,
    "describe_image": _tool_describe_image,
    "extract_table_from_image": _tool_extract_table_from_image,
    "image_to_xlsx": _tool_image_to_xlsx,
    "pptx_plan": _tool_pptx_plan,
    "pptx_create": _tool_pptx_create,
    "pptx_add_slide": _tool_pptx_add_slide,
    "pptx_add_bullets_slide": _tool_pptx_add_bullets_slide,
    "pptx_add_table_slide": _tool_pptx_add_table_slide,
    "pptx_add_chart_slide": _tool_pptx_add_chart_slide,
    "pptx_set_slide_styles": _tool_pptx_set_slide_styles,
    "pptx_modify_textbox": _tool_pptx_modify_textbox,
    "pptx_slide_count": _tool_pptx_slide_count,
    "pptx_check_placeholders": _tool_pptx_check_placeholders,
    "pptx_render_slide": _tool_pptx_render_slide,
    "pptx_apply_theme": _tool_pptx_apply_theme,
    "brand_list": _tool_brand_list,
    "brand_describe": _tool_brand_describe,
    "brand_apply": _tool_brand_apply,
    "brand_extract": _tool_brand_extract,
    "brand_register": _tool_brand_register,
    "docx_render_pages": _tool_docx_render_pages,
    "html_render_screenshot": _tool_html_render_screenshot,
    "docx_set_paragraph_format": _tool_docx_set_paragraph_format,
    # docx builder
    "docx_create": _tool_docx_create,
    "docx_add_heading": _tool_docx_add_heading,
    "docx_add_paragraph": _tool_docx_add_paragraph,
    "docx_add_form_field": _tool_docx_add_form_field,
    "docx_add_numbered_list": _tool_docx_add_numbered_list,
    "docx_add_bulleted_list": _tool_docx_add_bulleted_list,
    "docx_add_signature_block": _tool_docx_add_signature_block,
    "docx_add_table": _tool_docx_add_table,
    "docx_add_page_break": _tool_docx_add_page_break,
    # xlsx builder
    "xlsx_create": _tool_xlsx_create,
    "xlsx_add_sheet": _tool_xlsx_add_sheet,
    "xlsx_write_block": _tool_xlsx_write_block,
    "xlsx_set_formula": _tool_xlsx_set_formula,
    "xlsx_apply_style": _tool_xlsx_apply_style,
    "xlsx_merge_cells": _tool_xlsx_merge_cells,
    "xlsx_set_column_widths": _tool_xlsx_set_column_widths,
    "xlsx_freeze_panes": _tool_xlsx_freeze_panes,
    "xlsx_inspect": _tool_xlsx_inspect,
    # tracked changes + comments
    "docx_track_replace_paragraph": _tool_docx_track_replace_paragraph,
    "docx_track_insert_after": _tool_docx_track_insert_after,
    "docx_track_delete_paragraph": _tool_docx_track_delete_paragraph,
    "docx_accept_all_revisions": _tool_docx_accept_all_revisions,
    "docx_reject_all_revisions": _tool_docx_reject_all_revisions,
    "docx_list_revisions": _tool_docx_list_revisions,
    "docx_add_comment": _tool_docx_add_comment,
    "docx_list_comments": _tool_docx_list_comments,
    "docx_reply_to_comment": _tool_docx_reply_to_comment,
    "docx_resolve_comment": _tool_docx_resolve_comment,
    # legal vertical (placeholders)
    "legal_contract_extract_clauses": _tool_legal_contract_extract_clauses,
    "legal_compare_to_template": _tool_legal_compare_to_template,
    "legal_gdpr_review": _tool_legal_gdpr_review,
    "legal_aiact_review": _tool_legal_aiact_review,
    "legal_term_consistency": _tool_legal_term_consistency,
    # self-healing parser
    "parse_with_quality": _tool_parse_with_quality,
    # visual chart redraw
    "chart_redraw": _tool_chart_redraw,
    # cross-document consistency
    "term_consistency_check": _tool_term_consistency_check,
    # MTMT integration (extraction works, lookup placeholder)
    "extract_citations": _tool_extract_citations,
    "mtmt_search_author": _tool_mtmt_search_author,
    "mtmt_verify_citation": _tool_mtmt_verify_citation,
    # mega-tool — recursive sub-agent
    "do_task": _tool_do_task,
}


def _resolve(file_id: str) -> Path:
    p = output_path(file_id)
    if p.exists():
        return p
    p = input_path(file_id)
    if p.exists():
        return p
    raise FileNotFoundError(file_id)


# ============================================================
# LLM call (with retry — SiliconFlow returns intermittent 500s)
# ============================================================


_KIMI_TOOL_CALL_RX = re.compile(
    r"<\|tool_call_begin\|>\s*functions\.([\w]+):(\d+)\s*"
    r"<\|tool_call_argument_begin\|>(.*?)"
    r"<\|tool_call_end\|>",
    re.DOTALL,
)
# Capture between markers without trying to bracket-balance JSON. The previous
# `\{.*?\}` non-greedy stopped at the first inner `}`, silently truncating
# any nested-object args (e.g. glossary, replacements).


def _parse_kimi_text_tool_calls(content: str) -> list[dict]:
    """Parse Kimi K2.6's native tool-call format embedded in content.

    Format: `<|tool_calls_section_begin|><|tool_call_begin|>functions.NAME:N
    <|tool_call_argument_begin|>{json_args}<|tool_call_end|>...`
    Returns a list shaped like the OpenAI `tool_calls` array."""
    if "<|tool_call_begin|>" not in content:
        return []
    out = []
    for m in _KIMI_TOOL_CALL_RX.finditer(content):
        name, idx, args_raw = m.group(1), m.group(2), m.group(3).strip()
        out.append({
            "id": f"call_kimi_{idx}",
            "type": "function",
            "function": {"name": name, "arguments": args_raw},
        })
    return out


async def _call_llm(model: str, messages: list[dict], tools: list[dict]) -> dict:
    if not settings.SILICONFLOW_API_KEY:
        raise RuntimeError("SILICONFLOW_API_KEY not set")
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0.2,
        "max_tokens": 2048,
    }
    last_exc: Optional[Exception] = None
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(6),
        wait=wait_exponential(min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
        reraise=False,
    ):
        with attempt:
            async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT_S) as client:
                r = await client.post(
                    f"{settings.SILICONFLOW_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.SILICONFLOW_API_KEY}"},
                    json=payload,
                )
                if r.status_code >= 500:
                    last_exc = httpx.HTTPStatusError(
                        f"SiliconFlow {r.status_code}: {r.text[:200]}",
                        request=r.request, response=r,
                    )
                    raise last_exc
                if r.status_code >= 400:
                    # 400 = structural problem (bad tool, oversized request,
                    # invalid message); won't help to retry with same payload.
                    # Extract the structured error message only — raw body can
                    # echo back request fragments containing PII or auth state.
                    err_msg = ""
                    try:
                        err_json = r.json()
                        err_msg = (err_json.get("error", {}).get("message")
                                   or err_json.get("message") or "")
                    except Exception:
                        err_msg = ""
                    if not err_msg:
                        err_msg = f"<unparseable {r.status_code} body, len={len(r.text)}>"
                    log.error("SiliconFlow %d: %s", r.status_code, err_msg[:300])
                    raise httpx.HTTPStatusError(
                        f"SiliconFlow {r.status_code}: {err_msg[:300]}",
                        request=r.request, response=r,
                    )
                return r.json()
    raise last_exc or RuntimeError("LLM call failed without exception")


async def _call_with_fallback(messages: list[dict], tools: list[dict]) -> tuple[dict, str]:
    """Try primary, fall back on V4-Pro on 500/parse error."""
    try:
        data = await _call_llm(settings.LLM_MODEL_PRIMARY, messages, tools)
        return data, settings.LLM_MODEL_PRIMARY
    except Exception as e:
        log.warning("primary model failed (%s) — falling back", e)
        data = await _call_llm(settings.LLM_MODEL_FALLBACK, messages, tools)
        return data, f"{settings.LLM_MODEL_FALLBACK}_fallback"


# ============================================================
# Agent loop
# ============================================================


async def run_agent(
    *,
    pool,
    file_id: str,
    instruction: str,
    max_iters: int = MAX_ITERS,
    wall_budget_s: float = WALL_BUDGET_S,
    do_task_depth: int = 0,
) -> dict:
    """Run the autonomous edit agent. Returns final file_id, summary, trace.

    do_task_depth: tracks recursive sub-agent invocation. The do_task tool
    refuses to spawn at depth >= 1 to prevent runaway recursion.
    """
    src = _resolve(file_id)
    src_fmt = detect_from_path(src)
    is_image = src_fmt in {"png", "jpg", "jpeg", "webp", "gif", "bmp"}

    # No auto-round-trip for PDF anymore — find_replace works directly on PDF
    # (in-place via PyMuPDF) and preserves layout. If the agent needs structural
    # ops (replace_heading, insert_after, etc.), it can call convert_format itself.
    layout_warning = None
    working_fid = file_id

    state = AgentState(
        current_fid=working_fid,
        original_fid=file_id,
        original_format=src_fmt,
        do_task_depth=do_task_depth,
    )

    # Initial preview — only for document formats; images go straight to vision tools
    if is_image:
        size_kb = src.stat().st_size // 1024
        intro = (
            f"INPUT FILE: image, format={src_fmt}, size={size_kb} kB, file_id={file_id}\n"
            f"This is an IMAGE — use vision tools (ocr_image, describe_image, "
            f"extract_table_from_image, image_to_xlsx) to read or process it. "
            f"The standard read_document/find_replace tools won't work on images "
            f"unless you first convert the image into a document (e.g. via image_to_xlsx).\n"
        )
    else:
        try:
            initial = await edit_ops.extract(state.current_fid, mode="text")
            doc_preview = initial.get("text", "")[:DOC_PREVIEW_CHARS]
            truncated = len(initial.get("text", "")) > DOC_PREVIEW_CHARS
            intro = (
                f"DOCUMENT FORMAT: {detect_from_path(_resolve(state.current_fid))}\n"
                f"DOCUMENT PREVIEW (first {DOC_PREVIEW_CHARS} chars"
                f"{', truncated' if truncated else ''}):\n```\n{doc_preview}\n```\n"
            )
        except Exception as e:
            log.warning("could not pre-extract %s: %s", state.current_fid, e)
            intro = (
                f"INPUT FILE: format={src_fmt}, file_id={file_id} (pre-extraction failed: {e}). "
                f"Use read_document or appropriate tools.\n"
            )

    agent_id = f"agent-{int(time.time())}"
    events.emit(
        "agent_started",
        agent_id=agent_id,
        file_id=file_id,
        instruction=instruction,
        format=src_fmt,
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"{intro}\n"
                f"INSTRUCTION:\n{instruction}\n\n"
                f"Begin. Call tools step by step. End with finish()."
            ),
        },
    ]

    finish_summary: Optional[str] = None
    finish_confidence: Optional[float] = None
    model_used: Optional[str] = None
    warnings: list[str] = []
    if layout_warning:
        warnings.append(layout_warning)

    for it in range(max_iters):
        if time.time() - state.started_at > wall_budget_s:
            warnings.append(f"wall budget {wall_budget_s}s exceeded")
            break

        state.iters = it + 1
        try:
            data, model_used = await _call_with_fallback(messages, TOOL_SCHEMAS)
        except Exception as e:
            log.exception("LLM call failed irrecoverably")
            warnings.append(f"LLM call failed: {e}")
            break

        msg = data["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []

        # FALLBACK: Kimi K2.6 sometimes emits tool calls as RAW TEXT in content
        # using its native format `<|tool_calls_section_begin|><|tool_call_begin|>
        # functions.NAME:N<|tool_call_argument_begin|>{json}<|tool_call_end|>...`
        # instead of the OpenAI-compatible `tool_calls` array. Parse it ourselves.
        if not tool_calls and msg.get("content"):
            tool_calls = _parse_kimi_text_tool_calls(msg["content"]) or []
            if tool_calls:
                # Strip the parsed marker text from the content so it doesn't
                # confuse subsequent turns
                msg["content"] = re.sub(
                    r"<\|tool_calls_section_begin\|>.*?<\|tool_calls_section_end\|>",
                    "", msg["content"], flags=re.DOTALL,
                ).strip()

        # Append assistant message verbatim (with any reasoning + tool_calls).
        # Kimi K2.6 in thinking mode REQUIRES `reasoning_content` to be passed
        # back unchanged on the next call — otherwise SiliconFlow returns
        # `400 code 20015: "reasoning_content in the thinking mode must be passed back"`.
        asst_msg: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("reasoning_content"):
            asst_msg["reasoning_content"] = msg["reasoning_content"]
        if tool_calls:
            asst_msg["tool_calls"] = [
                {
                    "id": tc.get("id") or f"call_{it}_{i}",
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"].get("arguments") or "{}",
                    },
                }
                for i, tc in enumerate(tool_calls)
            ]
        messages.append(asst_msg)

        if not tool_calls:
            # Kimi just talked — try one more nudge then stop.
            warnings.append(f"iteration {it + 1}: no tool call (content: {(msg.get('content') or '')[:120]})")
            break

        finished = False
        for tc in asst_msg["tool_calls"]:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, TypeError):
                args = {}

            if name == "finish":
                finish_summary = args.get("summary", "(no summary)")
                finish_confidence = args.get("confidence")
                state.trace.append({"iter": it + 1, "tool": "finish", "args": args})
                events.emit(
                    "agent_finished",
                    agent_id=agent_id,
                    iter=it + 1,
                    summary=finish_summary,
                    confidence=finish_confidence,
                )
                finished = True
                break

            handler = TOOL_HANDLERS.get(name)
            if not handler:
                tool_result = {"error": f"unknown tool: {name}"}
            else:
                try:
                    tool_result = await handler(state, args, pool)
                except Exception as e:
                    log.exception("tool %s failed", name)
                    tool_result = {"error": str(e)}

            # Auto-register lineage metadata for any file_id the tool produced.
            # Source = the user's original input (state.original_fid). This way
            # every agent-spawned artefact shows up labeled with the tool that
            # made it and traces back to the user's upload — so the dashboard
            # left pane stays human-readable regardless of which tool was hit.
            try:
                from app.storage import write_meta, read_meta
                emitted_ids: list[str] = []
                if isinstance(tool_result, dict):
                    for k in ("file_id", "image_file_id", "png_file_id",
                              "svg_file_id", "intermediate_docx", "out_file_id"):
                        v = tool_result.get(k)
                        if isinstance(v, str) and v != state.original_fid:
                            emitted_ids.append(v)
                    # Some tools return a list of slides/images
                    for list_key in ("slides", "rendered_pages", "page_pngs"):
                        for item in tool_result.get(list_key) or []:
                            if isinstance(item, dict):
                                for k in ("image_file_id", "file_id"):
                                    v = item.get(k)
                                    if isinstance(v, str) and v != state.original_fid:
                                        emitted_ids.append(v)
                # Derive a clean label from the source's label + this
                # output's extension, so the user sees `page28.docx` instead
                # of `f09de8…docx` and downloads keep the meaningful name.
                src_meta = read_meta(state.original_fid) or {}
                src_label = (src_meta.get("label")
                             or src_meta.get("original_filename")
                             or state.original_fid)
                src_stem = src_label.rsplit(".", 1)[0] if "." in src_label else src_label
                for fid in emitted_ids:
                    state.spawned_file_ids.append(fid)
                    if read_meta(fid):
                        continue  # already labeled (e.g. convert tool wrote it)
                    out_ext = fid.rsplit(".", 1)[1] if "." in fid else ""
                    write_meta(
                        fid,
                        source_file_id=state.original_fid,
                        operation=f"kimi: {name}",
                        label=f"{src_stem}.{out_ext}" if out_ext else src_stem,
                    )
            except Exception:
                pass  # metadata is cosmetic — never block the agent loop

            # Compact result for trace + UI (don't bloat with full doc text)
            trace_result = (
                {"text_length_chars": tool_result.get("length_chars"),
                 "truncated": tool_result.get("truncated")}
                if name == "read_document"
                else tool_result
            )
            state.trace.append(
                {"iter": it + 1, "tool": name, "args": args, "result": trace_result}
            )
            events.emit(
                "agent_step",
                agent_id=agent_id,
                iter=it + 1,
                tool=name,
                args=args,
                result=trace_result,
            )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(tool_result, ensure_ascii=False)[:8000],
                }
            )

        if finished:
            break

    if finish_summary is None:
        warnings.append("agent did not call finish() within budget")
        finish_summary = "(no summary — agent stopped without finish)"

    # If the agent left the file in DOCX after a PDF input, that's the
    # typo-fix round-trip pattern (PDF → DOCX → edits → ... → DOCX) and the
    # user expects PDF output. For any OTHER final format (pptx, xlsx, html,
    # etc.) we trust the agent's choice — that's a "build new artifact" task
    # where the user explicitly wanted the new format.
    final_fid = state.current_fid
    final_path = _resolve(final_fid)
    final_fmt = detect_from_path(final_path)
    if src_fmt == "pdf" and final_fmt in {"docx", "doc", "odt"}:
        rt = await t_convert(pool, file_id=final_fid, target_format="pdf")
        final_fid = rt["file_id"]
        warnings.append(
            "agent edited PDF via DOCX intermediate; converted back to PDF at the end "
            "(layout may have minor reflow)."
        )

    # No regex-based format detection here on purpose. The agent's system
    # prompt explicitly instructs Kimi to honour user-requested output
    # formats ("docx fájlba", "pdf-ben", "as pptx" etc.) by calling the
    # appropriate builder/convert tools itself. Trying to patch missed
    # format requests with hand-rolled Hungarian inflection regex is exactly
    # the wrong abstraction layer when there's a 1T-parameter multilingual
    # LLM in the loop. If Kimi finishes in the wrong format, the right fix
    # is to strengthen the SYSTEM_PROMPT, not enumerate suffixes here.
    #
    # Sanity check: if the agent finished WITHOUT producing any new file
    # (current_fid == original_fid) and the original was an image, the
    # agent likely didn't follow the IMAGE-TO-DOCUMENT prompt rule. Surface
    # that as a warning so it's visible in the trace, but don't paper over
    # it with a forced jpeg→pdf convert (which yields a useless raster).
    if final_fid == state.original_fid and is_image:
        warnings.append(
            "agent finished without producing a new artefact — the source "
            "image is the only output. If you wanted a transcribed document, "
            "rephrase the request or check the agent trace."
        )

    # Mark every file produced during the run except the final one as
    # ephemeral, so the dashboard sidebar only shows the user-meaningful
    # artefact. The intermediates remain on disk (downloadable via direct
    # link if needed) and the cleanup loop will sweep them on TTL expiry.
    # SAFETY: never mark the original user input as ephemeral — it lives in
    # inputs/ and the user uploaded it, so it must stay visible in the
    # dashboard regardless of what the agent did with it.
    try:
        from app.storage import write_meta
        protected = {final_fid, state.original_fid}
        for fid in set(state.spawned_file_ids):
            if fid in protected:
                continue
            write_meta(fid, extra={"ephemeral": True})
    except Exception:
        pass

    elapsed_ms = int((time.time() - state.started_at) * 1000)
    events.emit(
        "agent_done",
        agent_id=agent_id,
        file_id=final_fid,
        iters=state.iters,
        ms_elapsed=elapsed_ms,
        summary=finish_summary,
    )

    return {
        "executed": True,
        "agent": True,
        "file_id": final_fid,
        "url": public_url(final_fid),
        "iterations": state.iters,
        "summary": finish_summary,
        "confidence": finish_confidence,
        "warnings": warnings,
        "trace": state.trace,
        "model": model_used,
        "ms_elapsed": elapsed_ms,
    }
