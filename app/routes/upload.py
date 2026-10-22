"""Upload route — accepts multipart/form-data file upload, returns file_id."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from app.config import settings
from app.storage import new_file_id, input_path, save_bytes, public_url

router = APIRouter()


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "filename required")
    ext = Path(file.filename).suffix.lstrip(".") or "bin"
    fid = new_file_id(ext)
    data = await file.read()
    from app.filecheck import check_bytes
    check_bytes(data, file.filename)
    await save_bytes(input_path(fid), data)
    return {
        "file_id": fid,
        "size_kb": len(data) // 1024,
        "url": public_url(fid),
    }


# ---------- egyszer használatos link (files(action="upload_link")): az ügynök gépéről, base64 nélkül ----------

@router.put("/upload-link/{token}", status_code=201)
@router.post("/upload-link/{token}", status_code=201)
async def upload_by_link(token: str, request: Request, name: Optional[str] = None):
    """`curl -T fájl "<link>"` (PUT, nyers törzs) vagy `curl -F file=@fájl "<link>"` (multipart) → a fájl a link
    gazdájának munkaterületére; version_of esetén annak új változataként."""
    import os
    from app import tenancy, upload_links
    from app.api_v1 import operations as O
    from app.filecheck import DamagedFile
    from app.storage import display_label, temp_path, write_meta
    d = upload_links.read(token)
    if d is None:
        raise HTTPException(403, "This upload link is not valid or has expired — ask for a new one "
                                 "(files(action=\"upload_link\")).")
    if not await upload_links.claim(d["n"], d["u"]):
        raise HTTPException(409, "This upload link was already used — each link takes one file.")
    tok = tenancy.set_user(d["u"])
    tmp = temp_path(f"uplink_{uuid.uuid4().hex}")
    try:
        limit, n = O.max_bytes(False), 0
        filename = name or d.get("f") or ""
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as f:
            if (request.headers.get("content-type") or "").startswith("multipart/"):
                form = await request.form()
                up = form.get("file")
                if up is None or not hasattr(up, "read"):
                    raise HTTPException(400, "multipart upload: send the file in the field “file” (curl -F file=@path).")
                filename = filename or up.filename or ""
                while chunk := await up.read(1 << 20):
                    n += len(chunk)
                    if n > limit:
                        raise HTTPException(413, f"Larger than {limit // (1024 * 1024)} MB.")
                    f.write(chunk)
            else:
                async for chunk in request.stream():
                    n += len(chunk)
                    if n > limit:
                        raise HTTPException(413, f"Larger than {limit // (1024 * 1024)} MB.")
                    f.write(chunk)
        if n == 0:
            raise HTTPException(400, "The upload was empty — send the file's bytes (curl -T path \"<link>\").")
        base = d.get("v") or None
        if not filename:
            from app.mcp_server import _sniff_ext
            filename = (Path(display_label(base)).stem if base else "upload") + "." + _sniff_ext(tmp)
        try:
            fid = O._save_named(tmp, filename)
        except DamagedFile as e:
            raise HTTPException(400, str(e))
        op = "upload version" if base else "upload link"
        write_meta(fid, original_filename=filename, label=filename, operation=op,
                   **({"source_file_id": base} if base else {}))
        await upload_links.done(d["n"], fid)
    except BaseException:
        await upload_links.release(d["n"])
        raise
    finally:
        if tmp.exists():
            os.unlink(tmp)
        tenancy.reset(tok)
    base_url = settings.PUBLIC_BASE_URL.rstrip("/")
    return {"file_id": fid, "name": filename, "size_kb": n // 1024, "url": public_url(fid),
            "edit_url": f"{base_url}/app/file/{fid}/edit",
            **({"version_of": base, "note": "Saved as a NEW VERSION of that file: files(action=\"compare\", "
                                               f"file_id=\"{fid}\") shows what changed."} if base else {})}
