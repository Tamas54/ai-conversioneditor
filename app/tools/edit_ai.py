"""
edit_with_instruction — natural-language editing.

Pipeline:
    1. Load source file (DOCX/MD/HTML; PDF is auto-round-tripped to DOCX)
    2. Extract plain text content for the LLM context
    3. Call Kimi K2.6 (fallback DeepSeek V3.2) to generate edit-operation plan
    4. If confidence < threshold and require_confidence=True → return plan
       without executing (caller decides)
    5. Execute operations sequentially via the deterministic edit primitives
    6. If original was PDF, render result back to PDF
    7. Return final file URL + the plan that was executed
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from app.llm.siliconflow import plan_edits
from app.storage import (
    input_path,
    new_file_id,
    output_path,
    public_url,
)
from app.tools import edits as edit_ops
from app.tools.convert import convert
from app.tools.formats import detect_from_path

log = logging.getLogger("aice.edit_ai")

CONFIDENCE_THRESHOLD = 0.7

# Map plan op-types to deterministic functions
OP_HANDLERS = {
    "find_replace": lambda fid, op: edit_ops.find_replace(
        fid, op["pattern"], op["replacement"], op.get("regex", False)
    ),
    "delete_section": lambda fid, op: edit_ops.delete_section(
        fid, op["heading"], op.get("level")
    ),
    "insert_after": lambda fid, op: edit_ops.insert_after(
        fid, op["anchor"], op["content"]
    ),
    "replace_heading": lambda fid, op: edit_ops.replace_heading(
        fid, op["old_title"], op["new_title"]
    ),
    "delete_pages": lambda fid, op: edit_ops.delete_pages(fid, op["page_range"]),
}


async def edit_with_instruction(
    pool,
    *,
    file_id: str,
    instruction: str,
    require_confidence: bool = True,
    return_format: Optional[str] = None,
) -> dict:
    t0 = time.time()
    src = input_path(file_id)
    if not src.exists():
        src = output_path(file_id)
    if not src.exists():
        raise FileNotFoundError(f"file_id not found: {file_id}")

    src_fmt = detect_from_path(src)
    layout_warning = None

    # PDF round-trip: convert to DOCX first
    working_fid = file_id
    working_fmt = src_fmt
    if src_fmt == "pdf":
        log.info("PDF input — round-tripping to DOCX for editing")
        rt = await convert(
            pool, file_id=file_id, target_format="docx"
        )
        working_fid = rt["file_id"]
        working_fmt = "docx"
        layout_warning = (
            "Input was PDF; converted to DOCX for editing then back. "
            "Layout may have minor reflow vs. the original."
        )

    # Extract document text for LLM context
    extracted = await edit_ops.extract(working_fid, mode="text")
    doc_text = extracted["text"]

    # Plan edits via LLM (give it the format so it picks format-appropriate ops)
    plan, model_used = await plan_edits(doc_text, instruction, format_hint=working_fmt)
    confidence = float(plan.get("confidence", 0.0))
    operations = plan.get("operations", [])
    warnings = list(plan.get("warnings", []))
    if layout_warning:
        warnings.append(layout_warning)

    # Confidence gate
    if require_confidence and confidence < CONFIDENCE_THRESHOLD:
        return {
            "executed": False,
            "reason": "low_confidence",
            "confidence": confidence,
            "warnings": warnings,
            "plan": plan,
            "model": model_used,
            "ms_elapsed": int((time.time() - t0) * 1000),
        }

    # Execute operations sequentially. Each op produces a new file_id;
    # we chain them so the next op edits the previous result.
    current_fid = working_fid
    executed_ops = []
    for op in operations:
        op_type = op.get("type")
        handler = OP_HANDLERS.get(op_type)
        if not handler:
            warnings.append(f"unknown op type skipped: {op_type}")
            continue
        try:
            res = await handler(current_fid, op)
            current_fid = res["file_id"]
            executed_ops.append({"op": op, "result_file_id": current_fid})
        except Exception as e:
            log.exception("op failed: %s", op_type)
            warnings.append(f"op {op_type} failed: {e}")

    # Convert back to original format (or requested format)
    final_fmt = return_format or src_fmt
    if final_fmt != working_fmt:
        rt = await convert(pool, file_id=current_fid, target_format=final_fmt)
        current_fid = rt["file_id"]

    return {
        "executed": True,
        "file_id": current_fid,
        "url": public_url(current_fid),
        "confidence": confidence,
        "warnings": warnings,
        "operations_executed": len(executed_ops),
        "operations": executed_ops,
        "model": model_used,
        "ms_elapsed": int((time.time() - t0) * 1000),
    }
