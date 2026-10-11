"""Hozzáférés: MCP-kulcs és dashboard-belépés egy middleware-ben.

    nyitva           /health, /login, /logout, /files/{id}?t=… (a link maga aláírt)
    MCP-kulcs        /mcp…           Authorization: Bearer <MCP_AUTH_TOKEN>  VAGY  ?key=<MCP_AUTH_TOKEN>
                                     (a claude.ai-connector fejlécet nem tud küldeni, URL-t igen)
    belépés VAGY     minden más      (dashboard, /api/*, /editor/*, /preview/*, /upload)
    MCP-kulcs                        — süti a böngészőnek, kulcs a programoknak (Bridge, scriptek)

Üres MCP_AUTH_TOKEN / DASHBOARD_PASSWORD = az adott védelem kikapcsolva (helyi fejlesztés); induláskor szól.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import time
from collections import defaultdict, deque
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import settings

log = logging.getLogger("aice.auth")
router = APIRouter()

COOKIE = "aice_session"
SESSION_TTL_S = 30 * 24 * 3600
_PUBLIC_PREFIXES = ("/health", "/login", "/logout", "/files/", "/favicon.ico")
# Jelszópróbálgatás ellen: IP-nként legfeljebb ennyi hibás próba ennyi idő alatt.
_FAIL_LIMIT, _FAIL_WINDOW_S = 10, 15 * 60
_fails: dict[str, deque[float]] = defaultdict(deque)


def _secret() -> bytes:
    # A jelszó is része a kulcsnak: jelszócsere = minden munkamenet érvénytelen.
    return hashlib.sha256(f"{settings.SESSION_SECRET}|{settings.DASHBOARD_PASSWORD}".encode()).digest()


def _sign(msg: str) -> str:
    return hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest()[:40]


def new_session() -> str:
    exp = int(time.time()) + SESSION_TTL_S
    return f"{exp}.{_sign(f'sess|{exp}')}"


def valid_session(token: str | None) -> bool:
    try:
        exp_s, sig = (token or "").split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    return exp > time.time() and hmac.compare_digest(sig, _sign(f"sess|{exp}"))


def _has_key(request: Request) -> bool:
    key = settings.MCP_AUTH_TOKEN
    if not key:
        return False
    auth = request.headers.get("authorization", "")
    given = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("key", "")
    return bool(given) and hmac.compare_digest(given, key)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith(_PUBLIC_PREFIXES):
        return await call_next(request)
    if path.startswith("/mcp"):
        if settings.MCP_AUTH_TOKEN and not _has_key(request):
            return JSONResponse({"error": "unauthorized: MCP key required (Bearer or ?key=)"}, status_code=401)
        return await call_next(request)
    if not settings.DASHBOARD_PASSWORD or _has_key(request) or valid_session(request.cookies.get(COOKIE)):
        return await call_next(request)
    wants_html = request.method == "GET" and "text/html" in request.headers.get("accept", "")
    if wants_html:
        nxt = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)
    return JSONResponse({"error": "unauthorized: log in at /login"}, status_code=401)


def log_auth_state() -> None:
    if not settings.MCP_AUTH_TOKEN:
        log.warning("MCP_AUTH_TOKEN üres — az /mcp végpont NYITOTT")
    if not settings.DASHBOARD_PASSWORD:
        log.warning("DASHBOARD_PASSWORD üres — a dashboard és az /api NYITOTT")
    if settings.FILE_SIGNING_KEY == "change-me-in-railway":
        log.warning("FILE_SIGNING_KEY az alapérték — a letöltési linkek hamisíthatók")


def _safe_next(nxt: str) -> str:
    # Csak saját útvonalra irányítunk vissza (nyílt átirányítás ellen).
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/dashboard"


_LOGIN_HTML = """<!doctype html><html lang="hu"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Belépés — ai-conversioneditor</title>
<style>
  body {{ margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: #f7ecd2; font: 15px ui-monospace, Menlo, monospace; color: #2e1f06; }}
  form {{ background: #fffaf0; border: 3px double #b8860b; border-radius: 6px; padding: 28px 32px;
    box-shadow: 0 8px 24px rgba(60,40,0,.18); width: min(340px, calc(100vw - 32px)); box-sizing: border-box; }}
  h1 {{ font-size: 17px; margin: 0 0 18px; color: #8b6508; }}
  input {{ width: 100%; box-sizing: border-box; padding: 9px 10px; border: 1px solid #b8860b; border-radius: 4px;
    font: inherit; background: #fff; }}
  button {{ margin-top: 14px; width: 100%; padding: 9px; border: 0; border-radius: 4px; background: #b8860b;
    color: #fffaf0; font: inherit; font-weight: 700; cursor: pointer; }}
  .err {{ color: #a0281e; margin: 0 0 12px; font-size: 13px; }}
</style></head><body>
<form method="post" action="/login">
  <h1>❦ ai-conversioneditor</h1>
  {error}
  <input type="password" name="password" placeholder="Jelszó" autofocus required autocomplete="current-password">
  <input type="hidden" name="next" value="{next}">
  <button type="submit">Belépés</button>
</form></body></html>"""


@router.get("/login", include_in_schema=False)
async def login_page(next: str = "/dashboard", error: str = "") -> HTMLResponse:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return HTMLResponse(_LOGIN_HTML.format(error=err, next=html.escape(_safe_next(next))))


@router.post("/login", include_in_schema=False)
async def login(request: Request, password: str = Form(...), next: str = Form("/dashboard")):
    nxt = _safe_next(next)
    ip = _client_ip(request)
    q = _fails[ip]
    now = time.time()
    while q and q[0] < now - _FAIL_WINDOW_S:
        q.popleft()
    if len(q) >= _FAIL_LIMIT:
        return RedirectResponse(f"/login?next={quote(nxt)}&error={quote('Túl sok próbálkozás, várj 15 percet.')}", status_code=303)
    if not settings.DASHBOARD_PASSWORD or not hmac.compare_digest(password, settings.DASHBOARD_PASSWORD):
        q.append(now)
        log.warning("sikertelen belépés: %s", ip)
        return RedirectResponse(f"/login?next={quote(nxt)}&error={quote('Hibás jelszó.')}", status_code=303)
    q.clear()
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(COOKIE, new_session(), max_age=SESSION_TTL_S, httponly=True,
                    secure=request.url.scheme == "https", samesite="lax")
    return resp


@router.get("/logout", include_in_schema=False)
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp
