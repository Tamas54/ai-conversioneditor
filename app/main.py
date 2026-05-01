"""
ai-conversioneditor MCP — main entrypoint.

Architecture:
    [ Claude / MCP client ]
            ↓ MCP-over-HTTP
    [ FastAPI app — this file ]
            ├── /mcp                 — MCP endpoint (tool calls)
            ├── /files/{file_id}     — output file serving (24h TTL token)
            ├── /upload              — file upload (returns file_id)
            └── /health              — liveness + worker pool status

    Background:
        - Worker pool (LibreOffice × 2 + Chromium × 1 pre-warmed by default)
        - Cleanup cron (every 60 min, deletes files older than TTL)
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.config import settings
from app.mcp_server import mount_mcp
from app.routes import files, upload, health, dashboard, preview
from app.storage.cleanup import cleanup_loop
from app.workers.pool import WorkerPool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("aice.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting ai-conversioneditor — data_dir=%s", settings.DATA_DIR)
    pool = WorkerPool(
        libreoffice_warm=settings.LO_WARM,
        chromium_warm=settings.CHROMIUM_WARM,
    )
    await pool.start()
    app.state.pool = pool

    cleanup_task = asyncio.create_task(cleanup_loop())
    log.info("Worker pool ready. Cleanup loop running.")
    try:
        yield
    finally:
        log.info("Shutting down…")
        cleanup_task.cancel()
        await pool.stop()


app = FastAPI(
    title="ai-conversioneditor",
    version="0.1.0",
    description="Universal document conversion + AI-driven editing MCP",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(upload.router)
app.include_router(files.router)
app.include_router(preview.router)
app.include_router(dashboard.router)
mount_mcp(app)


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        log_level="info",
    )
