"""Az API műveletei: import/*, convert, tool/<eszköz>, archive, merge, thumbnail, metadata, capture-website, export/*.

Egy task eredménye mindig {"files": [{file_id, filename, size, format}, …]} (+ "url" az export/url-nél, "data" az
adat-visszaadó műveleteknél, "form" az import/upload-nál). A taskok a job tulajdonosának munkaterületén futnak
(a tenancy a job indításakor öröklődik), így a fájlok ugyanott vannak, ahol a webes felületen.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from app.catalog import tools as T
from app.catalog.formats import from_filename, normalize
from app.config import settings


class OpError(Exception):
    def __init__(self, msg: str, code: str = "INVALID_TASK"):
        super().__init__(msg)
        self.code = code


SANDBOX_MAX = 10 * 1024 * 1024


def max_bytes(sandbox: bool) -> int:
    return SANDBOX_MAX if sandbox else int(os.getenv("API_MAX_FILE_MB", "2048")) * 1024 * 1024


CLOUD = ("s3", "azure", "gcs", "sftp", "google-drive", "dropbox", "onedrive")
CORE = ("convert", "archive", "merge", "thumbnail", "metadata", "capture-website")
_PROCESSING_PREFIX = ("convert", "tool/", "archive", "merge", "thumbnail", "metadata", "capture-website")


_TASK_KEYS = {"operation", "input", "options", "output_format", "input_format", "filename", "preset_id"}


def task_options(op: str, p: dict[str, Any]) -> dict[str, Any]:
    """A feladat opciói: az `options` objektum, plusz a feladat legfelső szintjén megadott ismert opciók
    (CloudConvert-stílus: {"operation": "tool/watermark-pdf", "text": "…"})."""
    from app.convert.options import OPTIONS
    opts = dict(p.get("options") or {})
    if op.startswith("tool/"):
        allowed = set(T.TOOLS[op[5:]].options)
    elif op == "convert":
        allowed = set(OPTIONS)
    else:
        return opts
    for k, v in p.items():
        if k in allowed and k not in _TASK_KEYS and k not in opts:
            opts[k] = v
    return opts


def known(op: str) -> bool:
    if op in ("import/upload", "import/url", "import/base64", "import/file", "export/url"):
        return True
    if op.startswith(("import/", "export/")):
        return op.split("/", 1)[1] in CLOUD
    if op.startswith("tool/"):
        return op[5:] in T.TOOLS
    return op in CORE


def validate(op: str, p: dict[str, Any], has_input: bool) -> None:
    from app.convert.options import OptionError, validate as vopts
    needs_input = op not in ("import/upload", "import/url", "import/base64", "import/file", "capture-website") \
        and not op.startswith("import/") and not (op.startswith("tool/") and T.TOOLS[op[5:]].min_files == 0)
    if needs_input and not has_input:
        raise _je(f"{op} needs an input task")
    if op.startswith("import/") and has_input:
        raise _je(f"{op} takes no input")
    if op == "import/url" and not (isinstance(p.get("url"), str) and p["url"].startswith(("http://", "https://"))):
        raise _je("import/url: url (http/https) is required")
    if op == "import/base64" and not (isinstance(p.get("file"), str) and isinstance(p.get("filename"), str)):
        raise _je("import/base64: file (base64) and filename are required")
    if op == "import/file" and not isinstance(p.get("file_id"), str):
        raise _je("import/file: file_id is required")
    if op == "convert":
        if not isinstance(p.get("output_format"), str):
            raise _je("convert: output_format is required")
        try:
            vopts(task_options(op, p))
        except OptionError as e:
            raise _je(f"convert: {e}")
    if op.startswith("tool/"):
        try:
            vopts(task_options(op, p), tool=True)
        except OptionError as e:
            raise _je(f"{op}: {e}")
    if op == "capture-website" and not (isinstance(p.get("url"), str) and p["url"].startswith(("http://", "https://"))):
        raise _je("capture-website: url is required")
    if op.startswith(("import/", "export/")) and op.split("/", 1)[1] in CLOUD:
        from app.connectors import validate_spec
        try:
            validate_spec(op.split("/", 1)[1], p, export=op.startswith("export/"))
        except ValueError as e:
            raise _je(f"{op}: {e}")


def _je(msg: str):
    from app.api_v1.jobs import JobError
    return JobError(msg, "INVALID_TASK")


# ---------- segédek ----------

def _file_entry(fid: str) -> dict[str, Any]:
    from app.storage import display_label
    from app.tools import crafts
    p = crafts.existing(fid)
    return {"file_id": fid, "filename": display_label(fid), "size": p.stat().st_size, "format": from_filename(p.name)}


def _input_files(inputs: list) -> list[str]:
    out: list[str] = []
    for t in inputs:
        for f in (t.result or {}).get("files") or []:
            if f.get("file_id"):
                out.append(f["file_id"])
    if not out:
        raise OpError("the input task produced no files", "NO_INPUT_FILES")
    return out


def _save_named(data_path: Path, filename: str) -> str:
    from app.storage import input_path, write_meta
    from app.storage.files import new_file_id
    ext = from_filename(filename) or (filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin")
    fid = new_file_id(ext)
    os.replace(data_path, input_path(fid))
    write_meta(fid, original_filename=filename, label=filename, operation="api import")
    return fid


def upload_token(task_id: str, exp: int) -> str:
    msg = f"upload|{task_id}|{exp}".encode()
    return f"{exp}.{hmac.new(settings.FILE_SIGNING_KEY.encode(), msg, hashlib.sha256).hexdigest()[:32]}"


def check_upload_token(task_id: str, token: str) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    return exp > time.time() and hmac.compare_digest(upload_token(task_id, exp), token)


async def _charge(job, task) -> None:
    if job.sandbox:
        return
    from app import billing, db
    try:
        task.credits = await billing.api_task(job.owner, 1)
    except db.InsufficientCredits:
        raise OpError("Your monthly API task allowance is used up and there are not enough credits. Top up under Plans.",
                      "PAYMENT_REQUIRED")


# ---------- végrehajtás ----------

async def execute(job, task, inputs: list) -> dict[str, Any]:
    op, p = task.operation, task.payload
    if op.startswith(_PROCESSING_PREFIX):
        await _charge(job, task)
    if op == "import/upload":
        return await _import_upload(job, task)
    if op == "import/url":
        return await _import_url(job, task)
    if op == "import/base64":
        return await _import_b64(job, task)
    if op == "import/file":
        try:
            return {"files": [_file_entry(p["file_id"])]}
        except (FileNotFoundError, ValueError):
            raise OpError(f"file not found: {p['file_id']}", "FILE_NOT_FOUND")
    if op == "convert":
        return await _convert(job, task, _input_files(inputs))
    if op.startswith("tool/"):
        return await _tool(op[5:], _input_files(inputs) if inputs else [], task_options(op, p))
    if op == "archive":
        opts = {"archive_format": p.get("output_format", "zip"), **({"password": p["password"]} if p.get("password") else {}),
                **({"filename": p["filename"]} if p.get("filename") else {})}
        return await _tool("create-archive", _input_files(inputs), opts)
    if op == "merge":
        fmt = normalize(p.get("output_format") or "pdf")
        return await _tool("merge-files", _input_files(inputs), {"output_format": fmt})
    if op == "thumbnail":
        opts = {k: p[k] for k in ("width", "height", "image_format", "page") if k in p}
        return await _tool("thumbnail", _input_files(inputs), opts)
    if op == "metadata":
        return await _tool("read-metadata", _input_files(inputs)[:1], {})
    if op == "capture-website":
        fmt = (p.get("output_format") or "pdf").lower()
        key = "website-to-pdf" if fmt == "pdf" else "website-to-image"
        opts = {"url": p["url"], **{k: p[k] for k in ("full_page", "wait_ms", "page_size", "width") if k in p}}
        if key == "website-to-image":
            opts["image_format"] = fmt
        return await _tool(key, [], opts)
    if op == "export/url":
        return _export_url(_input_files(inputs), p)
    if op.startswith(("import/", "export/")):
        from app import connectors
        provider = op.split("/", 1)[1]
        if op.startswith("import/"):
            path, name = await connectors.fetch(provider, p, max_bytes(job.sandbox), _tmpdir())
            return {"files": [_file_entry(_save_named(path, name))]}
        return {"files": await connectors.push(provider, p, _input_files(inputs))}
    raise OpError(f"unknown operation {op}", "UNKNOWN_OPERATION")


def _tmpdir() -> Path:
    from app.storage import temp_path
    from app.storage.files import new_file_id
    d = temp_path(new_file_id("api"))
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _import_upload(job, task) -> dict[str, Any]:
    exp = int(time.time()) + (3600 if job.sandbox else 24 * 3600)
    tok = upload_token(task.id, exp)
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    url = f"{base}/v1/upload/{task.id}?token={tok}"
    task.upload_event = asyncio.Event()
    task.result = {"form": {"url": url, "method": "POST", "field": "file", "parameters": {},
                            "chunks": {"put": f"{base}/v1/upload/{task.id}/chunks/{{n}}?token={tok}",
                                       "complete": f"{base}/v1/upload/{task.id}/complete?token={tok}",
                                       "max_chunk_mb": 50}}}
    job.emit("task.updated", task.public())
    from app.api_v1 import store
    await store.save_task(job, task)
    try:
        await asyncio.wait_for(task.upload_event.wait(), exp - time.time())
    except asyncio.TimeoutError:
        raise OpError("no file was uploaded in time", "UPLOAD_TIMEOUT")
    fid = (task.result or {}).get("uploaded")
    if not fid:
        raise OpError("upload failed", "UPLOAD_FAILED")
    return {"files": [_file_entry(fid)]}


async def receive_upload(job, task, src: Path, filename: str) -> str:
    """Az upload-végpont hívja: a fájl a munkaterületre kerül, a task folytatódik."""
    if src.stat().st_size > max_bytes(job.sandbox):
        src.unlink(missing_ok=True)
        raise OpError(f"the file is larger than {max_bytes(job.sandbox) // (1024 * 1024)} MB", "FILE_TOO_LARGE")
    fid = _save_named(src, filename or task.payload.get("filename") or "upload")
    task.result = {**(task.result or {}), "uploaded": fid}
    if task.upload_event:
        task.upload_event.set()
    return fid


async def _import_url(job, task) -> dict[str, Any]:
    from app import net
    p = task.payload
    tmp = _tmpdir() / "dl"
    try:
        name, size, _ = await net.download(p["url"], tmp, max_bytes(job.sandbox), headers=p.get("headers"))
    except net.UnsafeURL as e:
        raise OpError(str(e), "UNSAFE_URL")
    except ValueError as e:
        raise OpError(str(e), "DOWNLOAD_FAILED")
    except Exception as e:
        raise OpError(f"download failed: {e}", "DOWNLOAD_FAILED")
    return {"files": [_file_entry(_save_named(tmp, p.get("filename") or name))]}


async def _import_b64(job, task) -> dict[str, Any]:
    p = task.payload
    data = p["file"]
    if len(data) > (14 * 1024 * 1024 if not job.sandbox else SANDBOX_MAX * 4 // 3 + 4):
        raise OpError("import/base64 is for files up to 10 MB — use import/upload for larger files", "FILE_TOO_LARGE")
    try:
        raw = base64.b64decode(data.split(",", 1)[1] if data.startswith("data:") else data, validate=True)
    except (binascii.Error, ValueError):
        raise OpError("file is not valid base64", "INVALID_BASE64")
    tmp = _tmpdir() / "b64"
    tmp.write_bytes(raw)
    return {"files": [_file_entry(_save_named(tmp, p["filename"]))]}


async def _convert(job, task, file_ids: list[str]) -> dict[str, Any]:
    from app.convert import service
    from app.convert.options import OptionError
    p = task.payload
    opts = task_options("convert", p)
    if p.get("preset_id"):
        from app import db
        row = await db.preset_get(job.owner, int(p["preset_id"])) if db.enabled() else None
        if not row:
            raise OpError(f"no such preset: {p['preset_id']}", "PRESET_NOT_FOUND")
        opts = {**row["options"], **opts}
    if p.get("filename"):
        opts["filename"] = p["filename"]
    files = []
    for i, fid in enumerate(file_ids):
        if p.get("input_format"):
            fmt = normalize(p["input_format"])
            if from_filename(fid) != fmt:
                fid = _retag(fid, fmt)
        try:
            res = await service.convert_file(fid, p["output_format"], opts, split=True)
        except (service.ConversionError, OptionError) as e:
            raise OpError(str(e), "CONVERSION_NOT_POSSIBLE")
        except FileNotFoundError as e:
            raise OpError(str(e), "FILE_NOT_FOUND")
        except Exception as e:
            raise OpError(str(e)[:500], "CONVERSION_FAILED")
        files += [_file_entry(f["file_id"]) for f in res["files"]]
        task.percent = int(100 * (i + 1) / len(file_ids))
    return {"files": files, "engines": res.get("engines")}


def _retag(fid: str, fmt: str) -> str:
    """A hívó megmondta a forrás formátumát (pl. kiterjesztés nélküli letöltés): másolat a helyes kiterjesztéssel."""
    import shutil
    from app.storage import input_path
    from app.storage.files import new_file_id
    from app.tools import crafts
    new = new_file_id(fmt.replace(".", "_") if "." in fmt else fmt)
    if "." in fmt:
        new = new.rsplit(".", 1)[0] + "." + fmt
    shutil.copy(crafts.existing(fid), input_path(new))
    return new


async def _tool(key: str, file_ids: list[str], options: dict[str, Any]) -> dict[str, Any]:
    from app import ops
    from app.convert.options import OptionError
    if key not in T.TOOLS or not T.available(key):
        raise OpError(f"tool {key} isn't available on this server", "TOOL_UNAVAILABLE")
    try:
        res = await ops.run_tool(key, file_ids, options)
    except (OptionError, ValueError) as e:
        raise OpError(str(e), "INVALID_OPTIONS")
    except FileNotFoundError as e:
        raise OpError(str(e), "FILE_NOT_FOUND")
    out: dict[str, Any] = {"files": [_file_entry(f["file_id"]) for f in res.get("files", [])]}
    if res.get("data") is not None:
        out["data"] = res["data"]
    return out


def _export_url(file_ids: list[str], p: dict[str, Any]) -> dict[str, Any]:
    from app.storage import public_url
    from app.storage.files import sign_file_id
    ttl = int(p.get("expires_in") or settings.FILE_TTL_HOURS * 3600)
    ttl = max(60, min(ttl, 7 * 24 * 3600))
    files = []
    for fid in file_ids:
        e = _file_entry(fid)
        try:
            from app import tenancy
            tok, exp = sign_file_id(fid, ttl, uid=tenancy.current_user_id())
            base = settings.PUBLIC_BASE_URL.rstrip("/")
            uid = tenancy.current_user_id()
            e["url"] = f"{base}/files/{fid}?t={tok}" + ("" if uid == tenancy.ADMIN_ID else f"&u={uid}")
            e["expires_at"] = exp
        except TypeError:
            e["url"] = public_url(fid)
        files.append(e)
    return {"files": files}
