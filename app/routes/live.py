"""Live preview (plan20261026_live_preview.md, lane L1): the scene JSON and the test page.

  GET /api/video/{pid}/live.json?t=<signed>[&u=…]   the version's scene (app/tools/live_scene.py) — the same signed
                                                     token as preview.json (preview_serve.sign(pid) / signed(pid))
  GET /live/{pid}                                    the player page: WebGL canvas, the captions overlay, controls
                                                     (app/static/live-player.js; the captions: live-captions.js, L3)

The browser composes the picture itself (the working copies, the layer clips, the captions); nothing is rendered here.
"""
from __future__ import annotations

import json
import logging
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from app import tenancy
from app.storage import display_label, output_path

log = logging.getLogger("aice.live")
router = APIRouter()

_NO_STORE = {"Cache-Control": "private, no-store"}
_STATIC = Path(__file__).resolve().parent.parent / "static"


@router.get("/api/video/{project_id}/live.json")
async def live_scene(project_id: str, t: str = Query(..., description="signed token (as preview.json)"),
                     u: str = Query(tenancy.ADMIN_ID, description="workspace (signed into t)"),
                     prefer: str = Query("own", description="own | neutral: which copy of the same timeline")):
    from app.tools import live_scene as ls, preview_serve as ps
    if not ps.verify(project_id, None, t, u):
        raise HTTPException(403, "invalid or expired token")
    tenancy.set_user(u)
    try:
        scene = await ls.build(project_id, prefer="neutral" if prefer == "neutral" else "own")
    except FileNotFoundError:
        raise HTTPException(404, "no such video project") from None
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    return JSONResponse(scene, headers={**_NO_STORE, "ETag": f'"{scene["rev"]}"'})


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title><style>
:root {{ --bg: #F1F2EE; --ink: #15191B; --muted: #5C6870; --rule: #CDD2CB; --card: #FFFFFF; --mark: #E2431F; color-scheme: light dark; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #202526; --ink: #ECEFEC; --muted: #9AA6AC; --rule: #3A4346; --card: #2A3032; --mark: #FF5A36; }} }}
* {{ box-sizing: border-box; }}
[hidden] {{ display: none !important; }}
html, body {{ margin: 0; min-height: 100%; background: var(--bg); color: var(--ink); overflow-x: hidden; }}
body {{ display: grid; place-items: center; min-height: 100vh; padding: 16px;
  font: 13px/1.4 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; -webkit-font-smoothing: antialiased; }}
.pl {{ width: min(100%, max(640px, calc((100vh - 120px) * {ar}))); display: grid; gap: 8px; }}
.stage {{ position: relative; width: 100%; aspect-ratio: {w} / {h}; background: #000; box-shadow: 0 6px 24px rgba(0,0,0,.18); }}
.stage canvas {{ position: absolute; inset: 0; width: 100%; height: 100%; display: block; }}
.stage .caps {{ position: absolute; inset: 0; pointer-events: none; }}
.row {{ display: flex; align-items: center; gap: 10px; }}
.btn {{ font: inherit; font-weight: 600; font-size: 12px; line-height: 1; padding: 7px 12px; border: 1px solid var(--rule); border-radius: 6px;
  background: var(--card); color: var(--ink); cursor: pointer; min-width: 64px; }}
.btn:focus-visible, .seek:focus-visible {{ outline: 2px solid var(--mark); outline-offset: 2px; }}
.seek {{ flex: 1 1 auto; accent-color: var(--mark); }}
.time {{ font: 500 12px/1 ui-monospace, SFMono-Regular, Menlo, monospace; font-variant-numeric: tabular-nums; white-space: nowrap; }}
.st {{ margin: 0; color: var(--muted); min-height: 1.4em; }}
</style></head><body>
<div class="pl">
<div class="stage" id="lp-stage" aria-label="{name}"></div>
<div class="row"><button type="button" class="btn" id="lp-play">Play</button>
<input type="range" class="seek" id="lp-seek" min="0" max="{total}" step="0.01" value="0" aria-label="Position">
<span class="time" id="lp-time">0:00</span></div>
<p class="st" id="lp-msg" role="status"></p>
</div>
<script type="application/json" id="lp-cfg">{cfg}</script>
{captions}<script src="/static/live-player.js?v={ver}"></script>
<script>LivePlayer.page(document.getElementById("lp-cfg"));</script>
</body></html>"""


@router.get("/live/{project_id}")
async def live_page(project_id: str):
    from app.storage import read_meta
    from app.tools import preview_serve as ps
    if not project_id.lower().endswith(".ecproj") or not output_path(project_id).exists():
        raise HTTPException(404, "no such video project")
    meta = read_meta(project_id) or {}
    v = meta.get("video") or {}
    try:
        w, h = max(1, int(v.get("width") or 16)), max(1, int(v.get("height") or 9))
        total = float(v.get("duration") or 0)
    except (TypeError, ValueError):
        w, h, total = 16, 9, 0.0
    q = ps.signed(project_id)
    cfg = {"pid": project_id, "scene": f"/api/video/{quote(project_id)}/live.json{q}"}
    label = display_label(project_id) or project_id
    stem = label.rsplit(".", 1)[0] if "." in label else label

    def ver(name: str) -> int:
        try:
            return int((_STATIC / name).stat().st_mtime)
        except OSError:
            return 0
    caps = (f'<script src="/static/live-captions.js?v={ver("live-captions.js")}"></script>\n'
            if (_STATIC / "live-captions.js").exists() else "")
    return HTMLResponse(_PAGE.format(name=escape(f"{stem} — live preview"), w=w, h=h, ar=f"{w / h:.5f}", total=f"{total:.3f}",
                                     ver=ver("live-player.js"), captions=caps,
                                     cfg=json.dumps(cfg, ensure_ascii=False).replace("<", "\\u003c")),
                        headers={"Cache-Control": "no-cache"})
