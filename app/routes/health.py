"""Health check route."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
async def health(request: Request):
    pool = getattr(request.app.state, "pool", None)
    return {
        "status": "ok",
        "lo_warm": len(pool._lo_workers) if pool else 0,
        "chromium_ready": bool(pool and pool._browser),
    }
