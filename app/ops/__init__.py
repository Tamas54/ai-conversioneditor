"""Eszközök végrehajtása egy helyen (web, REST API, MCP, ügynök): bemeneti fájlok → kezelő → új munkaterület-fájlok.

A kezelők (app/ops/handlers/*) útvonalakkal dolgoznak: `async def h(inputs: list[Path], options: dict, out: Path)`,
és Path-listát vagy {"files": [...], "data": {...}} szótárat adnak vissza. Itt történik a validálás, a keret-
fogyasztás (billing), a kimenetek elnevezése és a metaadat.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Optional

from app.catalog import tools as T
from app.catalog.formats import from_filename
from app.convert.options import OptionError, validate

log = logging.getLogger("aice.ops")


def check_tool_call(tool: T.Tool, file_ids: list[str], options: dict[str, Any]) -> dict[str, Any]:
    opts = validate(options, tool=True)
    unknown = [k for k in opts if k not in tool.options and k not in ("output", "filename")]
    if unknown:
        raise OptionError(f"{tool.key} doesn't take: {', '.join(unknown)}. Options: {', '.join(tool.options) or 'none'}")
    if len(file_ids) < tool.min_files:
        raise ValueError(f"{tool.key} needs at least {tool.min_files} file(s).")
    if tool.max_files and len(file_ids) > tool.max_files:
        raise ValueError(f"{tool.key} takes at most {tool.max_files} file(s).")
    for fid in file_ids:
        fmt = from_filename(fid)
        if fmt and not T.accepts(tool, fmt) and not (tool.key == "add-subtitles" and fmt in ("srt", "vtt", "ass")):
            raise ValueError(f"{tool.key} doesn't take .{fmt} files.")
    return opts


def _resolve(fid: str) -> Path:
    from app.tools import crafts
    try:
        return crafts.existing(fid)
    except (FileNotFoundError, ValueError):
        raise FileNotFoundError(f"file not found: {fid}")


async def _charge(tool: T.Tool, n_inputs: int, pages: int = 0) -> None:
    from app import billing
    if tool.quota in ("form_pages",):
        await billing.use("form_pages", max(1, pages or n_inputs))
    elif tool.quota == "scan_pages":
        await billing.scan_pages(max(1, pages or n_inputs))


def _pages(paths: list[Path]) -> int:
    n = 0
    for p in paths:
        if p.suffix.lower() == ".pdf":
            try:
                import pymupdf
                with pymupdf.open(str(p)) as d:
                    n += d.page_count
            except Exception:
                n += 1
        else:
            n += 1
    return n


async def run_tool(key: str, file_ids: list[str], options: Optional[dict[str, Any]] = None,
                   charge: bool = True) -> dict[str, Any]:
    """Vissza: {"files": [{file_id, url, label, size_kb}], "data": {...}?, "tool": key}."""
    from app.storage import display_label, output_path, public_url, temp_path, write_meta
    from app.storage.files import new_file_id
    tool = T.get(key)
    if tool is None or not T.available(key):
        raise ValueError(f"no such tool: {key}")
    opts = check_tool_call(tool, file_ids, dict(options or {}))
    out_mode = opts.pop("output", "auto")
    filename = opts.pop("filename", None)
    inputs = [_resolve(f) for f in file_ids]
    if charge:
        await _charge(tool, len(inputs), _pages(inputs) if tool.quota in ("form_pages", "scan_pages") else 0)
    work = temp_path(new_file_id("op"))
    work.mkdir(parents=True)
    handler = T.handler(key)
    try:
        opts["_resolve"] = _resolve
        res = await handler(inputs, opts, work)
        paths, data = (res.get("files") or [], res.get("data")) if isinstance(res, dict) else (res or [], None)
        names = (res.get("names") if isinstance(res, dict) else None) or []
        files = []
        src_id = file_ids[0] if file_ids else None
        stem = Path(filename).stem if filename else (Path(display_label(src_id)).stem if src_id else key)
        for i, p in enumerate(paths, 1):
            ext = from_filename(p.name) or p.suffix.lstrip(".") or "bin"
            fid = new_file_id(ext)
            shutil.move(str(p), output_path(fid))
            nice = i18n_tag(key)
            arc = names[i - 1] if i - 1 < len(names) else None      # a kezelő saját neve (pl. archívumbeli útvonal)
            label = arc.rsplit("/", 1)[-1] if arc else f"{stem}{f' ({nice})' if nice else ''}{f'-{i:03d}' if len(paths) > 1 else ''}.{ext}"
            write_meta(fid, source_file_id=src_id, operation=f"tool {key}", label=label)
            files.append({"file_id": fid, "url": public_url(fid), "label": label, "size_kb": output_path(fid).stat().st_size // 1024,
                          **({"path": arc} if arc else {})})
        out: dict[str, Any] = {"tool": key, "files": files}
        if files:
            out.update(file_id=files[0]["file_id"], url=files[0]["url"], label=files[0]["label"])
        if data is not None:
            out["data"] = data
        if len(files) > 1 and (out_mode == "zip" or (out_mode == "auto" and tool.output == "zip")):
            out = await _zip_result(out, stem)
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def i18n_tag(key: str) -> str:
    """A kimeneti fájlnév címkéje (angolul, rövid): „report (compressed).pdf”."""
    from app.site import i18n
    return i18n.raw(i18n.DEFAULT, f"tool.{key}.tag", "") or ""


async def _zip_result(out: dict[str, Any], stem: str) -> dict[str, Any]:
    import zipfile
    from app.storage import output_path, public_url, write_meta
    from app.storage.files import new_file_id
    fid = new_file_id("zip")
    with zipfile.ZipFile(output_path(fid), "w", zipfile.ZIP_DEFLATED) as z:
        for f in out["files"]:
            z.write(output_path(f["file_id"]), f.get("path") or f["label"])
    label = f"{stem}.zip"
    write_meta(fid, operation=f"tool {out['tool']}", label=label)
    zf = {"file_id": fid, "url": public_url(fid), "label": label, "size_kb": output_path(fid).stat().st_size // 1024}
    return {**out, **zf, "files": [zf] + out["files"]}
