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
    # Attach the pool BEFORE warming it up so /health and other endpoints
    # can answer immediately. Warm-up runs in the background — first
    # tool request may briefly wait if the pool isn't ready yet, but the
    # health probe won't time out (Railway's 30-180s window).
    app.state.pool = pool
    warmup_task = asyncio.create_task(pool.start())
    cleanup_task = asyncio.create_task(cleanup_loop())
    log.info("Worker pool warming in background; cleanup loop running.")
    try:
        yield
    finally:
        log.info("Shutting down…")
        cleanup_task.cancel()
        warmup_task.cancel()
        await pool.stop()


app = FastAPI(
    title="ai-conversioneditor",
    version="0.1.0",
    description="Universal document conversion + AI-driven editing MCP",
    lifespan=lifespan,
    # Don't auto-redirect /mcp → /mcp/ — the redirect loses the request
    # body for POST and confuses MCP clients that expect a direct response
    # to their first POST. Mounted sub-apps handle their own trailing-slash
    # logic, so this only affects FastAPI's own routes.
    redirect_slashes=False,
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
        # Trust X-Forwarded-* from Railway's edge proxy so FastAPI knows
        # the original scheme is HTTPS. Without this, auto-trailing-slash
        # redirects produce `Location: http://…` and clients (like MCP
        # over Streamable HTTP) downgrade or reject the connection.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
