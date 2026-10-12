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
_PUBLIC_PREFIXES = ("/health", "/login", "/signup", "/logout", "/files/", "/favicon.ico", "/static/", "/api/stripe/webhook",
                    "/robots.txt", "/llms.txt", "/sitemap.xml",
                    "/.well-known/", "/oauth/")      # OAuth az MCP-hez (app/oauth.py) — a belépést maga kéri

# Vendég (belépés nélkül): kipróbálhat mindent, ami nem AI (nincs LLM/FLUX-költség). (metódus, út-előtag)
_GUEST_OK = (
    ("GET", "/app"), ("GET", "/editor/"), ("GET", "/preview/"), ("GET", "/api/thumb/"), ("GET", "/api/print/"), ("GET", "/api/files"),
    ("GET", "/api/account"), ("GET", "/api/convert/targets"), ("GET", "/api/forms/fields"), ("GET", "/api/jobs/"),
    ("GET", "/api/revisions"), ("GET", "/api/billing/plans"), ("GET", "/api/images/search"), ("GET", "/api/pictures"),
    ("POST", "/api/make/picture"),                                    # a képválasztó: AI-képnél a végpont maga kér fiókot
    ("POST", "/api/upload"), ("POST", "/api/editor/save"), ("POST", "/api/make/convert"), ("POST", "/api/make/pdf"),
    ("POST", "/api/make/photo"), ("POST", "/api/make/design"), ("POST", "/api/make/social"), ("POST", "/api/make/book"),
    ("POST", "/api/make/fill_form"), ("POST", "/api/make/document"),
    ("POST", "/api/make/deck"), ("POST", "/api/make/handout"),          # deck: vendégnek csak az üres pakli (AI nélkül)
    ("POST", "/api/make/blank"),                                        # „New”: üres fájl, AI nélkül
    ("POST", "/api/make/album"),                                        # fotóalbum: AI nélkül (az AI-képaláírás maga kér fiókot)
    ("GET", "/api/design/columns"),                                     # tömeges személyre szabás: a táblázat oszlopai
    ("GET", "/api/video/assets"), ("POST", "/api/video/asset"),         # a videószerkesztő médiafájljai (a projekt mellé)
    ("DELETE", "/api/files/"), ("DELETE", "/api/jobs/"),                # (eddig egy komment végére csúszva — vendég nem tudott törölni)
)
GUEST_MAX_UPLOAD_MB = 25
_GUEST_POSTS, _GUEST_WINDOW_S = 60, 10 * 60          # vendég POST-ok IP-nként (feltöltés + gyártás)
_guest_posts: dict[str, deque] = defaultdict(deque)
ACCOUNT_NEEDED = "This uses AI — create a free account to use it."


def _guest_ok(request: Request) -> bool:
    path, method = request.url.path, request.method
    if path == "/" and method == "GET":
        return True
    return any(method == m and (path == p or path.startswith(p if p.endswith("/") else p + "/") or path == p.rstrip("/"))
               for m, p in _GUEST_OK) or (method == "GET" and path.startswith("/api/files/"))
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


GUEST_TTL_S = 24 * 3600


def new_session(uid: str = tenancy.ADMIN_ID, ttl: int = 0) -> str:
    exp = int(time.time()) + (ttl or SESSION_TTL_S)
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
    if key.startswith("at_"):                   # OAuth access token (ChatGPT, Claude… „Connect”) — app/oauth.py
        from app import oauth
        return await oauth.access_uid(key)
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
            # A WWW-Authenticate-ből találja meg a kliens (ChatGPT, Claude) az OAuth-ot: belépés + „Allow”, kulcs nélkül
            from app import oauth
            return JSONResponse({"error": "unauthorized: sign in (OAuth) or send an API key (Bearer or ?key=)"},
                                status_code=401, headers={"WWW-Authenticate": oauth.www_authenticate(bool(_given_key(request)))})
        uid = tenancy.ADMIN_ID  # helyi fejlesztés kulcs nélkül
    if uid is None:
        uid = session_uid(request.cookies.get(COOKIE))
    if uid is None and not settings.DASHBOARD_PASSWORD:
        uid = tenancy.ADMIN_ID  # helyi fejlesztés jelszó nélkül
    new_guest = None
    if uid is None and _guest_ok(request):
        uid = new_guest = tenancy.new_guest_id()        # első látogatás: vendég-munkaterület, aláírt sütiben
    if uid is not None and tenancy.is_guest(uid):
        if not _guest_ok(request):
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                return RedirectResponse(f"/signup?next={quote(path)}", status_code=303)
            return JSONResponse({"detail": ACCOUNT_NEEDED, "account_required": True}, status_code=401)
        if request.method in ("POST", "DELETE"):
            ip = _client_ip(request)
            if _limited(_guest_posts, ip, _GUEST_POSTS, _GUEST_WINDOW_S):
                return JSONResponse({"detail": "Slow down a little — or create a free account for higher limits."}, status_code=429)
            _guest_posts[ip].append(time.time())
            if path == "/api/upload" and int(request.headers.get("content-length") or 0) > GUEST_MAX_UPLOAD_MB * 1024 * 1024:
                return JSONResponse({"detail": f"Files up to {GUEST_MAX_UPLOAD_MB} MB without an account."}, status_code=413)
    if uid is None:
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            nxt = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)
        return JSONResponse({"error": "unauthorized: sign in at /login"}, status_code=401)
    if path.startswith("/dashboard") and uid != tenancy.ADMIN_ID:
        return RedirectResponse("/app", status_code=303)
    token = tenancy.set_user(uid)
    try:
        resp = await call_next(request)
    finally:
        tenancy.reset(token)
    if new_guest:
        resp.set_cookie(COOKIE, new_session(new_guest, ttl=GUEST_TTL_S), max_age=GUEST_TTL_S, httponly=True,
                        secure=request.url.scheme == "https", samesite="lax")
    return resp


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
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml"><link rel="icon" href="/static/favicon-32.png" type="image/png" sizes="32x32">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png"><meta name="theme-color" content="#1E4436">
<meta property="og:title" content="{product} — finished files, changed by asking"><meta property="og:image" content="/static/og.png">
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
    from app.routes.product import static_v
    return HTMLResponse(_PAGE.replace('href="/static/product.css"', f'href="{static_v("product.css")}"').format(title=title, product=html.escape(settings.PRODUCT_NAME), action=action,
                                     fields=fields, button=button, alt=alt, next=html.escape(_safe_next(nxt)), error=err))


_EMAIL = '<label>Email<input type="email" name="email" autocomplete="email" required autofocus></label>'
_PW = '<label>Password<input type="password" name="password" autocomplete="{ac}" required minlength="{min}"></label>'


@router.get("/login", include_in_schema=False)
async def login_page(next: str = "/app", error: str = "") -> HTMLResponse:
    signup = "/signup" + (f"?next={quote(_safe_next(next), safe='')}" if _safe_next(next) != "/app" else "")   # pl. az OAuth-engedélyezés folytatódjon
    alt = (f'No account yet? <a href="{html.escape(signup)}">Create one</a> · ' if settings.SIGNUP_OPEN else "") + \
          '<a href="/app">Look around without an account</a>'
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
    await _adopt_guest(request, uid)
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
                 'Already have an account? <a href="/login">Sign in</a> · <a href="/app">Not now</a>', next, error)


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
    await _adopt_guest(request, user["id"])
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(COOKIE, new_session(user["id"]), max_age=SESSION_TTL_S, httponly=True,
                    secure=request.url.scheme == "https", samesite="lax")
    return resp


async def _adopt_guest(request: Request, uid: str) -> None:
    """Ha vendégként dolgozott, a fájljai a (most létrejött / belépett) fiókba kerülnek."""
    guest = session_uid(request.cookies.get(COOKIE))
    if guest and tenancy.is_guest(guest) and uid != guest:
        from app import db
        moved = tenancy.migrate(guest, uid)
        try:
            await db.reassign_owner(guest, uid)
        except Exception:
            log.warning("vendég-verziók átírása nem sikerült", exc_info=True)
        log.info("vendég %s → %s: %d fájl", guest, uid, moved)


@router.get("/logout", include_in_schema=False)
async def logout(next: str = "/app"):
    resp = RedirectResponse(_safe_next(next), status_code=303)     # „Use another account” az engedélyező oldalról
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
    guest = tenancy.is_guest(uid)
    from app import billing
    if not guest:
        await billing.monthly_topup(uid)
    user = await db.user_get(uid) if db.enabled() and not guest else None
    plan = "pro" if uid == tenancy.ADMIN_ID else (user or {}).get("plan") or "free"
    month = billing.month_key()
    usage = {k: {"used": await db.usage_get(uid, month, k), "limit": v}
             for k, v in billing.PLANS[plan]["quota"].items()} if user else {}
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return {
        "guest": guest, "guest_max_upload_mb": GUEST_MAX_UPLOAD_MB if guest else None,
        "plan": plan, "plan_name": billing.PLANS[plan]["name"], "usage": usage, "billing_enabled": billing.enabled(),
        "has_subscription": bool((user or {}).get("stripe_customer_id")),
        "id": uid, "email": (user or {}).get("email"), "name": (user or {}).get("name") or ("Admin" if uid == tenancy.ADMIN_ID else None),
        "is_admin": uid == tenancy.ADMIN_ID, "credits": None if uid == tenancy.ADMIN_ID else (user or {}).get("credits", 0),
        "image_credit_cost": settings.IMAGE_CREDIT_COST,
        "ocr_max_pages": 1000 if uid == tenancy.ADMIN_ID else settings.OCR_MAX_PAGES,
        "keys": await db.api_key_list(uid) if db.enabled() and not guest else [],
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
    from app import oauth
    oauth.forget(key_id)              # a kapcsolódott AI (OAuth) is azonnal kiesik
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
