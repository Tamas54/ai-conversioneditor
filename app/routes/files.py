"""File-serving route — signed token required."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.storage import (
    display_label, input_path, output_path, verify_file_token,
)

router = APIRouter()


@router.get("/files/{file_id}")
async def get_file(file_id: str, t: str = Query(..., description="signed token")):
    if not verify_file_token(file_id, t):
        raise HTTPException(403, "invalid or expired token")
    p = output_path(file_id)
    if not p.exists():
        p = input_path(file_id)
    if not p.exists():
        raise HTTPException(404, "file not found")
    # Use the human label as the download filename so the user gets
    # `page28.docx` instead of `f09de87…docx`. Falls back to file_id.
    download_name = display_label(file_id) or file_id
    return FileResponse(p, filename=download_name)
