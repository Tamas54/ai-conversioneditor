"""Upload route — accepts multipart/form-data file upload, returns file_id."""
from __future__ import annotations

from pathlib import Path
from fastapi import APIRouter, File, UploadFile, HTTPException

from app.storage import new_file_id, input_path, save_bytes, public_url

router = APIRouter()


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "filename required")
    ext = Path(file.filename).suffix.lstrip(".") or "bin"
    fid = new_file_id(ext)
    data = await file.read()
    await save_bytes(input_path(fid), data)
    return {
        "file_id": fid,
        "size_kb": len(data) // 1024,
        "url": public_url(fid),
    }
