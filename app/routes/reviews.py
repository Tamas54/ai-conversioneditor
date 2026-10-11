"""Dashboard-API: verziótörténet és „vissza Claude-nak" kérések (Postgres, app/db.py)."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException

from app import db, events

router = APIRouter()


def _need_db() -> None:
    if not db.enabled():
        raise HTTPException(503, "No database configured (DATABASE_URL).")


@router.get("/api/history/{file_id}")
async def api_history(file_id: str) -> dict[str, Any]:
    _need_db()
    return await db.lineage(file_id)


@router.get("/api/reviews")
async def api_reviews(status: Optional[str] = "open", file_id: Optional[str] = None) -> dict[str, Any]:
    _need_db()
    return {"items": await db.review_list(status or None, file_id=file_id)}


@router.post("/api/reviews")
async def api_review_create(body: dict[str, Any]) -> dict[str, Any]:
    _need_db()
    if not body.get("file_id"):
        raise HTTPException(400, "file_id is required")
    row = await db.review_create(body["file_id"], (body.get("note") or "").strip() or None, "dashboard")
    events.emit("review_requested", file_id=row["file_id"], review_id=row["id"], note=row.get("note"))
    return row


@router.post("/api/reviews/{review_id}/resolve")
async def api_review_resolve(review_id: int, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    _need_db()
    body = body or {}
    row = await db.review_resolve(review_id, body.get("resolution_file_id"), body.get("note"))
    events.emit("review_resolved", review_id=review_id, file_id=row["file_id"])
    return row
