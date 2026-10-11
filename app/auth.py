"""Hozzáférés és identitás: ki a kérés gazdája (→ tenancy), egy middleware-ben.

    nyitva           /health, /login, /signup, /logout, /files/{id}?t=…[&u=…] (a link maga aláírt)
    MCP-kulcs        /mcp…   Authorization: Bearer <kulcs> VAGY ?key=<kulcs>
                             kulcs = a régi MCP_AUTH_TOKEN (→ admin) vagy egy felhasználó sk_… kulcsa
    belépés VAGY     minden más — süti a böngészőnek, kulcs a programoknak
    kulcs
    /dashboard       a régi (magyar) belső felület: csak admin; a felhasználók az /app-ot kapják

A middleware a kérés idejére beállítja a munkaterületet (tenancy.set_user), így minden fájlművelet a
felhasználó saját tárhelyén fut. Admin: a régi DASHBOARD_PASSWORD (e-mail nélkül vagy „admin”-nal).
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import time
from collections import defaultdict, deque
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import tenancy
from app.config import settings

log = logging.getLogger("aice.auth")
router = APIRouter()

COOKIE = "aice_session"
SESSION_TTL_S = 30 * 24 * 3600
_PUBLIC_PREFIXES = ("/health", "/login", "/signup", "/logout", "/files/", "/favicon.ico", "/static/")
_FAIL_LIMIT, _FAIL_WINDOW_S = 10, 15 * 60          # hibás belépés IP-nként
_SIGNUP_LIMIT, _SIGNUP_WINDOW_S = 5, 60 * 60       # regisztráció IP-nként
_fails: dict[str, deque[float]] = defaultdict(deque)
_signups: dict[str, deque[float]] = defaultdict(deque)
_key_cache: dict[str, tuple[str, float]] = {}      # kulcs-hash → (uid, lejárat) — ne kérdezzük a DB-t minden hívásnál


# ---------- munkamenet ----------

def _secret() -> bytes:
    # A jelszó is része a kulcsnak: az admin-jelszó cseréje mindenkit kiléptet.
    return hashlib.sha256(f"{settings.SESSION_SECRET}|{settings.DASHBOARD_PASSWORD}".encode()).digest()


def _sign(msg: str) -> str:
    return hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest()[:40]


def new_session(uid: str = tenancy.ADMIN_ID) -> str:
    exp = int(time.time()) + SESSION_TTL_S
    return f"{uid}.{exp}.{_sign(f'sess|{uid}|{exp}')}"


def session_uid(token: Optional[str]) -> Optional[str]:
    parts = (token or "").split(".")
    try:
        if len(parts) == 2:                       # régi (egyfelhasználós) süti → admin
            exp, sig, uid, msg = int(parts[0]), parts[1], tenancy.ADMIN_ID, f"sess|{parts[0]}"
        elif len(parts) == 3:
            uid, exp, sig = parts[0], int(parts[1]), parts[2]
            msg = f"sess|{uid}|{exp}"
        else:
            return None
    except ValueError:
        return None
    return uid if exp > time.time() and hmac.compare_digest(sig, _sign(msg)) else None


def _same(a: str, b: str) -> bool:
    # Bájtként: a compare_digest str-re TypeError-t dob nem-ASCII karakternél (ékezetes jelszó).
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _given_key(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else request.query_params.get("key", "")


async def key_uid(request: Request) -> Optional[str]:
    key = _given_key(request)
    if not key:
        return None
    if settings.MCP_AUTH_TOKEN and _same(key, settings.MCP_AUTH_TOKEN):
        return tenancy.ADMIN_ID
    if not key.startswith("sk_"):
        return None
    from app import db
    if not db.enabled():
        return None
    h = hashlib.sha256(key.encode()).hexdigest()
    hit = _key_cache.get(h)
    if hit and hit[1] > time.time():
        return hit[0]
    uid = await db.api_key_user(key)
    if uid:
        _key_cache[h] = (uid, time.time() + 60)
    return uid


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _limited(store: dict, ip: str, limit: int, window: int) -> bool:
    q, now = store[ip], time.time()
    while q and q[0] < now - window:
        q.popleft()
    return len(q) >= limit


# ---------- middleware ----------

async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith(_PUBLIC_PREFIXES):
        return await call_next(request)
    uid = await key_uid(request)
    if path.startswith("/mcp") and uid is None:
        if settings.MCP_AUTH_TOKEN:
            return JSONResponse({"error": "unauthorized: API key required (Bearer or ?key=)"}, status_code=401)
        uid = tenancy.ADMIN_ID  # helyi fejlesztés kulcs nélkül
    if uid is None:
        uid = session_uid(request.cookies.get(COOKIE))
    if uid is None and not settings.DASHBOARD_PASSWORD:
        uid = tenancy.ADMIN_ID  # helyi fejlesztés jelszó nélkül
    if uid is None:
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            nxt = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)
        return JSONResponse({"error": "unauthorized: sign in at /login"}, status_code=401)
    if path.startswith("/dashboard") and uid != tenancy.ADMIN_ID:
        return RedirectResponse("/app", status_code=303)
    token = tenancy.set_user(uid)
    try:
        return await call_next(request)
    finally:
        tenancy.reset(token)


def log_auth_state() -> None:
    if not settings.MCP_AUTH_TOKEN:
        log.warning("MCP_AUTH_TOKEN üres — az /mcp végpont kulcs nélkül is admin")
    if not settings.DASHBOARD_PASSWORD:
        log.warning("DASHBOARD_PASSWORD üres — belépés nélkül mindenki admin (csak helyi fejlesztéshez!)")
    if settings.FILE_SIGNING_KEY == "change-me-in-railway":
        log.warning("FILE_SIGNING_KEY az alapérték — a letöltési linkek hamisíthatók")


def _safe_next(nxt: str) -> str:
    # Csak saját útvonalra irányítunk vissza (nyílt átirányítás ellen).
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/app"


# ---------- oldalak ----------

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} — {product}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700;12..96,800&family=Instrument+Sans:wght@400;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/product.css"></head>
<body class="auth"><main class="auth-card">
  <a class="brand" href="/">{product}</a>
  <h1>{title}</h1>
  {error}
  <form method="post" action="{action}">{fields}
    <input type="hidden" name="next" value="{next}">
    <button class="btn primary wide" type="submit">{button}</button>
  </form>
  <p class="muted small">{alt}</p>
</main></body></html>"""


def _page(title, action, fields, button, alt, nxt, error="") -> HTMLResponse:
    err = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return HTMLResponse(_PAGE.format(title=title, product=html.escape(settings.PRODUCT_NAME), action=action,
                                     fields=fields, button=button, alt=alt, next=html.escape(_safe_next(nxt)), error=err))


_EMAIL = '<label>Email<input type="email" name="email" autocomplete="email" required autofocus></label>'
_PW = '<label>Password<input type="password" name="password" autocomplete="{ac}" required minlength="{min}"></label>'


@router.get("/login", include_in_schema=False)
async def login_page(next: str = "/app", error: str = "") -> HTMLResponse:
    alt = 'No account yet? <a href="/signup">Create one</a>' if settings.SIGNUP_OPEN else ""
    fields = _EMAIL.replace(" required", "") + _PW.format(ac="current-password", min=1)
    return _page("Sign in", "/login", fields, "Sign in", alt, next, error)


@router.post("/login", include_in_schema=False)
async def login(request: Request, password: str = Form(...), email: str = Form(""), next: str = Form("/app")):
    nxt, ip = _safe_next(next), _client_ip(request)
    if _limited(_fails, ip, _FAIL_LIMIT, _FAIL_WINDOW_S):
        return RedirectResponse(f"/login?next={quote(nxt)}&error={quote('Too many attempts — try again in 15 minutes.')}", 303)
    uid: Optional[str] = None
    if email.strip().lower() in ("", "admin") and settings.DASHBOARD_PASSWORD and _same(password, settings.DASHBOARD_PASSWORD):
        uid = tenancy.ADMIN_ID
    elif email.strip():
        from app import db
        if db.enabled():
            user = await db.user_login(email, password)
            uid = user["id"] if user else None
    if uid is None:
        _fails[ip].append(time.time())
        log.warning("sikertelen belépés: %s", ip)
        return RedirectResponse(f"/login?next={quote(nxt)}&error={quote('Wrong email or password.')}", 303)
    _fails[ip].clear()
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(COOKIE, new_session(uid), max_age=SESSION_TTL_S, httponly=True,
                    secure=request.url.scheme == "https", samesite="lax")
    return resp


@router.get("/signup", include_in_schema=False)
async def signup_page(next: str = "/app", error: str = "") -> HTMLResponse:
    if not settings.SIGNUP_OPEN:
        return _page("Sign up", "/signup", "", "Back to sign in", "Sign-up is closed for now.", "/login", "")
    fields = ('<label>Name<input type="text" name="name" autocomplete="name" maxlength="80"></label>'
              + _EMAIL + _PW.format(ac="new-password", min=10))
    return _page("Create your account", "/signup", fields, "Create account",
                 'Already have an account? <a href="/login">Sign in</a>', next, error)


@router.post("/signup", include_in_schema=False)
async def signup(request: Request, email: str = Form(...), password: str = Form(...), name: str = Form(""),
                 next: str = Form("/app")):
    from app import db
    nxt, ip = _safe_next(next), _client_ip(request)
    if not settings.SIGNUP_OPEN or not db.enabled():
        raise HTTPException(403, "sign-up is closed")
    if _limited(_signups, ip, _SIGNUP_LIMIT, _SIGNUP_WINDOW_S):
        return RedirectResponse(f"/signup?error={quote('Too many sign-ups from this network — try later.')}", 303)
    if len(password) < 10 or "@" not in email:
        return RedirectResponse(f"/signup?error={quote('Use a valid email and a password of at least 10 characters.')}", 303)
    try:
        user = await db.user_create(email, password, name.strip() or None, credits=settings.SIGNUP_CREDITS)
    except ValueError as e:
        return RedirectResponse(f"/signup?error={quote(str(e))}", 303)
    _signups[ip].append(time.time())
    if settings.SIGNUP_CREDITS:
        await db.credits_add(user["id"], 0, "signup")  # napló-sor a kezdő egyenlegről
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(COOKIE, new_session(user["id"]), max_age=SESSION_TTL_S, httponly=True,
                    secure=request.url.scheme == "https", samesite="lax")
    return resp


@router.get("/logout", include_in_schema=False)
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ---------- fiók-API ----------

def _require_db():
    from app import db
    if not db.enabled():
        raise HTTPException(503, "accounts need the database (DATABASE_URL)")
    return db


@router.get("/api/account")
async def api_account(request: Request) -> dict:
    uid = tenancy.current_user_id()
    from app import db
    user = await db.user_get(uid) if db.enabled() else None
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return {
        "id": uid, "email": (user or {}).get("email"), "name": (user or {}).get("name") or ("Admin" if uid == tenancy.ADMIN_ID else None),
        "is_admin": uid == tenancy.ADMIN_ID, "credits": None if uid == tenancy.ADMIN_ID else (user or {}).get("credits", 0),
        "image_credit_cost": settings.IMAGE_CREDIT_COST,
        "ocr_max_pages": 1000 if uid == tenancy.ADMIN_ID else settings.OCR_MAX_PAGES,
        "keys": await db.api_key_list(uid) if db.enabled() else [],
        "mcp_url": f"{base}/mcp", "product": settings.PRODUCT_NAME,
    }


@router.post("/api/account/keys")
async def api_key_create(body: Optional[dict] = None) -> dict:
    db = _require_db()
    created = await db.api_key_create(tenancy.current_user_id(), ((body or {}).get("name") or "MCP key")[:60])
    created["mcp_url"] = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/mcp?key={created['key']}"
    return created


@router.delete("/api/account/keys/{key_id}")
async def api_key_revoke(key_id: int) -> dict:
    db = _require_db()
    if not await db.api_key_revoke(tenancy.current_user_id(), key_id):
        raise HTTPException(404, "no such key")
    _key_cache.clear()
    return {"revoked": key_id}


@router.post("/api/admin/credits")
async def api_admin_credits(body: dict) -> dict:
    db = _require_db()
    if tenancy.current_user_id() != tenancy.ADMIN_ID:
        raise HTTPException(403, "admin only")
    rows = await db._rows("SELECT id FROM users WHERE email = lower(%s) OR id = %s", (body.get("email") or "", body.get("user_id") or ""))
    if not rows:
        raise HTTPException(404, "no such user")
    bal = await db.credits_add(rows[0]["id"], int(body.get("delta", 0)), body.get("reason") or "admin grant")
    return {"user_id": rows[0]["id"], "credits": bal}
