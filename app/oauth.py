"""OAuth 2.1 az MCP-hez (MCP auth spec 2025-06-18) — „élő ember is végigcsinálja” (Kommandant, 10-12).

A felhasználó EGY címet ír a ChatGPT / Claude connector-mezőjébe (https://<host>/mcp); a kliens a 401-ből
megtalálja a metaadatot, regisztrál, a böngészőben felugrik a belépés + „Allow”, és kész — kulcsmásolás nélkül.

    GET  /.well-known/oauth-protected-resource[/mcp]    RFC 9728 — melyik engedélyező szerver
    GET  /.well-known/oauth-authorization-server[/mcp]  RFC 8414 (+ /.well-known/openid-configuration)
    POST /oauth/register                                RFC 7591 dinamikus kliensregisztráció
    GET  /oauth/authorize  → belépés (a meglévő /login) → engedélyező oldal → POST /oauth/authorize
    POST /oauth/token      authorization_code (PKCE S256) | refresh_token

ÁLLAPOTMENTES: a client_id, a code, az access és a refresh token AZ ÜGYFÉLNÉL lévő, HMAC-cal aláírt token
(a SESSION_SECRET-ből), új tábla / DDL nincs. A visszavonható „engedély” egy sor a meglévő api_keys táblában
(„<kliens> (connected)” — a Connect your AI listában látszik, Revoke = a kliens kitiltva); az access token erre
a sorra hivatkozik, a kulcs maga sosem kerül ki.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from typing import Any, Optional
from urllib.parse import quote, urlencode, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app import tenancy
from app.config import settings

log = logging.getLogger("aice.oauth")
router = APIRouter()

SCOPE = "mcp"
CODE_TTL_S = 300                    # a „code” 5 percig érvényes, egyszer használható
ACCESS_TTL_S = 30 * 24 * 3600       # az access token 30 nap; utána a refresh tokennel újat kap
CSRF_TTL_S = 15 * 60
_CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Authorization, Content-Type, MCP-Protocol-Version",
         "Access-Control-Allow-Methods": "GET, POST, OPTIONS"}
_used_codes: dict[str, float] = {}  # felhasznált code-ok (jti → lejárat) — egy példány fut


# ---------- aláírt tokenek ----------

def _key() -> bytes:
    # Külön levezetett kulcs (a munkamenet-süti a DASHBOARD_PASSWORD-öt is keveri — annak cseréje ne bontsa a kapcsolatokat).
    return hashlib.sha256(f"aice-oauth|{settings.SESSION_SECRET}".encode()).digest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _mac(kind: str, body: str) -> str:
    return _b64(hmac.new(_key(), f"{kind}.{body}".encode(), hashlib.sha256).digest()[:24])


def seal(kind: str, payload: dict[str, Any]) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    return f"{kind}_{body}.{_mac(kind, body)}"


def unseal(kind: str, token: Optional[str]) -> Optional[dict[str, Any]]:
    if not token or not token.startswith(kind + "_"):
        return None
    body, _, sig = token[len(kind) + 1:].rpartition(".")
    if not body or not hmac.compare_digest(sig.encode(), _mac(kind, body).encode()):
        return None
    try:
        p = json.loads(_unb64(body))
    except Exception:
        return None
    if not isinstance(p, dict) or (p.get("exp") and p["exp"] < time.time()):
        return None
    return p


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:24]


def _client_secret(client_id: str) -> str:
    return "cs_" + _b64(hmac.new(_key(), f"secret|{client_id}".encode(), hashlib.sha256).digest())


def _base() -> str:
    return settings.PUBLIC_BASE_URL.rstrip("/")


# ---------- az MCP-hívás hitelesítése (app/auth.py key_uid hívja) ----------

_active: dict[int, float] = {}       # api_keys.id → meddig hihetünk neki (ne kérdezzük a DB-t minden hívásnál)


def forget(key_id: Optional[int] = None) -> None:
    """Visszavonáskor: a gyorsítótár ürítése (app/auth.py)."""
    _active.clear() if key_id is None else _active.pop(key_id, None)


async def access_uid(token: str) -> Optional[str]:
    """Egy OAuth access token gazdája, ha érvényes és az engedély (api_keys-sor) nincs visszavonva."""
    p = unseal("at", token)
    if not p:
        return None
    kid, uid = p.get("k"), p.get("u")
    if not isinstance(kid, int) or not uid:
        return None
    if _active.get(kid, 0) > time.time():
        return uid
    from app import db
    if not db.enabled() or not await db.api_key_active(kid, uid):
        return None
    _active[kid] = time.time() + 60
    return uid


def www_authenticate(error: bool = False) -> str:
    return (f'Bearer realm="{settings.PRODUCT_NAME}", resource_metadata="{_base()}/.well-known/oauth-protected-resource"'
            + (', error="invalid_token"' if error else ""))


# ---------- metaadat ----------

def _prm() -> dict[str, Any]:
    return {"resource": f"{_base()}/mcp", "authorization_servers": [_base()], "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"], "resource_name": settings.PRODUCT_NAME,
            "resource_documentation": f"{_base()}/app/connect"}


def _asm() -> dict[str, Any]:
    b = _base()
    return {"issuer": b, "authorization_endpoint": f"{b}/oauth/authorize", "token_endpoint": f"{b}/oauth/token",
            "registration_endpoint": f"{b}/oauth/register", "scopes_supported": [SCOPE],
            "response_types_supported": ["code"], "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "service_documentation": f"{b}/app/connect"}


@router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@router.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
async def protected_resource() -> JSONResponse:
    return JSONResponse(_prm(), headers=_CORS)


@router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
@router.get("/.well-known/oauth-authorization-server/mcp", include_in_schema=False)
@router.get("/.well-known/openid-configuration", include_in_schema=False)
@router.get("/.well-known/openid-configuration/mcp", include_in_schema=False)
async def authorization_server() -> JSONResponse:
    return JSONResponse(_asm(), headers=_CORS)


@router.options("/oauth/register", include_in_schema=False)
@router.options("/oauth/token", include_in_schema=False)
async def _preflight() -> Response:
    return Response(status_code=204, headers=_CORS)


def _err(status: int, error: str, desc: str) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": desc}, status_code=status,
                        headers={**_CORS, "Cache-Control": "no-store"})


# ---------- kliensregisztráció (RFC 7591) ----------

def _redirect_ok(uri: str) -> bool:
    """https bárhová; http csak a saját gépre (natív appok, RFC 8252); egyedi séma (cursor://, vscode://…) is,
    de veszélyes séma nem."""
    try:
        u = urlparse(uri)
    except ValueError:
        return False
    if u.fragment or not u.scheme:
        return False
    if u.scheme == "https":
        return bool(u.netloc)
    if u.scheme == "http":
        return (u.hostname or "") in ("localhost", "127.0.0.1", "::1")
    return u.scheme.lower() not in ("javascript", "data", "file", "vbscript", "about", "blob")


@router.post("/oauth/register", include_in_schema=False)
async def register(request: Request) -> JSONResponse:
    try:
        meta = await request.json()
    except Exception:
        return _err(400, "invalid_client_metadata", "Send the client metadata as JSON.")
    uris = meta.get("redirect_uris") if isinstance(meta, dict) else None
    if not isinstance(uris, list) or not uris or len(uris) > 10 or not all(isinstance(u, str) and _redirect_ok(u) for u in uris):
        return _err(400, "invalid_redirect_uri", "redirect_uris: one or more https (or loopback http / app scheme) addresses.")
    name = " ".join(str(meta.get("client_name") or "An AI app").split())[:60] or "An AI app"
    method = meta.get("token_endpoint_auth_method") or "none"
    if method not in ("none", "client_secret_post", "client_secret_basic"):
        method = "none"
    iat = int(time.time())
    cid = seal("cl", {"n": name, "r": uris, "m": method, "iat": iat})
    out: dict[str, Any] = {"client_id": cid, "client_id_issued_at": iat, "client_name": name, "redirect_uris": uris,
                           "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                           "token_endpoint_auth_method": method, "scope": SCOPE}
    if method != "none":
        out["client_secret"], out["client_secret_expires_at"] = _client_secret(cid), 0
    log.info("OAuth-kliens regisztrálva: %s (%s)", name, ", ".join(urlparse(u).netloc or u for u in uris))
    return JSONResponse(out, status_code=201, headers={**_CORS, "Cache-Control": "no-store"})


# ---------- engedélyezés ----------

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} — {product}</title>
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml"><meta name="theme-color" content="#1E4436">
<meta name="robots" content="noindex">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700;12..96,800&family=Instrument+Sans:wght@400;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="{css}"></head>
<body class="auth"><main class="auth-card consent">
  <a class="brand" href="/app">{product}</a>
  {body}
</main></body></html>"""


def _html(title: str, body: str, status: int = 200) -> HTMLResponse:
    from app.routes.product import static_v
    page = (_PAGE.replace("{title}", html.escape(title)).replace("{product}", html.escape(settings.PRODUCT_NAME))
            .replace("{css}", static_v("product.css")).replace("{body}", body))
    return HTMLResponse(page, status_code=status, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                                           "Referrer-Policy": "no-referrer"})


def _error_page(msg: str) -> HTMLResponse:
    return _html("Can't connect", f'<h1>Can\'t connect</h1><p class="muted">{html.escape(msg)}</p>'
                 f'<p class="muted small">Go back to your AI app and add the connection again. '
                 f'<a href="/app/connect">How to connect</a></p>', 400)


def _back(ruri: str, state: Optional[str], **params: str) -> RedirectResponse:
    q = {**params, **({"state": state} if state else {})}
    return RedirectResponse(ruri + ("&" if "?" in ruri else "?") + urlencode(q), status_code=302)


def _check(params) -> tuple[Optional[dict], str, Optional[HTMLResponse]]:
    """client_id + redirect_uri ellenőrzése — hibánál NEM irányítunk vissza (nyílt átirányítás ellen)."""
    client = unseal("cl", params.get("client_id"))
    if not client:
        return None, "", _error_page("This AI app isn't registered here (or its registration is too old).")
    ruri = params.get("redirect_uri") or (client["r"][0] if len(client.get("r") or []) == 1 else "")
    if ruri not in (client.get("r") or []):
        return None, "", _error_page("The app asked to return to an address it didn't register, so we stopped here for your safety.")
    return client, ruri, None


async def _who(uid: str) -> str:
    if uid == tenancy.ADMIN_ID:
        return "admin"
    try:
        from app import db
        u = await db.user_get(uid) if db.enabled() else None
        return (u or {}).get("email") or (u or {}).get("name") or "your account"
    except Exception:
        return "your account"


@router.get("/oauth/authorize", include_in_schema=False)
async def authorize(request: Request):
    from app.auth import COOKIE, session_uid
    q = request.query_params
    client, ruri, bad = _check(q)
    if bad:
        return bad
    state = q.get("state")
    if q.get("response_type") != "code":
        return _back(ruri, state, error="unsupported_response_type")
    chal, method = q.get("code_challenge") or "", q.get("code_challenge_method") or "plain"
    if not chal or method != "S256":
        return _back(ruri, state, error="invalid_request", error_description="PKCE with S256 is required.")
    uid = session_uid(request.cookies.get(COOKIE))
    if not settings.DASHBOARD_PASSWORD and uid is None:
        uid = tenancy.ADMIN_ID                                  # helyi fejlesztés jelszó nélkül (mint a middleware)
    if uid is None or tenancy.is_guest(uid):                    # belépés (vagy regisztráció), aztán ide vissza
        here = request.url.path + "?" + request.url.query
        return RedirectResponse(f"/login?next={quote(here, safe='')}", status_code=303)
    name = html.escape(client["n"])
    csrf = seal("csrf", {"u": uid, "c": _h(q.get("client_id", "")), "exp": int(time.time()) + CSRF_TTL_S})
    keep = {k: q.get(k) for k in ("client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method",
                                  "scope", "resource") if q.get(k)}
    keep["redirect_uri"] = ruri
    hidden = "".join(f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">' for k, v in keep.items())
    host = html.escape(urlparse(ruri).netloc or urlparse(ruri).scheme)
    switch = quote(request.url.path + "?" + request.url.query, safe="")
    body = f"""<h1>Connect {name}</h1>
  <p><strong>{name}</strong> wants to use your {html.escape(settings.PRODUCT_NAME)} files — make and edit documents,
  slides, PDFs and photos in your account.</p>
  <ul class="consent-list">
    <li>It works in your account: new files show up in <em>Files</em>, each with a link to open it here.</li>
    <li>It never sees your password. Disconnect it any time in <a href="/app/connect">Connect your AI</a>.</li>
  </ul>
  <p class="muted small">Signed in as <strong>{html.escape(await _who(uid))}</strong> ·
    <a href="/logout?next={quote('/login?next=' + switch, safe='')}">Use another account</a></p>
  <form method="post" action="/oauth/authorize" class="consent-actions">{hidden}
    <input type="hidden" name="csrf" value="{html.escape(csrf)}">
    <button class="btn ghost" type="submit" name="decision" value="deny">Cancel</button>
    <button class="btn primary" type="submit" name="decision" value="allow" autofocus>Allow</button>
  </form>
  <p class="muted small consent-to">You'll go back to <strong>{host}</strong>.</p>"""
    return _html(f"Connect {client['n']}", body)


@router.post("/oauth/authorize", include_in_schema=False)
async def authorize_post(request: Request):
    from app.auth import COOKIE, session_uid
    form = await request.form()
    client, ruri, bad = _check(form)
    if bad:
        return bad
    uid = session_uid(request.cookies.get(COOKIE))
    if not settings.DASHBOARD_PASSWORD and uid is None:
        uid = tenancy.ADMIN_ID
    csrf = unseal("csrf", form.get("csrf"))
    if not uid or tenancy.is_guest(uid) or not csrf or csrf.get("u") != uid or csrf.get("c") != _h(form.get("client_id", "")):
        return _error_page("This page expired — start connecting again from your AI app.")
    state = form.get("state")
    if form.get("decision") != "allow":
        return _back(ruri, state, error="access_denied", error_description="The person cancelled.")
    chal = form.get("code_challenge") or ""
    if not chal or (form.get("code_challenge_method") or "plain") != "S256":
        return _back(ruri, state, error="invalid_request", error_description="PKCE with S256 is required.")
    code = seal("code", {"u": uid, "c": _h(form.get("client_id", "")), "r": ruri, "ch": chal,
                         "res": form.get("resource") or "", "sc": form.get("scope") or SCOPE,
                         "exp": int(time.time()) + CODE_TTL_S, "j": secrets.token_urlsafe(9)})
    log.info("OAuth: %s engedélyezte: %s", uid, client["n"])
    return _back(ruri, state, code=code)


# ---------- token ----------

def _client_auth(request: Request, form) -> tuple[Optional[str], Optional[str]]:
    cid, secret = form.get("client_id"), form.get("client_secret")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            from urllib.parse import unquote
            user, _, pw = base64.b64decode(auth[6:].strip()).decode().partition(":")
            cid, secret = unquote(user) or cid, unquote(pw) or secret
        except Exception:
            pass
    return cid, secret


@router.post("/oauth/token", include_in_schema=False)
async def token(request: Request):
    ctype = request.headers.get("content-type", "")
    try:
        form = await request.json() if "json" in ctype else await request.form()
    except Exception:
        return _err(400, "invalid_request", "Send the token request as a form (or JSON).")
    cid, secret = _client_auth(request, form)
    client = unseal("cl", cid)
    if not client:
        return _err(401, "invalid_client", "Unknown client — register again.")
    if client.get("m", "none") != "none" and not (secret and hmac.compare_digest(secret.encode(), _client_secret(cid).encode())):
        return _err(401, "invalid_client", "Wrong client secret.")
    grant = form.get("grant_type")
    from app import db
    if grant == "authorization_code":
        code = unseal("code", form.get("code"))
        if not code or code.get("c") != _h(cid):
            return _err(400, "invalid_grant", "The code is invalid or expired.")
        if form.get("redirect_uri") and form.get("redirect_uri") != code.get("r"):
            return _err(400, "invalid_grant", "redirect_uri doesn't match the one used to sign in.")
        verifier = form.get("code_verifier") or ""
        if not verifier or _b64(hashlib.sha256(verifier.encode()).digest()) != code.get("ch"):
            return _err(400, "invalid_grant", "PKCE check failed (code_verifier).")
        now = time.time()
        for j in [j for j, e in _used_codes.items() if e < now]:
            _used_codes.pop(j, None)
        if code.get("j") in _used_codes:
            return _err(400, "invalid_grant", "This code was already used.")
        _used_codes[code["j"]] = code["exp"]
        if not db.enabled():
            return _err(503, "temporarily_unavailable", "Connected apps need the account database, which isn't set up on this server.")
        created = await db.api_key_create(code["u"], f"{client['n'][:44]} (connected)")   # a visszavonható engedély
        kid, uid, sc = int(created["id"]), code["u"], code.get("sc") or SCOPE
        log.info("OAuth: token kiadva %s → %s (kulcs #%s)", client["n"], uid, kid)
    elif grant == "refresh_token":
        rt = unseal("rt", form.get("refresh_token"))
        if not rt or rt.get("c") != _h(cid):
            return _err(400, "invalid_grant", "The refresh token is invalid.")
        kid, uid, sc = int(rt["k"]), rt["u"], rt.get("sc") or SCOPE
        if not db.enabled() or not await db.api_key_active(kid, uid):
            return _err(400, "invalid_grant", "This connection was removed — connect again.")
    else:
        return _err(400, "unsupported_grant_type", "grant_type: authorization_code or refresh_token.")
    at = seal("at", {"k": kid, "u": uid, "exp": int(time.time()) + ACCESS_TTL_S})
    rt_new = seal("rt", {"k": kid, "u": uid, "c": _h(cid), "sc": sc})
    return JSONResponse({"access_token": at, "token_type": "Bearer", "expires_in": ACCESS_TTL_S,
                         "refresh_token": rt_new, "scope": sc},
                        headers={**_CORS, "Cache-Control": "no-store", "Pragma": "no-cache"})
