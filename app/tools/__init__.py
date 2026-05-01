from app.tools.convert import convert
from app.tools.edits import (
    find_replace,
    delete_section,
    insert_after,
    replace_heading,
    delete_pages,
    merge,
    extract,
    style_paragraph,
    style_all_headings,
    insert_image,
)
from app.tools.pptx_builder import (
    pptx_create,
    pptx_add_slide,
    pptx_add_bullets_slide,
    pptx_add_chart_slide,
    pptx_set_slide_styles,
    pptx_slide_count,
)
from app.tools.edit_ai import edit_with_instruction

__all__ = [
    "convert",
    "find_replace",
    "delete_section",
    "insert_after",
    "replace_heading",
    "delete_pages",
    "merge",
    "extract",
    "style_paragraph",
    "style_all_headings",
    "insert_image",
    "pptx_create",
    "pptx_add_slide",
    "pptx_add_bullets_slide",
    "pptx_set_slide_styles",
    "pptx_slide_count",
    "edit_with_instruction",
]
