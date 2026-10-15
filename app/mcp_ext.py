"""Csoportosított MCP-eszközök a bővítéshez (az mcp_server.py tölti be, a meglévő eszközök mellé).

    file_tools   minden fájleszköz egy hívásban (tömörítés, PDF-csomag, kép, videó, hang, vízjel, web, archívum, metaadat)
    transfer     import URL-ről vagy felhőből (S3, Azure, GCS, SFTP, Drive, Dropbox, OneDrive), export ugyanoda
    jobs         többlépéses feladat (import → convert/tool → export), ugyanaz, mint a REST /v1/jobs
    catalog      formátumok, átalakítási irányok, eszközök és opciók (csak olvas)
    units        mértékegység- és időzóna-átváltás (csak olvas)

A leírások a katalógusból készülnek, így mindig a ténylegesen elérhető eszközöket sorolják fel.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from app.mcp_server import linked, mcp


def _tools_doc() -> str:
    from app.catalog import tools as T
    from app.site import i18n
    groups = []
    for g, tl in T.by_group().items():
        if g in ("forms", "signature", "utility"):
            continue
        items = "; ".join(f"{t.key} ({', '.join(t.options) or 'no options'})" for t in tl)
        groups.append(f"{i18n.t('en', f'tgroup.{g}.name')}: {items}.")
    return (
        "Run a file tool. tool = one of the keys below; file_ids = the input files (upload first with upload_file / "
        "upload_from_url / transfer); options = the tool's options. Returns new file(s) — the originals stay. Long tasks "
        "(video, OCR) return a job: call again with job_id until status is \"done\".\n" + "\n".join(groups) +
        "\nAdvanced: ffmpeg-advanced / imagemagick-advanced take allow-listed flags in options.args (e.g. [\"-crf\", \"20\"])."
    )


async def _await_or_job(kind: str, coro_factory, wait_s: float = 45) -> dict[str, Any]:
    from app import jobs

    async def work(progress):
        progress(0, 1, kind)
        res = await coro_factory()
        progress(1, 1, "done")
        return res
    job = jobs.start(kind, work)
    task = jobs._JOBS[job["id"]]["_task"]
    try:
        await asyncio.wait_for(asyncio.shield(task), wait_s)
    except asyncio.TimeoutError:
        return {**jobs._public(jobs._JOBS[job["id"]]), "hint": "Still running — call again with job_id."}
    j = jobs._public(jobs._JOBS[job["id"]])
    if j["status"] == "failed":
        raise ValueError(j.get("error") or "the tool failed")
    return j["result"]


async def _file_tools(tool: str, file_ids: Optional[list[str]] = None, options: Optional[dict] = None,
                      job_id: Optional[str] = None) -> dict:
    from app import jobs, ops
    from app.catalog import tools as T
    if job_id:
        j = await jobs.job_status(job_id)
        return j.get("result") or j if j.get("status") == "done" else j
    t = T.get(tool)
    if not t or not T.available(tool):
        raise ValueError(f"Unknown tool {tool!r}. Tools: {', '.join(x.key for x in T.live(static=False))}")
    if t.group in ("forms", "signature"):
        raise ValueError("Forms and signatures have their own tools: forms, signature, fill_assistant.")
    ids = [i for i in (file_ids or []) if i]
    ops.check_tool_call(t, ids, options or {})
    return await _await_or_job(f"tool:{tool}", lambda: ops.run_tool(tool, ids, options or {}))


_file_tools.__name__ = "file_tools"
_file_tools.__doc__ = _tools_doc()
file_tools = mcp.tool()(linked(_file_tools))


@mcp.tool()
@linked
async def transfer(action: str, provider: str = "url", url: Optional[str] = None, file_ids: Optional[list[str]] = None,
                   settings: Optional[dict] = None, filename: Optional[str] = None) -> dict:
    """
    Bring files in from the web or a cloud store, or send results out.

    action="import": provider="url" with url (public http/https), or provider = s3 | azure | gcs | sftp |
    google-drive | dropbox | onedrive with settings (credentials and location — e.g. s3: bucket, key, region,
    access_key_id, secret_access_key[, endpoint for S3-compatible]; dropbox: path, access_token; google-drive:
    file_id, access_token). Returns a file_id in the person's workspace.
    action="export": file_ids + provider (same settings; the target key/path may end with "/" for a folder),
    or provider="url" for temporary signed download links. Credentials are used for this call only and never stored.
    """
    from app.api_v1 import operations as O
    from app.storage import public_url
    s = dict(settings or {})
    if action == "import":
        if provider == "url":
            from app import net
            if not url:
                raise ValueError("url is required")
            tmp = O._tmpdir() / "dl"
            name, size, _ = await net.download(url, tmp, O.max_bytes(False))
            fid = O._save_named(tmp, filename or name)
            return {"file_id": fid, "url": public_url(fid), "label": filename or name, "size_kb": size // 1024}
        from app import connectors
        path, name = await connectors.fetch(provider, s, O.max_bytes(False), O._tmpdir())
        fid = O._save_named(path, filename or name)
        return {"file_id": fid, "url": public_url(fid), "label": filename or name}
    if action == "export":
        ids = [i for i in (file_ids or []) if i]
        if not ids:
            raise ValueError("file_ids is required")
        if provider == "url":
            return O._export_url(ids, s)
        from app import connectors
        return {"files": await connectors.push(provider, s, ids)}
    raise ValueError("action is import or export")


@mcp.tool()
@linked
async def jobs(action: str = "create", tasks: Optional[dict] = None, job_id: Optional[str] = None, wait: int = 30) -> dict:
    """
    Multi-step pipelines, same as the REST API /v1/jobs. action="create" with tasks = {name: {operation, input, …}}:
    operations import/url, import/file (file_id), import/base64, import/s3 … → convert (output_format, options) or
    tool/<key> (options) or archive / merge / thumbnail / metadata / capture-website → export/url or export/<cloud>.
    Tasks without dependencies run in parallel. action="status" (job_id, waits up to `wait` s), action="cancel".
    Example: {"in": {"operation": "import/url", "url": "…"}, "pdf": {"operation": "convert", "input": "in",
    "output_format": "pdf"}, "out": {"operation": "export/url", "input": "pdf"}}.
    """
    from app import tenancy
    from app.api_v1 import jobs as J
    uid = tenancy.current_user_id()
    if action == "create":
        job = J.build(uid, {"tasks": tasks or {}}, sandbox=False)
        J.start(job)
        try:
            await asyncio.wait_for(job.done.wait(), max(0, min(wait, 55)))
        except asyncio.TimeoutError:
            pass
        return _job_out(job.public())
    j = J.get(job_id or "", uid)
    if not j:
        raise ValueError("No such job.")
    if action == "cancel":
        await J.cancel(j)
        return _job_out(j.public())
    if not j.done.is_set():
        try:
            await asyncio.wait_for(j.done.wait(), max(0, min(wait, 55)))
        except asyncio.TimeoutError:
            pass
    return _job_out(j.public())


def _job_out(job: dict) -> dict:
    """MCP-barát alak: a fájlok kiemelve (file_id → szerkesztő/előnézet link a linked burkolóban)."""
    files = []
    for t in job["tasks"]:
        for f in (t.get("result") or {}).get("files") or []:
            if f.get("file_id") and not t["operation"].startswith("import/"):
                files.append({"file_id": f["file_id"], "label": f.get("filename"), **({"url": f["url"]} if f.get("url") else {})})
    return {"job_id": job["id"], "status": job["status"], "files": files,
            "tasks": [{k: t.get(k) for k in ("name", "operation", "status", "code", "message")} for t in job["tasks"]]}


@mcp.tool()
async def catalog(kind: str = "conversions", source_format: Optional[str] = None, target_format: Optional[str] = None,
                  query: Optional[str] = None) -> dict:
    """
    Look up what is possible (read-only). kind="conversions" with source_format → all target formats (with
    target_format → engine chain and options); kind="formats" (query filters by name/extension); kind="tools"
    → every file tool with its options; kind="options" with target_format → conversion options and their ranges.
    """
    from app.catalog import tools as T
    from app.catalog.formats import FORMATS, normalize
    from app.convert import graph
    from app.convert.options import for_pair, for_target
    if kind == "formats":
        q = (query or "").lower()
        return {"formats": [{"key": f.key, "category": f.category, "name": f.name} for f in FORMATS.values()
                            if not q or q in f.key or q in f.name.lower()]}
    if kind == "tools":
        return {"tools": [{"key": t.key, "group": t.group, "options": list(t.options), "accepts": list(t.accepts)} for t in T.live()]}
    if kind == "options":
        if not target_format:
            raise ValueError("target_format is required")
        opts = for_pair(normalize(source_format), normalize(target_format)) if source_format else for_target(normalize(target_format))
        return {"options": {o.key: o.schema() for o in opts}}
    if not source_format:
        raise ValueError("source_format is required")
    s = normalize(source_format)
    if target_format:
        p = graph.plan(s, normalize(target_format))
        if not p:
            return {"possible": False, "targets": graph.targets(s)}
        return {"possible": True, "engines": [e.engine for e in p], "options": [o.key for o in for_pair(s, normalize(target_format))]}
    return {"source_format": s, "targets": graph.targets(s)}


@mcp.tool()
async def units(value: Optional[float] = None, from_unit: Optional[str] = None, to_unit: Optional[str] = None,
                time: Optional[str] = None, from_zone: Optional[str] = None, to_zones: Optional[list[str]] = None) -> dict:
    """
    Exact unit and time-zone conversion (read-only). Units: value + from_unit + to_unit (length, weight,
    temperature, area, volume, speed, time, data, energy, pressure, power, angle, frequency, fuel; e.g. mi→km,
    c→f, l100km→mpg). Time zones: from_zone + to_zones (IANA names like Europe/Budapest, or CET, EST…) and
    time "YYYY-MM-DD HH:MM" (empty = now), daylight saving included.
    """
    from app.catalog import units as U
    if from_zone:
        return U.tz_convert(time, from_zone, to_zones or ["UTC"])
    if value is None or not from_unit or not to_unit:
        raise ValueError("give value, from_unit and to_unit (or from_zone and to_zones)")
    return {"value": value, "from": from_unit, "to": to_unit, "result": U.convert(value, from_unit, to_unit)}
