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
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import Context

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
        "\nposition (page-numbers, watermarks): center | top-left | top | top-right | left | right | bottom-left | bottom | "
        "bottom-right | tile (watermarks); also bottom-center → bottom, top-center → top, middle → center; page numbers go "
        "top-left | top | top-right | bottom-left | bottom | bottom-right (center = bottom)."
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
    files, seen = [], {}
    for t in job["tasks"]:
        for f in (t.get("result") or {}).get("files") or []:
            if f.get("file_id") and not t["operation"].startswith("import/"):
                if f["file_id"] in seen:              # a convert és az export/url ugyanazt a fájlt adja: egyszer
                    if f.get("url") and "url" not in seen[f["file_id"]]:
                        seen[f["file_id"]]["url"] = f["url"]
                    continue
                item = {"file_id": f["file_id"], "label": f.get("filename"), **({"url": f["url"]} if f.get("url") else {}),
                        **({"size_bytes": f["size"]} if isinstance(f.get("size"), int) else {})}
                seen[f["file_id"]] = item
                files.append(item)
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


# ---------------------------------------------------------------- űrlap, aláírás, ügynöki kitöltés

_UI = Path(__file__).parent / "static" / "mcp"
UI_REVIEW, UI_REVIEW_SKY = "ui://forms/form-review.html", "ui://forms/form-review.skybridge.html"
UI_PAD, UI_PAD_SKY = "ui://forms/signature-pad.html", "ui://forms/signature-pad.skybridge.html"


def _ui_html(name: str) -> str:
    """Önálló HTML (a híd beágyazva): a beágyazott keret nem tölthet be külső fájlt."""
    html = (_UI / name).read_text("utf-8")
    return html.replace('<script src="bridge.js"></script>', "<script>\n" + (_UI / "bridge.js").read_text("utf-8") + "\n</script>")


for _uri, _file, _mime in ((UI_REVIEW, "form-review.html", "text/html;profile=mcp-app"),
                           (UI_REVIEW_SKY, "form-review.html", "text/html+skybridge"),
                           (UI_PAD, "signature-pad.html", "text/html;profile=mcp-app"),
                           (UI_PAD_SKY, "signature-pad.html", "text/html+skybridge")):
    mcp.resource(_uri, mime_type=_mime, name=_file.rsplit(".", 1)[0],
                 description="Interactive view for MCP Apps / ChatGPT Apps clients")((lambda f: (lambda: _ui_html(f)))(_file))


def _ui_meta(uri: str, sky: str, invoking: str) -> dict:
    return {"ui": {"resourceUri": uri}, "openai/outputTemplate": sky, "openai/widgetAccessible": True,
            "openai/toolInvocation/invoking": invoking}


async def _tool(key: str, file_ids: list[str], options: dict) -> dict:
    from app import ops, tenancy
    from app.catalog import tools as T
    ops.check_tool_call(T.TOOLS[key], file_ids, options)
    # MCP: a látó/nyelvi munkát a kliens saját ügynöke végzi (a háttérmunka a környezetet örökli)
    tok = tenancy.client_makes_images.set(True)
    try:
        return await _await_or_job(f"tool:{key}", lambda: ops.run_tool(key, file_ids, options))
    finally:
        tenancy.client_makes_images.reset(tok)


@mcp.tool()
@linked
async def forms(action: str = "recognize", file_id: Optional[str] = None, values: Optional[dict] = None,
                schema: Optional[dict] = None, data_file_id: Optional[str] = None, flatten: bool = False,
                keep_look: bool = True, language: Optional[str] = None, template_id: Optional[str] = None,
                period: Optional[str] = None, defaults: Optional[dict] = None, exceptions: Optional[list[dict]] = None,
                profile: Optional[dict] = None, sample_file_id: Optional[str] = None, formats: Optional[list[str]] = None,
                name: Optional[str] = None) -> dict:
    """
    Forms from ANY file: fillable or flat PDF, DOCX, XLSX, photo or scan (also DOC/ODT/XLS/ODS).
    action="recognize": returns one field schema {kind, fields:[{name, label, type, page, box, options, required}]}
      (types: text, multiline, checkbox, date, signature, choice, number, email, phone, iban, hu_tax_id, taj,
      hu_vat, hu_bank, postcode) and a preview PNG with the boxes. You may edit the schema (labels, types, boxes,
      new fields with a box/cell/label, {"name": …, "remove": true}) and pass it back as schema.
      data.quality says whether the free recognition looks complete; if it is "low" (or data.deep.hint is set),
      YOU do the vision work: look at the preview PNG of every page and send the corrected fields back as schema
      (boxes in page units: PDF points or image pixels, origin top left). This server's own AI isn't used over MCP.
    action="fill": values {field name or label: value} → the form filled in its ORIGINAL format (PDF stays PDF,
      DOCX stays DOCX, XLSX stays XLSX; a photo keeps its look unless keep_look=false → clean PDF). Values are
      validated (dates, IBAN, Hungarian tax ID / TAJ / VAT / bank account check digits); errors come back per field.
      flatten=true makes a PDF print-ready. Accents (ő, ű) are kept.
    action="bulk_fill": data_file_id (CSV/XLSX, first row = field names) → one copy per row (PDF: combined; else ZIP).
    action="make_fillable": a flat PDF, DOCX or photo → a PDF with real form fields on its blanks.
    For filling from the user's saved profile and documents (asks for what's missing), use fill_assistant.

    RECURRING SPREADSHEET TEMPLATES (a monthly attendance sheet / timesheet), filled in ONE call, deterministically:
    action="template_learn": file_id (the empty template .xlsx/.xls) + sample_file_id (a filled earlier month)
      (+ period "YYYY-MM" of the sample if the sheet has no day-number column) → a suggested profile from the
      difference + the evidence. Show it to the person; once they approve (or correct) it, register it.
    action="template_register": file_id + profile (+ name) → template_id, kept in the person's account (encrypted).
      profile = {"sheet"?, "header": {"name": "B3", "job_title": "B4", "code": "E4", "month": "E3"},
      "month_format"?: "date" | "{year}. {month}", "days": {"first_row": 8, "step"?: 1} (day d at row first_row + d − 1),
      "columns": {"arrive": "B", "leave": "C", "absence": "D"}, "rules": {"workday": {"arrive": "{start}", "leave":
      "{end}"}, "kinds": {"szabadság": {"leave": "SZABADSÁG", "absence": "+"}}}, "totals"?: {"hours": "E39",
      "absence_days": "D39"}, "header_values"?: {"name": …}, "defaults"?: {"start": "8:00", "end": "16:00"}}.
    action="template_fill": template_id + period "YYYY-MM" (+ defaults {"start", "end", header values} + exceptions
      [{"date": "YYYY-MM-DD" | "from"/"to", "kind": "szabadság"}] + formats ["xlsx", "pdf", "xls"]) → the filled sheet.
      Workdays come from the Hungarian work calendar (Mt. 102. § (1) holidays incl. Easter-based ones, and that year's
      decree of transferred working and rest days; a year without a published decree is an error, never a guess).
      Only given or derived data goes on the sheet: a workday gets the defaults, an exception day its rule, every other
      day stays empty. Times go in as times, dates as dates, numbers as numbers; LibreOffice recalculates every formula
      before the PDF. `check`: workdays and exception days counted, the sheet's own total hours = the expected value,
      no #VALUE!/#REF!, every workday has its entry.
    action="templates": the person's templates; action="template_delete": template_id.
    """
    if action.startswith("template"):
        return await _sheet_template(action, file_id, template_id, period, defaults, exceptions, profile,
                                     sample_file_id, formats, name)
    if not file_id:
        raise ValueError("file_id is required (upload the form first)")
    opts: dict[str, Any] = {}
    if language:
        opts["language"] = language
    if schema:
        opts["schema"] = schema
    if action == "recognize":
        return await _tool("form-recognize", [file_id], opts)
    if action == "fill":
        return await _tool("fill-form", [file_id], {**opts, "values": values or {}, "flatten": flatten, "keep_look": keep_look})
    if action == "bulk_fill":
        if not data_file_id:
            raise ValueError("data_file_id (CSV or XLSX) is required for bulk_fill")
        return await _tool("bulk-fill-form", [file_id], {**{k: v for k, v in opts.items() if k == "schema"},
                                                         "data_file": data_file_id, "flatten": flatten})
    if action == "make_fillable":
        return await _tool("make-fillable", [file_id], {k: v for k, v in opts.items() if k == "language"})
    raise ValueError("action: recognize, fill, bulk_fill, make_fillable, template_learn, template_register, "
                     "template_fill, templates or template_delete")


async def _sheet_template(action, file_id, template_id, period, defaults, exceptions, profile, sample_file_id,
                          formats, name) -> dict:
    from app import tenancy
    from app.formkit import sheet_template as ST
    from app.tools.crafts import existing
    uid = tenancy.current_user_id()
    if action == "templates":
        return {"templates": await ST.list_(uid)}
    if action == "template_learn":
        if not file_id or not sample_file_id:
            raise ValueError("template_learn needs file_id (the empty template) and sample_file_id (a filled month)")
        empty, sample = await ST._to_xlsx(existing(file_id)), await ST._to_xlsx(existing(sample_file_id))
        return ST.learn(empty, sample, period)
    if action == "template_register":
        if not file_id or not profile:
            raise ValueError("template_register needs file_id (the template .xlsx/.xls) and profile (template_learn suggests one)")
        return await ST.register(uid, existing(file_id), profile, name)
    if action == "template_fill":
        if not template_id or not period:
            raise ValueError("template_fill needs template_id and period (YYYY-MM)")
        return await ST.fill(uid, template_id, period, defaults, exceptions, formats)
    if action == "template_delete":
        if not template_id:
            raise ValueError("template_delete needs template_id")
        return {"deleted": await ST.delete(uid, template_id)}
    raise ValueError("template actions: template_learn, template_register, template_fill, templates, template_delete")


@mcp.tool(meta=_ui_meta(UI_PAD, UI_PAD_SKY, "Opening the signature pad…"), structured_output=True)
async def signature(action: str = "list", file_id: Optional[str] = None, image_base64: Optional[str] = None,
                    name: Optional[str] = None, kind: str = "signature", color: Optional[str] = None,
                    signature_id: Optional[str] = None, document_id: Optional[str] = None, placement: str = "auto",
                    page: Optional[int] = None, x: Optional[float] = None, y: Optional[float] = None,
                    width: Optional[float] = None, field: Optional[str] = None) -> dict[str, Any]:
    """
    Signatures and initials, stored encrypted for this user only.
    action="create": from a photo/scan/image (file_id) or a drawing (image_base64 PNG) → a clean transparent PNG
      (paper, shadows and noise removed) and a signature_id to reuse. Clients with interactive UI show a pad.
    action="list" | "delete" (signature_id).
    action="place": put signature_id into document_id (PDF or DOCX): placement "auto" (signature field, then a
      "Signature:/Aláírás:" line or label), "field" (field = name/label), "anchor" (field = text to sign next to),
      or "xy" (page, x, y in points from the top left, width). Returns the signed copy; the original stays.
    """
    from app import tenancy
    from app.formkit import signature as G
    uid = tenancy.current_user_id()
    if action == "list":
        return {"signatures": await G.list_(uid)}
    if action == "delete":
        if not signature_id or not await G.delete(uid, signature_id):
            raise ValueError("no such signature")
        return {"deleted": signature_id}
    if action == "create":
        opts = {k: v for k, v in (("name", name), ("kind", kind), ("color", color), ("image_base64", image_base64)) if v}
        return await _tool("create-signature", [file_id] if file_id else [], opts)
    if action == "place":
        if not document_id:
            raise ValueError("document_id (the PDF or DOCX to sign) is required")
        opts = {k: v for k, v in (("signature_id", signature_id), ("image_file", file_id), ("placement", placement),
                                  ("page", page), ("x", x), ("y", y), ("width", width), ("field", field)) if v is not None}
        return await _tool("sign-document", [document_id], opts)
    raise ValueError("action: create, list, delete or place")


async def _elicit_answers(ctx, questions: list[dict]) -> Optional[dict]:
    """Ha a kliens tud elicitationt, a hiányzó mezőket egy űrlapban kérdezzük meg."""
    from pydantic import Field, create_model
    caps = getattr(getattr(getattr(ctx, "session", None), "client_params", None), "capabilities", None)
    if not caps or not getattr(caps, "elicitation", None):
        return None
    fields, back = {}, {}
    for i, q in enumerate(questions[:20]):
        key = q["name"] if q["name"].isidentifier() and not q["name"].startswith("_") else f"f{i}"
        back[key] = q["name"]
        desc = q["label"] + (f" — {q['error']}" if q.get("error") else "") + (f" ({' / '.join(q['options'])})" if q.get("options") else "")
        typ = Optional[bool] if q["type"] == "checkbox" else Optional[str]
        fields[key] = (typ, Field(default=None, title=q["label"], description=desc))
    Model = create_model("MissingFields", **fields)
    res = await ctx.elicit("A few details are missing for this form:", schema=Model)
    if getattr(res, "action", "") != "accept" or not getattr(res, "data", None):
        return None
    data = res.data.model_dump() if hasattr(res.data, "model_dump") else dict(res.data)
    return {back[k]: v for k, v in data.items() if v not in (None, "")}


@mcp.tool(meta=_ui_meta(UI_REVIEW, UI_REVIEW_SKY, "Filling the form…"), structured_output=True)
async def fill_assistant(ctx: Context, action: str = "fill", file_id: Optional[str] = None,
                         source_file_ids: Optional[list[str]] = None, values: Optional[dict] = None,
                         answers: Optional[dict] = None, profile: Optional[dict] = None, save_profile: bool = False,
                         signature_id: Optional[str] = None, schema: Optional[dict] = None,
                         flatten: bool = False) -> dict[str, Any]:
    """
    Agentic form filling. Fills file_id (any form: PDF, DOCX, XLSX, photo) from the user's saved profile, the
    documents in source_file_ids (ID card scan, earlier forms, contracts…) and values/answers you pass; validates
    everything (dates, IBAN, Hungarian tax ID, TAJ, VAT number, bank account, email, phone) and returns a preview.
    Missing or invalid fields: if this client supports elicitation the user is asked right here; otherwise the
    result has status "needs_input" and questions — ask the user and call again with answers {field: value}.
    signature_id (or the profile's) is placed into signature fields. Clients with interactive UI get a review view
    with a signature pad. action="profile_get" | "profile_set" (profile {full_name, birth_date, birth_place,
    mother_name, address, email, phone, tax_id, taj, iban, …}, merged; stored encrypted).
    Never invent personal data: only use what the profile, the documents or the user gave.
    """
    from app.formkit import profile as P
    from app import tenancy
    uid = tenancy.current_user_id()
    if action == "profile_get":
        return {"profile": await P.load_profile(uid), "fields": list(P.PROFILE_KEYS)}
    if action == "profile_set":
        return {"profile": await P.save_profile(uid, profile or {})}
    if action != "fill":
        raise ValueError("action: fill, profile_get or profile_set")
    if not file_id:
        raise ValueError("file_id (the form) is required")
    if profile and save_profile:
        await P.save_profile(uid, profile)
    opts: dict[str, Any] = {k: v for k, v in (("values", values), ("answers", answers), ("profile", profile),
                                              ("source_files", source_file_ids), ("signature_id", signature_id),
                                              ("schema", schema)) if v}
    opts["flatten"] = flatten
    res = await _tool("ai-form-filler", [file_id], opts)
    if "data" not in res and res.get("status") in ("queued", "running"):
        return res                                            # hosszú munka: job_id-vel kérdezhető (jobs eszköz)
    data = res.get("data") or {}
    if data.get("status") == "needs_input" and data.get("questions"):
        try:
            got = await _elicit_answers(ctx, data["questions"])
        except Exception:
            got = None
        if got:
            opts["answers"] = {**(answers or {}), **got}
            res = await _tool("ai-form-filler", [file_id], opts)
            data = res.get("data") or {}
    return {**data, "files": res.get("files", []), "file_id": res.get("file_id"), "url": res.get("url"),
            "form_file_id": file_id}
