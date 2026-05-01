"""Sidecar metadata for files — labels, source lineage, operation hints.

Writes `{file_id}.meta.json` next to the actual file. The metadata is purely
cosmetic + traceability — `file_id` remains the authoritative identifier in
all APIs, signed URLs, and tool calls. Missing metadata is handled gracefully
by the caller (display_label falls back to a short version of file_id).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from app.storage.files import input_path, output_path

log = logging.getLogger("aice.storage.meta")


_META_SUFFIX = ".meta.json"


def _meta_path_for(file_id: str) -> Path:
    """Locate the meta sidecar by checking where the actual file lives.
    Defaults to outputs/ if the file doesn't exist yet (most common case
    for write-during-creation)."""
    out = output_path(file_id)
    if out.exists():
        return out.parent / f"{file_id}{_META_SUFFIX}"
    in_ = input_path(file_id)
    if in_.exists():
        return in_.parent / f"{file_id}{_META_SUFFIX}"
    # Default: outputs (most callers create outputs)
    return output_path(file_id).parent / f"{file_id}{_META_SUFFIX}"


def write_meta(
    file_id: str, *,
    label: Optional[str] = None,
    source_file_id: Optional[str] = None,
    operation: Optional[str] = None,
    original_filename: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Write/merge the sidecar. Best-effort — never raises (metadata is
    cosmetic, must not block real operations)."""
    try:
        p = _meta_path_for(file_id)
        existing: dict[str, Any] = {}
        if p.exists():
            try:
                existing = json.loads(p.read_text("utf-8"))
            except Exception:
                existing = {}
        merged = dict(existing)
        if label is not None:
            merged["label"] = label
        if source_file_id is not None:
            merged["source_file_id"] = source_file_id
        if operation is not None:
            merged["operation"] = operation
        if original_filename is not None:
            merged["original_filename"] = original_filename
        if extra:
            merged.update(extra)
        merged.setdefault("created_at", time.time())
        merged["updated_at"] = time.time()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(merged, ensure_ascii=False, indent=2), "utf-8")
    except Exception as e:
        log.warning("write_meta failed for %s: %s", file_id, e)


def read_meta(file_id: str) -> Optional[dict[str, Any]]:
    """Return the merged sidecar, or None if absent/unreadable."""
    p = _meta_path_for(file_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return None


def delete_meta(file_id: str) -> bool:
    """Remove the sidecar for a deleted file. Returns True if removed."""
    # Check both inputs and outputs because the file is already gone
    for d in (output_path(file_id).parent, input_path(file_id).parent):
        p = d / f"{file_id}{_META_SUFFIX}"
        if p.exists():
            try:
                p.unlink()
                return True
            except OSError:
                pass
    return False


def display_label(file_id: str, meta: Optional[dict[str, Any]] = None) -> str:
    """Best-effort human label. Priority: explicit label → original_filename
    → short uuid + ext. Operation is NOT folded into the label — it's shown
    as a separate tag in the UI, and folding it in produces ugly verbose
    labels like 'kimi: docx_add_paragraph · f09de873.docx'."""
    if meta is None:
        meta = read_meta(file_id)
    if meta:
        if meta.get("label"):
            return str(meta["label"])
        if meta.get("original_filename"):
            return str(meta["original_filename"])
    parts = file_id.rsplit(".", 1)
    short = parts[0][:8] if parts and parts[0] else file_id[:8]
    ext = parts[1] if len(parts) == 2 else ""
    return f"{short}.{ext}" if ext else short


def is_meta_path(name: str) -> bool:
    """Check if a filename is a sidecar (used by directory listing to filter)."""
    return name.endswith(_META_SUFFIX)


def new_with_meta(
    ext: str, *,
    source_file_id: Optional[str] = None,
    operation: Optional[str] = None,
    label: Optional[str] = None,
    original_filename: Optional[str] = None,
) -> str:
    """Generate a new file_id and immediately register its metadata.
    Returns the file_id. Convenience wrapper for tools that produce outputs."""
    from app.storage.files import new_file_id
    fid = new_file_id(ext)
    write_meta(
        fid,
        label=label, source_file_id=source_file_id,
        operation=operation, original_filename=original_filename,
    )
    return fid
