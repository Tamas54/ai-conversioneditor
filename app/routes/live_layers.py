"""Élő előnézet, L2: a grafikai rétegek klipjei (a logika: app/tools/live_layers.py). Mind aláírt (mint a /files/ és a
szakasz-előnézet: preview_serve.sign), süti nélkül is kérhető (a lejátszó, megosztott link):

    GET  /api/video/live/layer/{hash}.mp4?p=<pid>&t=<exp.sig>[&u=]   a klip (Range-kérésekkel); nincs kész → elsőbbséggel
                                                                      sorba, legfeljebb LIVE_LAYERS_WAIT_S-ig vár, 503 + Retry-After
    POST /api/video/{pid}/live/layers/prefetch?t=…   {"t": <lejátszófej, s>} → a fejtől mért távolság szerint sorba
                                                      → {"layers": layers_part(…), "queue": …}
    GET  /api/video/{pid}/live/layers.json?t=…       a bejegyzések állapota (a hiányzók sorba, a meglévő elsőbbség marad)
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response

from app import tenancy

router = APIRouter()
_NO_STORE = {"Cache-Control": "private, no-store"}
IMMUTABLE = "private, max-age=31536000, immutable"


def _busy() -> Response:
    return Response("The layer clip is still rendering — try again in a moment.", status_code=503, media_type="text/plain",
                    headers={"Retry-After": "2", **_NO_STORE})


@router.get("/api/video/live/layer/{layer_hash}.mp4")
async def live_layer_clip(layer_hash: str, p: str = Query(..., description="the video project"),
                          t: str = Query(..., description="signed token"),
                          u: str = Query(tenancy.ADMIN_ID, description="workspace (signed into t)")):
    """Egy réteg klipje (a hash a tartalom: örökre ugyanaz). A FileResponse a Range-kéréseket is kiszolgálja (206)."""
    from app.tools import live_layers
    if not live_layers._HASH.match(layer_hash):
        raise HTTPException(404, "no such layer clip")
    if not live_layers.verify(p, layer_hash, t, u):
        raise HTTPException(403, "invalid or expired token")
    tenancy.set_user(u)
    try:
        path = await live_layers.clip(p, layer_hash)
    except TimeoutError:
        return _busy()
    except (LookupError, FileNotFoundError) as e:
        raise HTTPException(404, str(e)[:200]) from None
    except RuntimeError as e:
        raise HTTPException(500, f"The layer clip could not be rendered: {str(e)[:200]}") from None
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": IMMUTABLE})


@router.get("/api/video/live/matte/{key}.mp4")
async def live_matte_clip(key: str, p: str = Query(..., description="the video project"), t: str = Query(..., description="signed token"),
                          u: str = Query(tenancy.ADMIN_ID, description="workspace (signed into t)")):
    """A background deck's speaker matte (app/tools/deck_matte.py: made once, the same clip the final video uses)."""
    import re as _re
    from app.tools import deck_matte, preview_cache
    if not _re.fullmatch(r"[0-9a-f]{64}", key):
        raise HTTPException(404, "no such matte")
    if not deck_matte.verify(p, key, t, u):
        raise HTTPException(403, "invalid or expired token")
    path = preview_cache.matte_path(key)
    if not path.exists():
        return Response("The cut-out is still being made — try again in a moment.", status_code=503, media_type="text/plain",
                        headers={"Retry-After": "5", **_NO_STORE})
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": IMMUTABLE})


def _check(pid: str, t: str, u: str) -> None:
    from app.tools import preview_serve as ps
    if not ps.verify(pid, None, t, u):
        raise HTTPException(403, "invalid or expired token")
    tenancy.set_user(u)


async def _part(pid: str, **kw: Any) -> list[dict[str, Any]]:
    from app.tools import live_layers
    try:
        return await live_layers.layers_part(pid, **kw)
    except (LookupError, FileNotFoundError) as e:
        raise HTTPException(404, str(e)[:200]) from None


@router.post("/api/video/{project_id}/live/layers/prefetch")
async def live_layers_prefetch(project_id: str, body: dict[str, Any] = Body(default={}), t: str = Query(...),
                               u: str = Query(tenancy.ADMIN_ID)):
    from app.tools import live_layers
    _check(project_id, t, u)
    try:
        playhead = float(body.get("t", body.get("playhead", 0)) or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "t: the playhead in seconds") from None
    layers = await _part(project_id, playhead=playhead)
    return JSONResponse({"layers": layers, "queue": live_layers.stats()}, headers=_NO_STORE)


@router.get("/api/video/{project_id}/live/layers.json")
async def live_layers_state(project_id: str, t: str = Query(...), u: str = Query(tenancy.ADMIN_ID)):
    from app.tools import live_layers
    _check(project_id, t, u)
    return JSONResponse({"layers": await _part(project_id), "queue": live_layers.stats()}, headers=_NO_STORE)
