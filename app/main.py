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
from pathlib import Path
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.config import settings
from app.mcp_server import mcp as _mcp_instance, mount_mcp
from app.auth import auth_middleware, log_auth_state, router as auth_router
from app.routes import files, upload, health, dashboard, preview, editor, reviews, make, product, pictures, studio
from app import billing, jobs
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
    log_auth_state()
    from app import db
    await db.start()
    from app import credits
    await credits.start()                 # induló árak, napi egyeztetés (kie ↔ könyv), félbemaradt AI-feladatok
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

    # CRITICAL: when mounting FastMCP's streamable_http_app inside another
    # FastAPI app (instead of running it as the top-level app), its session
    # manager's task group does NOT auto-init from the sub-app's lifespan.
    # We must enter `mcp.session_manager.run()` from the parent lifespan
    # ourselves. Without this every POST /mcp/ raises:
    #   RuntimeError: Task group is not initialized. Make sure to use run().
    async with _mcp_instance.session_manager.run():
        try:
            yield
        finally:
            log.info("Shutting down…")
            cleanup_task.cancel()
            warmup_task.cancel()
            await credits.stop()
            await pool.stop()
            await db.stop()


app = FastAPI(
    title="ai-conversioneditor",
    version="0.1.0",
    description="Universal document conversion + AI-driven editing MCP",
    lifespan=lifespan,
    # FastAPI auto-redirects /mcp -> /mcp/ with HTTP 307, which preserves
    # the POST body and method. Combined with uvicorn proxy_headers=True,
    # the redirect target is HTTPS (Railway proxy header). MCP clients
    # following the redirect end up at the right place.
)

app.middleware("http")(auth_middleware)


from fastapi.exception_handlers import http_exception_handler  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from starlette.exceptions import HTTPException as _StarletteHTTPException  # noqa: E402
from app.filecheck import DamagedFile, scrub  # noqa: E402


@app.exception_handler(_StarletteHTTPException)
async def _http_error(request, exc):            # a felhasználó soha nem lát szerveroldali útvonalat
    if isinstance(exc.detail, str):
        exc.detail = scrub(exc.detail)
    return await http_exception_handler(request, exc)


@app.exception_handler(DamagedFile)
async def _damaged_file(request, exc):          # bármelyik feltöltési út: a sérült fájl azonnal, érthetően
    return JSONResponse({"detail": str(exc)}, status_code=400)


from app.sharing import AccessDenied  # noqa: E402


@app.exception_handler(AccessDenied)
async def _not_shared(request, exc):            # megosztott munkaterület: csak megtekintés / nincs megosztva → 403
    return JSONResponse({"detail": str(exc), "not_shared": True}, status_code=getattr(exc, "status", 403))


from fastapi.staticfiles import StaticFiles  # noqa: E402
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(auth_router)
from app.oauth import router as oauth_router  # noqa: E402
app.include_router(oauth_router)
app.include_router(health.router)
app.include_router(upload.router)
app.include_router(files.router)
app.include_router(preview.router)
app.include_router(dashboard.router)
app.include_router(editor.router)
app.include_router(reviews.router)
app.include_router(make.router)
app.include_router(pictures.router)
from app.routes import graphics  # noqa: E402  (újraépítés · infografika · album)
app.include_router(graphics.router)
app.include_router(studio.router)
from app.routes import video as video_routes  # noqa: E402
from app.routes import checks as checks_routes  # noqa: E402  (tartalmi ellenőrzés · élő adatok · ágak)
app.include_router(video_routes.router)
app.include_router(checks_routes.router)
app.include_router(jobs.router)
app.include_router(billing.router)
from app.routes import credits as credits_routes  # noqa: E402  (kreditkönyv, árazás, admin-egyeztetés)
app.include_router(credits_routes.router)
from app.routes import ai_video as ai_video_routes  # noqa: E402  (kie.ai: webhook, AI-klip, minőségjavítás)
app.include_router(ai_video_routes.router)
app.include_router(product.router)
from app import seo  # noqa: E402
app.include_router(seo.router)
from app.routes import convert_web  # noqa: E402  (a weboldal konverter-widgetje, presetek, átváltók)
app.include_router(convert_web.router)
from app.routes import formkit_web  # noqa: E402  (aláírások, adatprofil — titkosítva)
app.include_router(formkit_web.router)
from app.routes import share as share_routes  # noqa: E402  (megosztás: meghívás, link, megosztva velem, ütközés)
app.include_router(share_routes.router)
from app.routes import live as live_routes; app.include_router(live_routes.router)  # noqa: E402,E702  (élő előnézet, L1)
from app.site.routes import build_router as _site_router  # noqa: E402  (nyilvános, többnyelvű oldalak)
app.include_router(_site_router())
from app.api_v1.app import v1 as _api_v1  # noqa: E402  (nyilvános REST API, saját OpenAPI-val)
app.mount("/v1", _api_v1)
mount_mcp(app)


class _McpNoSlashRedirect:
    """/mcp → /mcp/ átirányítás NÉLKÜL (a 307-et nem minden MCP-kliens követi POST-tal; a ChatGPT-be
    a „Connect your AI” oldal /mcp?key=… címe kerül). A legkülső réteg: a többi middleware már /mcp/-t lát."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self.inner(scope, receive, send)


app.add_middleware(_McpNoSlashRedirect)


class _PageGZip:
    """Gzip csak a lapokra és a statikus CSS/JS-re (Core Web Vitals: az /app ~225 KB → ~50 KB). Letöltés, MCP, API,
    SSE és szerkesztő-erőforrás NEM megy át rajta: ott a Range-kérés, a folyam és a CPU fontosabb."""

    _SKIP = ("/files/", "/mcp", "/api/", "/v1/", "/editor/", "/preview/", "/dashboard")

    def __init__(self, inner):
        from starlette.middleware.gzip import GZipMiddleware
        self.inner = inner
        self.gz = GZipMiddleware(inner, minimum_size=1024)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope.get("type") == "http" else ""
        if path and scope.get("method") == "GET" and not path.startswith(self._SKIP) \
                and (not path.startswith("/static/") or path.endswith((".css", ".js", ".svg", ".json", ".html"))):
            await self.gz(scope, receive, send)
        else:
            await self.inner(scope, receive, send)


app.add_middleware(_PageGZip)


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
