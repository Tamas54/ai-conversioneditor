"""Live preview, captions (lane L3): the ASS file and its fonts, for app/static/live-captions.js (JASSUB).

    GET /api/video/live/captions/{hash}.ass?p=<pid>&t=<token>[&u=]   the ASS (gzip when accepted), immutable
    GET /api/video/live/font/{key}?t=<token>                           an Inter font file (content hash in the key)

The links are signed like the segment preview's (preview_serve: HMAC with FILE_SIGNING_KEY); the URLs come from
app/tools/live_captions.captions_part (the scene's "captions"). A cookie-less player (a shared link, a guest) also needs
these two paths in app/auth.py's _SIGNED_PREVIEW (the integrator adds them with the other lanes' live paths).
"""
from __future__ import annotations

import mimetypes

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from app import tenancy

# the vendored JASSUB wasm (app/static/vendor/jassub/) must be served as application/wasm (streaming compile); the
# prod image knows it from /etc/mime.types, a slim image without that file would not
mimetypes.add_type("application/wasm", ".wasm")

router = APIRouter()


@router.get("/api/video/live/captions/{ass_hash}.ass")
async def live_captions_ass(request: Request, ass_hash: str, p: str = Query(..., description="the video project"),
                            t: str = Query(..., description="signed token"),
                            u: str = Query(tenancy.ADMIN_ID, description="workspace (signed into t)")):
    from app.tools import live_captions as lc
    from app.tools import preview_serve as ps
    if not ps.verify(p, lc.ass_token_h(ass_hash), t, u):
        raise HTTPException(403, "invalid or expired token")
    tenancy.set_user(u)
    got = lc.cached(ass_hash)
    if got is None:
        try:
            got = lc.rebuild(p, ass_hash)
        except FileNotFoundError:
            got = None
        if got is None:
            raise HTTPException(404, "These captions changed — fetch the scene again.")
    text, gz = got
    headers = {"Cache-Control": lc.IMMUTABLE, "Vary": "Accept-Encoding"}
    if gz and "gzip" in request.headers.get("accept-encoding", ""):
        return Response(gz, media_type=lc.ASS_MEDIA, headers={**headers, "Content-Encoding": "gzip"})
    return Response(text.encode("utf-8"), media_type=lc.ASS_MEDIA, headers=headers)


@router.get("/api/video/live/font/{key}")
async def live_captions_font(request: Request, key: str, t: str = Query(..., description="signed token")):
    from app.tools import live_captions as lc
    from app.tools import preview_serve as ps
    if not ps.verify(lc.FONT_PID, lc.font_token_h(key), t, tenancy.ADMIN_ID):
        raise HTTPException(403, "invalid or expired token")
    path = lc.font_file_of(key)
    if path is None:
        raise HTTPException(404, "no such font")
    if "gzip" in request.headers.get("accept-encoding", ""):
        return Response(lc.font_gzip(path), media_type=lc.FONT_MEDIA,
                        headers={"Cache-Control": lc.IMMUTABLE, "Vary": "Accept-Encoding", "Content-Encoding": "gzip"})
    return FileResponse(path, media_type=lc.FONT_MEDIA, headers={"Cache-Control": lc.IMMUTABLE, "Vary": "Accept-Encoding"})
