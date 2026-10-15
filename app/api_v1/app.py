"""A nyilvános REST API (/v1) — külön FastAPI-alkalmazás, saját OpenAPI-val (/v1/openapi.json).

Hitelesítés: Authorization: Bearer sk_… (élő) vagy sk_test_… (sandbox: nincs kreditfogyasztás, 10 MB, 1 óra
megőrzés). A kulcsot a fő app auth-middleware-je oldja fel felhasználóvá (tenancy); itt csak megköveteljük,
hogy kulccsal jöjjön a kérés, és kulcsonként korlátozzuk a kérésszámot.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Optional, Union

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app import tenancy
from app.api_v1 import jobs as J
from app.config import settings

DESCRIPTION = """Convert, edit and process files: {formats} formats, {conversions} conversion directions, {tools} tools.

A **job** is a set of named **tasks**. A task's `input` is the name of an earlier task. Typical job:
`import/url` → `convert` (or `tool/<key>`) → `export/url`. Follow it with `GET /jobs/{{id}}?wait=30`,
Server-Sent Events (`/jobs/{{id}}/events`) or a webhook.

Authentication: `Authorization: Bearer sk_…`. Sandbox keys (`sk_test_…`) don't use credits (files up to 10 MB).
"""

v1 = FastAPI(title=f"{settings.PRODUCT_NAME} API", version="1.0.0", docs_url=None, redoc_url=None,
             openapi_url="/openapi.json", description="")


# ---------- hibák egységes alakban ----------

def _err(status: int, code: str, message: str, headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)


@v1.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        return _err(exc.status_code, detail["code"], detail.get("message", ""), getattr(exc, "headers", None))
    code = {400: "INVALID_REQUEST", 401: "UNAUTHENTICATED", 402: "PAYMENT_REQUIRED", 403: "FORBIDDEN", 404: "NOT_FOUND",
            413: "FILE_TOO_LARGE", 429: "RATE_LIMITED"}.get(exc.status_code, "ERROR")
    return _err(exc.status_code, code, str(detail), getattr(exc, "headers", None))


@v1.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
    return _err(422, "INVALID_REQUEST", f"{loc}: {first.get('msg', 'invalid')}")


@v1.exception_handler(J.JobError)
async def _job_error(request: Request, exc: J.JobError):
    return _err(422, exc.code, str(exc))


# ---------- kulcs, sandbox, rate limit ----------

_hits: dict[str, deque] = defaultdict(deque)


class Ctx:
    def __init__(self, uid: str, key: str):
        self.uid = uid
        self.key = key
        self.sandbox = key.startswith("sk_test_")


def rate_limit() -> int:
    return int(os.getenv("API_RATE_PER_MIN", "60"))


def ctx(request: Request) -> Ctx:
    from app.auth import _given_key
    key = _given_key(request)
    uid = tenancy.current_user_id()
    if not key or tenancy.is_guest(uid):
        raise HTTPException(401, {"code": "UNAUTHENTICATED", "message": "Send your API key: Authorization: Bearer sk_…"})
    c = Ctx(uid, key)
    limit = rate_limit() // (2 if c.sandbox else 1)
    h = hashlib.sha256(key.encode()).hexdigest()[:24]
    q, now = _hits[h], time.time()
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= limit:
        retry = int(60 - (now - q[0])) + 1
        raise HTTPException(429, {"code": "RATE_LIMITED", "message": f"Limit is {limit} requests per minute."},
                            headers={"Retry-After": str(retry), "X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": "0"})
    q.append(now)
    request.state.rl = (limit, limit - len(q))
    return c


@v1.middleware("http")
async def _rl_headers(request: Request, call_next):
    resp = await call_next(request)
    rl = getattr(request.state, "rl", None)
    if rl:
        resp.headers["X-RateLimit-Limit"], resp.headers["X-RateLimit-Remaining"] = str(rl[0]), str(rl[1])
    return resp


# ---------- modellek (OpenAPI) ----------

class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="allow", json_schema_extra={"examples": [
        {"operation": "import/url", "url": "https://example.com/report.docx"},
        {"operation": "convert", "input": "in", "output_format": "pdf", "options": {"page_range": "1-3"}},
        {"operation": "tool/compress-pdf", "input": "pdf", "options": {"compression": "recommended"}},
        {"operation": "export/url", "input": ["pdf"]}]})
    operation: str = Field(..., description="import/upload, import/url, import/base64, import/file, import/s3, import/azure, "
                                            "import/gcs, import/sftp, import/google-drive, import/dropbox, import/onedrive, convert, "
                                            "tool/<key>, archive, merge, thumbnail, metadata, capture-website, export/url, export/s3, …")
    input: Optional[Union[str, list[str]]] = Field(None, description="Name(s) of earlier task(s) whose files this task takes.")
    output_format: Optional[str] = Field(None, description="convert/archive/merge/capture-website: target format, e.g. pdf, jpg, mp4.")
    input_format: Optional[str] = Field(None, description="convert: override the detected source format.")
    options: Optional[dict[str, Any]] = Field(None, description="Conversion or tool options (see /catalog/operations); "
                                                                "the same options apply to every input file of the task.")
    preset_id: Optional[int] = Field(None, description="convert: start from a saved preset (options given here override it).")
    url: Optional[str] = Field(None, description="import/url, capture-website: http(s) address.")
    filename: Optional[str] = None


class JobCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"tag": "invoice-42", "tasks": {
        "in": {"operation": "import/url", "url": "https://example.com/invoice.docx"},
        "pdf": {"operation": "convert", "input": "in", "output_format": "pdf"},
        "out": {"operation": "export/url", "input": "pdf"}}}]})
    tasks: dict[str, TaskSpec]
    tag: Optional[str] = Field(None, max_length=120)
    webhook_url: Optional[str] = Field(None, description="Called on job.finished and job.failed (signed, see /me).")


class FileOut(BaseModel):
    file_id: str
    filename: str
    size: int
    format: str
    url: Optional[str] = None


class TaskOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    name: str
    operation: str
    status: str = Field(..., description="waiting, processing, finished, error, cancelled")
    depends_on: list[str]
    message: Optional[str] = None
    code: Optional[str] = None
    percent: int = 0
    credits: int = 0
    result: Optional[dict[str, Any]] = None


class JobOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    tag: Optional[str] = None
    status: str = Field(..., description="waiting, processing, finished, error, cancelled")
    sandbox: bool
    credits: int = 0
    tasks: list[TaskOut]


class WebhookIn(BaseModel):
    url: str
    events: list[str] = Field(default_factory=lambda: ["job.finished", "job.failed"])


class ConvertIn(BaseModel):
    url: Optional[str] = Field(None, description="Source URL (or file_id, or file + filename as base64).")
    file_id: Optional[str] = None
    file: Optional[str] = Field(None, description="Base64 content (up to 10 MB).")
    filename: Optional[str] = None
    output_format: str
    options: Optional[dict[str, Any]] = None
    wait: int = Field(60, ge=0, le=300, description="Seconds to wait for the result.")


# ---------- végpontok ----------

@v1.get("/me", tags=["account"])
async def me(request: Request) -> dict[str, Any]:
    c = ctx(request)
    from app import billing, db
    from app.api_v1 import webhooks
    plan = await billing.plan_of(c.uid)
    user = await db.user_get(c.uid) if db.enabled() and c.uid != tenancy.ADMIN_ID else None
    used = await db.usage_get(c.uid, billing.month_key(), "api_tasks") if db.enabled() else 0
    return {"user_id": c.uid, "plan": plan, "sandbox": c.sandbox, "credits": None if c.uid == tenancy.ADMIN_ID else (user or {}).get("credits"),
            "api_tasks": {"used": used, "monthly_allowance": billing.PLANS[plan].get("api_tasks"), "per_credit_above": billing.API_TASKS_PER_CREDIT},
            "rate_limit_per_minute": rate_limit() // (2 if c.sandbox else 1), "webhook_secret": webhooks.secret(c.uid),
            "max_file_mb": (10 if c.sandbox else int(os.getenv("API_MAX_FILE_MB", "2048")))}


@v1.get("/catalog/formats", tags=["catalog"])
async def catalog_formats(category: Optional[str] = None) -> dict[str, Any]:
    from app.catalog.formats import FORMATS
    return {"formats": [{"key": f.key, "category": f.category, "name": f.name, "mime": f.mime, "aliases": list(f.aliases)}
                        for f in FORMATS.values() if not category or f.category == category]}


@v1.get("/catalog/conversions", tags=["catalog"])
async def catalog_conversions(from_: str = Query(..., alias="from"), to: Optional[str] = None) -> dict[str, Any]:
    from app.convert import graph
    from app.convert.options import for_pair
    if to:
        plan = graph.plan(from_, to, static=True)
        if not plan:
            raise HTTPException(404, {"code": "CONVERSION_NOT_POSSIBLE", "message": f"{from_} → {to} isn't supported"})
        return {"from": from_, "to": to, "engines": [e.engine for e in plan], "options": [o.key for o in for_pair(from_, to)],
                "available_here": bool(graph.plan(from_, to))}
    return {"from": from_, "targets": graph.targets(from_, static=True)}


@v1.get("/catalog/operations", tags=["catalog"])
async def catalog_operations() -> dict[str, Any]:
    from app.api_v1.catalog import catalog_ops
    from app.catalog import tools as T
    from app.convert.options import json_schema
    return {"operations": [{"operation": op, "description": d} for op, d in catalog_ops()],
            "tools": {t.key: {"options": json_schema(list(t.options), tool=True), "min_files": t.min_files, "max_files": t.max_files,
                              "accepts": list(t.accepts)} for t in T.live(static=True)},
            "convert_options": json_schema()}


@v1.post("/jobs", status_code=201, response_model=JobOut, tags=["jobs"])
async def create_job(request: Request, body: JobCreate) -> dict[str, Any]:
    c = ctx(request)
    spec = {"tasks": {k: v.model_dump(exclude_none=True) for k, v in body.tasks.items()}, "tag": body.tag,
            "webhook_url": body.webhook_url}
    if body.webhook_url:
        from app import net
        try:
            await net.check_url(body.webhook_url)
        except net.UnsafeURL as e:
            raise J.JobError(f"webhook_url: {e}")
    job = J.build(c.uid, spec, c.sandbox)
    J.start(job)
    await asyncio.sleep(0)
    return job.public()


@v1.get("/jobs", tags=["jobs"])
async def list_jobs(request: Request, status: Optional[str] = None, tag: Optional[str] = None, limit: int = Query(50, le=200)):
    c = ctx(request)
    live = {j.id: j.public() for j in J.list_jobs(c.uid, status, tag, limit)}
    from app.api_v1 import store
    rows = [r for r in await store.list_db(c.uid, status, tag, limit) if r["id"] not in live]
    data = list(live.values()) + rows
    return {"data": data[:limit]}


async def _job_or_404(c: Ctx, job_id: str) -> Union[J.Job, dict]:
    j = J.get(job_id, c.uid)
    if j:
        return j
    from app.api_v1 import store
    d = await store.load_job(job_id, c.uid)
    if d:
        return d
    raise HTTPException(404, {"code": "NOT_FOUND", "message": "No such job."})


@v1.get("/jobs/{job_id}", response_model=JobOut, tags=["jobs"])
async def get_job(request: Request, job_id: str, wait: int = Query(0, ge=0, le=60)) -> dict[str, Any]:
    c = ctx(request)
    j = await _job_or_404(c, job_id)
    if isinstance(j, dict):
        return j
    if wait and not j.done.is_set():
        try:
            await asyncio.wait_for(j.done.wait(), wait)
        except asyncio.TimeoutError:
            pass
    return j.public()


@v1.get("/jobs/{job_id}/events", tags=["jobs"])
async def job_events(request: Request, job_id: str):
    from sse_starlette.sse import EventSourceResponse
    c = ctx(request)
    j = await _job_or_404(c, job_id)
    if isinstance(j, dict):
        async def once():
            yield {"event": "job.finished" if j["status"] == "finished" else "job.failed", "data": json.dumps(j, default=str)}
        return EventSourceResponse(once())
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    j.listeners.append(q)

    async def gen():
        try:
            yield {"event": "job.snapshot", "data": json.dumps(j.public(), default=str)}
            if j.done.is_set():
                yield {"event": "job.finished" if j.status == "finished" else "job.failed", "data": json.dumps(j.public(), default=str)}
                return
            while True:
                ev = await q.get()
                yield {"event": ev["event"], "data": json.dumps(ev["data"], default=str)}
                if ev["event"] in ("job.finished", "job.failed"):
                    return
        finally:
            if q in j.listeners:
                j.listeners.remove(q)
    return EventSourceResponse(gen(), ping=15)


@v1.delete("/jobs/{job_id}", tags=["jobs"])
async def delete_job(request: Request, job_id: str) -> dict[str, Any]:
    c = ctx(request)
    j = J.get(job_id, c.uid)
    if not j:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "No such job."})
    await J.cancel(j)
    from app.storage import delete_meta, output_path, input_path
    removed = 0
    for t in j.tasks.values():
        for f in (t.result or {}).get("files") or []:
            for p in (output_path(f["file_id"]), input_path(f["file_id"])):
                if p.exists():
                    p.unlink()
                    removed += 1
            delete_meta(f["file_id"])
    return {"id": j.id, "status": j.status, "files_deleted": removed}


@v1.get("/tasks/{task_id}", response_model=TaskOut, tags=["jobs"])
async def get_task(request: Request, task_id: str) -> dict[str, Any]:
    c = ctx(request)
    hit = J.task_by_id(task_id)
    if not hit or hit[0].owner != c.uid:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "No such task."})
    return hit[1].public()


# ---------- feltöltés (import/upload) — a tokenes cím kulcs nélkül is hívható ----------

def _upload_task(task_id: str, token: str):
    from app.api_v1.operations import check_upload_token
    if not check_upload_token(task_id, token):
        raise HTTPException(403, {"code": "FORBIDDEN", "message": "Invalid or expired upload token."})
    hit = J.task_by_id(task_id)
    if not hit or hit[1].operation != "import/upload" or hit[1].status != "processing":
        raise HTTPException(409, {"code": "UPLOAD_NOT_EXPECTED", "message": "This task isn't waiting for an upload."})
    return hit


@v1.post("/upload/{task_id}", status_code=201, tags=["upload"])
async def upload(task_id: str, token: str, file: UploadFile = File(...)) -> dict[str, Any]:
    from app.api_v1.operations import OpError, max_bytes, receive_upload
    job, task = _upload_task(task_id, token)
    tok = tenancy.set_user(job.owner)
    try:
        from app.storage import temp_path
        from app.storage.files import new_file_id
        tmp = temp_path(new_file_id("up"))
        limit = max_bytes(job.sandbox)
        n = 0
        with open(tmp, "wb") as f:
            while chunk := await file.read(1 << 20):
                n += len(chunk)
                if n > limit:
                    f.close()
                    tmp.unlink(missing_ok=True)
                    raise HTTPException(413, {"code": "FILE_TOO_LARGE", "message": f"Larger than {limit // (1024 * 1024)} MB."})
                f.write(chunk)
        try:
            fid = await receive_upload(job, task, tmp, file.filename or "upload")
        except OpError as e:
            raise HTTPException(413, {"code": e.code, "message": str(e)})
    finally:
        tenancy.reset(tok)
    return {"task_id": task_id, "file_id": fid, "size": n}


@v1.put("/upload/{task_id}/chunks/{n}", tags=["upload"])
async def upload_chunk(request: Request, task_id: str, n: int, token: str) -> dict[str, Any]:
    job, task = _upload_task(task_id, token)
    if not 0 <= n < 10000:
        raise HTTPException(400, "chunk index 0–9999")
    tok = tenancy.set_user(job.owner)
    try:
        from app.storage import temp_path
        d = temp_path(f"chunks_{task_id}")
        d.mkdir(parents=True, exist_ok=True)
        size = 0
        with open(d / f"{n:05d}", "wb") as f:
            async for part in request.stream():
                size += len(part)
                if size > 50 * 1024 * 1024:
                    raise HTTPException(413, {"code": "CHUNK_TOO_LARGE", "message": "Chunks up to 50 MB."})
                f.write(part)
        total = sum(p.stat().st_size for p in d.iterdir())
        from app.api_v1.operations import max_bytes
        if total > max_bytes(job.sandbox):
            shutil.rmtree(d, ignore_errors=True)
            raise HTTPException(413, {"code": "FILE_TOO_LARGE", "message": "The file is too large."})
    finally:
        tenancy.reset(tok)
    return {"chunk": n, "size": size}


class CompleteIn(BaseModel):
    filename: str
    chunks: int = Field(..., ge=1, le=10000)


@v1.post("/upload/{task_id}/complete", status_code=201, tags=["upload"])
async def upload_complete(task_id: str, token: str, body: CompleteIn) -> dict[str, Any]:
    from app.api_v1.operations import OpError, receive_upload
    job, task = _upload_task(task_id, token)
    tok = tenancy.set_user(job.owner)
    try:
        from app.storage import temp_path
        from app.storage.files import new_file_id
        d = temp_path(f"chunks_{task_id}")
        parts = [d / f"{i:05d}" for i in range(body.chunks)]
        missing = [i for i, p in enumerate(parts) if not p.exists()]
        if missing:
            raise HTTPException(400, {"code": "MISSING_CHUNKS", "message": f"missing chunks: {missing[:20]}"})
        out = temp_path(new_file_id("up"))
        with open(out, "wb") as w:
            for p in parts:
                with open(p, "rb") as r:
                    shutil.copyfileobj(r, w)
        shutil.rmtree(d, ignore_errors=True)
        try:
            fid = await receive_upload(job, task, out, body.filename)
        except OpError as e:
            raise HTTPException(413, {"code": e.code, "message": str(e)})
    finally:
        tenancy.reset(tok)
    return {"task_id": task_id, "file_id": fid}


# ---------- webhookok ----------

@v1.get("/webhooks", tags=["webhooks"])
async def webhooks_list(request: Request) -> dict[str, Any]:
    from app.api_v1 import webhooks
    c = ctx(request)
    return {"data": await webhooks.list_hooks(c.uid), "secret": webhooks.secret(c.uid)}


@v1.post("/webhooks", status_code=201, tags=["webhooks"])
async def webhooks_create(request: Request, body: WebhookIn) -> dict[str, Any]:
    from app import net
    from app.api_v1 import webhooks
    c = ctx(request)
    try:
        return await webhooks.create_hook(c.uid, body.url, body.events)
    except net.UnsafeURL as e:
        raise HTTPException(422, {"code": "UNSAFE_URL", "message": str(e)})


@v1.delete("/webhooks/{hook_id}", tags=["webhooks"])
async def webhooks_delete(request: Request, hook_id: int) -> dict[str, Any]:
    from app.api_v1 import webhooks
    c = ctx(request)
    if not await webhooks.delete_hook(c.uid, hook_id):
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "No such webhook."})
    return {"deleted": hook_id}


# ---------- mentett beállítások (presetek) — ugyanazok, mint a weboldalon ----------

class PresetIn(BaseModel):
    name: str = Field(..., max_length=80)
    scope: str = Field(..., max_length=40, description="A target format (e.g. webp) or a tool key (e.g. compress-pdf).")
    options: dict[str, Any]


@v1.get("/presets", tags=["presets"])
async def presets_list(request: Request, scope: Optional[str] = None) -> dict[str, Any]:
    from app import db
    c = ctx(request)
    return {"data": await db.preset_list(c.uid, scope) if db.enabled() else []}


@v1.post("/presets", status_code=201, tags=["presets"])
async def presets_create(request: Request, body: PresetIn) -> dict[str, Any]:
    from app import db
    from app.convert.options import OptionError, validate
    c = ctx(request)
    if not db.enabled():
        raise HTTPException(503, {"code": "UNAVAILABLE", "message": "Presets need the database."})
    try:
        opts = validate(body.options, tool="-" in body.scope)
    except OptionError as e:
        raise HTTPException(422, {"code": "INVALID_OPTIONS", "message": str(e)})
    return await db.preset_save(c.uid, body.name.strip(), body.scope.strip().lower(), opts)


@v1.delete("/presets/{preset_id}", tags=["presets"])
async def presets_delete(request: Request, preset_id: int) -> dict[str, Any]:
    from app import db
    c = ctx(request)
    if not db.enabled() or not await db.preset_delete(c.uid, preset_id):
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "No such preset."})
    return {"deleted": preset_id}


# ---------- aláírások, adatprofil (titkosítva tárolva) ----------

@v1.get("/signatures", tags=["signatures"])
async def signatures_list(request: Request) -> dict[str, Any]:
    from app.formkit import signature as G
    c = ctx(request)
    return {"data": await G.list_(c.uid)}


@v1.get("/signatures/{sid}/image", tags=["signatures"], response_class=Response)
async def signatures_image(request: Request, sid: str) -> Response:
    from app.formkit import signature as G
    c = ctx(request)
    try:
        png = await G.load(c.uid, sid)
    except KeyError:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "No such signature."})
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, no-store"})


@v1.delete("/signatures/{sid}", tags=["signatures"])
async def signatures_delete(request: Request, sid: str) -> dict[str, Any]:
    from app.formkit import signature as G
    c = ctx(request)
    if not await G.delete(c.uid, sid):
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "No such signature."})
    return {"deleted": sid}


@v1.get("/profile", tags=["profile"])
async def profile_get(request: Request) -> dict[str, Any]:
    from app.formkit import profile as P
    c = ctx(request)
    return {"data": await P.load_profile(c.uid), "fields": list(P.PROFILE_KEYS)}


@v1.put("/profile", tags=["profile"])
async def profile_put(request: Request, body: dict[str, Any], merge: bool = True) -> dict[str, Any]:
    from app.formkit import profile as P
    c = ctx(request)
    try:
        return {"data": await P.save_profile(c.uid, body, merge=merge)}
    except ValueError as e:
        raise HTTPException(422, {"code": "INVALID_PROFILE", "message": str(e)})


@v1.delete("/profile", tags=["profile"])
async def profile_delete(request: Request) -> dict[str, Any]:
    from app.formkit import profile as P
    c = ctx(request)
    return {"deleted": await P.delete_profile(c.uid)}


# ---------- egylépéses rövidítés (Zapier, Make, n8n) ----------

@v1.post("/convert", tags=["shortcuts"], response_model=JobOut)
async def convert_shortcut(request: Request, body: ConvertIn) -> dict[str, Any]:
    c = ctx(request)
    if body.url:
        first = {"operation": "import/url", "url": body.url, **({"filename": body.filename} if body.filename else {})}
    elif body.file_id:
        first = {"operation": "import/file", "file_id": body.file_id}
    elif body.file and body.filename:
        first = {"operation": "import/base64", "file": body.file, "filename": body.filename}
    else:
        raise HTTPException(422, {"code": "INVALID_REQUEST", "message": "Give url, file_id, or file + filename."})
    job = J.build(c.uid, {"tasks": {"import": first, "convert": {"operation": "convert", "input": "import",
                                                                  "output_format": body.output_format, "options": body.options or {}},
                                    "export": {"operation": "export/url", "input": "convert"}}}, c.sandbox)
    J.start(job)
    if body.wait:
        try:
            await asyncio.wait_for(job.done.wait(), body.wait)
        except asyncio.TimeoutError:
            pass
    return job.public()


@v1.get("/units/convert", tags=["utilities"])
async def units_convert(value: float, from_: str = Query(..., alias="from"), to: str = Query(...)) -> dict[str, Any]:
    from app.catalog import units as U
    try:
        return {"value": value, "from": from_, "to": to, "result": U.convert(value, from_, to)}
    except U.UnitError as e:
        raise HTTPException(422, {"code": "INVALID_UNIT", "message": str(e)})


@v1.get("/timezones/convert", tags=["utilities"])
async def timezones_convert(from_: str = Query(..., alias="from"), to: str = "UTC", time: Optional[str] = None) -> dict[str, Any]:
    from app.catalog import units as U
    try:
        return U.tz_convert(time, from_, [z for z in to.split(",") if z.strip()])
    except U.UnitError as e:
        raise HTTPException(422, {"code": "INVALID_TIME", "message": str(e)})


# ---------- OpenAPI ----------

def _openapi() -> dict[str, Any]:
    if v1.openapi_schema:
        return v1.openapi_schema
    from fastapi.openapi.utils import get_openapi
    from app.site import content
    c = content.counts()
    schema = get_openapi(title=v1.title, version=v1.version, routes=v1.routes,
                         description=DESCRIPTION.format(**c))
    schema["servers"] = [{"url": settings.PUBLIC_BASE_URL.rstrip("/") + "/v1"}]
    schema.setdefault("components", {})["securitySchemes"] = {"bearer": {"type": "http", "scheme": "bearer",
                                                                         "description": "sk_… (live) or sk_test_… (sandbox)"}}
    schema["security"] = [{"bearer": []}]
    v1.openapi_schema = schema
    return schema


v1.openapi = _openapi
