"""Az ügynök bővítése: a katalógus minden konverziója és eszköze az ügynök kezében is (ugyanazok a kezelők, mint a
weben, az API-ban és az MCP-ben). Az agent.py a végén tölti be: a sémák a TOOL_SCHEMAS-hoz, a kezelők a
TOOL_HANDLERS-hez adódnak."""
from __future__ import annotations

from typing import Any


def _tool_keys() -> list[str]:
    from app.catalog import tools as T
    return [t.key for t in T.live(static=True) if not t.ai]


SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "file_tool",
        "description": ("Run a file tool on the working file (or given file_ids): compress, PDF suite (merge, split, rotate, "
                        "protect, unlock, PDF/A, OCR, watermark, page numbers, redact…), image (resize, crop, rotate, upscale, "
                        "collage), video/audio (trim, crop, GIF, extract audio, volume, subtitles), archives, metadata, "
                        "website capture, forms, signatures. The first result file becomes the working file."),
        "parameters": {"type": "object", "properties": {
            "tool": {"type": "string", "description": "Tool key, e.g. compress-pdf, merge-pdf, resize-image, trim-video."},
            "file_ids": {"type": "array", "items": {"type": "string"}, "description": "Inputs; default = the working file."},
            "options": {"type": "object", "description": "The tool's options (see catalog)."}},
            "required": ["tool"]}}},
    {"type": "function", "function": {
        "name": "convert_any",
        "description": ("Convert the working file to ANY of 264 formats (heic, webp, mobi, epub, mp4, mp3, dwg→pdf, woff2…) "
                        "with options (quality, width, page_range, codec, trim…). Use convert_format for the classic "
                        "office formats; this one for everything else."),
        "parameters": {"type": "object", "properties": {
            "target_format": {"type": "string"}, "options": {"type": "object"},
            "file_id": {"type": "string", "description": "Default = the working file."}},
            "required": ["target_format"]}}},
    {"type": "function", "function": {
        "name": "units_convert",
        "description": "Exact unit conversion (value, from_unit, to_unit) or time-zone conversion (time, from_zone, to_zones).",
        "parameters": {"type": "object", "properties": {
            "value": {"type": "number"}, "from_unit": {"type": "string"}, "to_unit": {"type": "string"},
            "time": {"type": "string"}, "from_zone": {"type": "string"}, "to_zones": {"type": "array", "items": {"type": "string"}}}}}},
]


async def _file_tool(state, args: dict, pool) -> dict:
    from app import ops
    ids = [i for i in args.get("file_ids") or [] if i] or ([state.current_fid] if state.current_fid else [])
    res = await ops.run_tool(args["tool"], ids, args.get("options") or {})
    files = res.get("files") or []
    if files:
        state.current_fid = files[0]["file_id"]
    return {"file_id": state.current_fid, "files": [f["file_id"] for f in files], "data": res.get("data")}


async def _convert_any(state, args: dict, pool) -> dict:
    from app.convert import service
    fid = args.get("file_id") or state.current_fid
    res = await service.convert_file(fid, args["target_format"], args.get("options") or {}, pool)
    state.current_fid = res["file_id"]
    state.keep_format = True
    return {"file_id": res["file_id"], "files": [f["file_id"] for f in res["files"]], "engines": res.get("engines")}


async def _units(state, args: dict, pool) -> dict:
    from app.catalog import units as U
    if args.get("from_zone"):
        return U.tz_convert(args.get("time"), args["from_zone"], args.get("to_zones") or ["UTC"])
    return {"result": U.convert(float(args["value"]), args["from_unit"], args["to_unit"])}


SCHEMAS.append({"type": "function", "function": {
    "name": "form_assistant",
    "description": ("Forms from any file (PDF, DOCX, XLSX, photo): action recognize (field list), fill (values → the form "
                    "in its original format, validated), smart_fill (from the user's saved profile + source_file_ids; "
                    "returns questions for what is missing — ask the user, then call again with answers), sign "
                    "(signature_id into the signature field), profile_get / profile_set (the user's saved data). "
                    "Never invent personal data."),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["recognize", "fill", "smart_fill", "sign", "profile_get", "profile_set"]},
        "file_id": {"type": "string", "description": "The form; default = the working file."},
        "values": {"type": "object"}, "answers": {"type": "object"}, "profile": {"type": "object"},
        "source_file_ids": {"type": "array", "items": {"type": "string"}},
        "signature_id": {"type": "string"}, "flatten": {"type": "boolean"}},
        "required": ["action"]}}})


async def _form_assistant(state, args: dict, pool) -> dict:
    from app import ops, tenancy
    from app.formkit import profile as P
    act = args.get("action")
    uid = tenancy.current_user_id()
    if act == "profile_get":
        return {"profile": await P.load_profile(uid)}
    if act == "profile_set":
        return {"profile": await P.save_profile(uid, args.get("profile") or {})}
    fid = args.get("file_id") or state.current_fid
    if not fid:
        return {"error": "upload the form first"}
    if act == "recognize":
        res = await ops.run_tool("form-recognize", [fid], {})
        return {"fields": [{k: f.get(k) for k in ("name", "label", "type", "required")} for f in res["data"]["fields"]]}
    if act == "sign":
        res = await ops.run_tool("sign-document", [fid], {"signature_id": args.get("signature_id") or ""})
    elif act == "fill":
        res = await ops.run_tool("fill-form", [fid], {"values": args.get("values") or {}, "flatten": bool(args.get("flatten"))})
    else:
        opts = {k: args[k] for k in ("values", "answers", "profile", "signature_id") if args.get(k)}
        if args.get("source_file_ids"):
            opts["source_files"] = args["source_file_ids"]
        res = await ops.run_tool("ai-form-filler", [fid], opts)
    files = res.get("files") or []
    if files:
        state.current_fid = files[0]["file_id"]
        state.keep_format = True
    data = dict(res.get("data") or {})
    data.pop("schema", None)
    return {"file_id": state.current_fid, **data}


HANDLERS = {"file_tool": _file_tool, "convert_any": _convert_any, "units_convert": _units, "form_assistant": _form_assistant}


def register(schemas: list, handlers: dict) -> None:
    names = {s["function"]["name"] for s in schemas}
    schemas.extend(s for s in SCHEMAS if s["function"]["name"] not in names)
    for k, v in HANDLERS.items():
        handlers.setdefault(k, v)
