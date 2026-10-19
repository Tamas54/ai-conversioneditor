"""Megosztott munkaterület (OMLETT 4): egy fájl vagy videóprojekt egy másik fiókkal, szerepkörrel.

    viewer   látja, letölti, előnézi (a családját is: ősök = nyersanyag, változatok, médiafájlok)
    editor   + új változatot csinál (minden szerkesztés új verzió a TULAJDONOS munkaterületén)
    owner    + megoszt, visszavon, töröl (a munkaterület gazdája mindig owner)

A fájlok fizikailag a tulajdonos munkaterületén maradnak (app/tenancy.py). Egy kérés, amely más munkaterületének
fájl-azonosítóját hozza (URL-ben, query-ben, JSON-törzsben vagy MCP-argumentumban), itt kap jogot: az EGY belépési
pont az auth-middleware (enter_request) — web, szerkesztő, render és MCP mind ezen megy át. Ha a jog megvan, a kérés
idejére a munkaterület a tulajdonosé (tenancy.workspace_id, user_root), a művelet gazdája viszont a belépett
felhasználó marad (tenancy.current_user_id): ő fizet (kredit, keret), az ő nevén fut a job, ő kerül a naplóba.

Tárolás Postgresben (db.py: shares, share_links, share_audit). Feature flag: SHARING_ENABLED=1
(alapból ki — kikapcsolva minden úgy működik, mint eddig: a munkaterületek fizikailag elszigeteltek).
E-mailt NEM küld (az appnak nincs levelező útja): a regisztrálatlan címre szóló meghívó függő marad, és a
regisztrációkor / belépéskor oldódik fel (claim_invites).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

from app import tenancy

log = logging.getLogger("aice.sharing")

ROLES = ("viewer", "editor", "owner")
_RANK = {r: i for i, r in enumerate(ROLES)}
FID_RE = re.compile(r"^[0-9a-f]{32}\.[A-Za-z0-9]{1,12}$")
NOT_SHARED = "This file isn't shared with you — ask its owner to share it (Share in the file's menu)."
VIEW_ONLY = "This file is shared with you for viewing only — ask its owner for edit access."
OWNER_ONLY = "Only the owner of this file can do that."
MIXED = "These files are in different people's workspaces — work on one shared project at a time."


def enabled() -> bool:
    from app import db
    return os.getenv("SHARING_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on") and db.enabled()


class AccessDenied(PermissionError):
    """403: a fájl nincs megosztva a felhasználóval, vagy csak megtekintésre. MCP-n (`mcp`) kész JSON-RPC válasz:
    az eszköz hibája (isError) — a kliens-AI érthető mondatot kap, és munka (job) sem indul."""

    def __init__(self, msg: str, mcp: Optional[dict[str, Any]] = None):
        super().__init__(msg)
        self.mcp = mcp


def _rank(role: Optional[str]) -> int:
    return _RANK.get(role or "", -1)


def _exists_in(uid: str, fid: str) -> bool:
    root = tenancy.user_root(uid)
    return (root / "inputs" / fid).exists() or (root / "outputs" / fid).exists()


class Shared:
    """Egy kérés megosztott munkaterülete: kié (owner), ki dolgozik benne (actor), fájlonként milyen joggal (roles).

    root_for(fid) adja a fájl helyét: a megosztott család és az e kérésben létrehozott fájlok a tulajdonosnál; a
    belépett felhasználó SAJÁT fájljai (pl. a projekthez hozott új felvétel) a sajátjában; új fájlt a tulajdonosnál
    csak szerkesztő hozhat létre. A kérés minden fájl-azonosítóját a belépéskor ellenőriztük (resolve) — a tulajdonos
    további fájljait itt csak a szerver saját kódja éri el (névadás, verziófa), nem a kérés."""

    def __init__(self, owner: str, actor: str, roles: dict[str, str], denied: Optional[dict[str, str]] = None):
        self.owner, self.actor, self.roles = owner, actor, dict(roles)
        self.created: set[str] = set()
        self.denied = denied or {}
        self.if_version: Optional[str] = None

    @property
    def role(self) -> str:
        return max(self.roles.values(), key=_rank, default="viewer") if self.owner != self.actor else "owner"

    def can_edit(self) -> bool:
        return _rank(self.role) >= _RANK["editor"]

    def root_for(self, fid: str) -> Path:
        if fid in self.denied:
            raise AccessDenied(self.denied[fid])
        own = tenancy.user_root(self.owner)
        if self.owner == self.actor or fid in self.roles or fid in self.created or _exists_in(self.owner, fid):
            return own
        if _exists_in(self.actor, fid):
            return tenancy.user_root(self.actor)
        if self.can_edit():
            self.created.add(fid)            # új változat / render a megosztott projektben
            return own
        raise AccessDenied(VIEW_ONLY)

    def workspace_of(self, fid: str) -> str:
        try:
            root = self.root_for(fid)
        except (AccessDenied, FileNotFoundError):
            return self.owner
        return self.actor if root == tenancy.user_root(self.actor) and self.actor != self.owner else self.owner

    def can_write(self, fid: str) -> bool:
        if self.owner == self.actor or fid in self.created:
            return True
        r = self.roles.get(fid)
        if r is not None:
            return _rank(r) >= _RANK["editor"]
        if _exists_in(self.actor, fid):
            return True
        return self.can_edit()


# ---------- család: egy megosztott fájl és ami hozzá tartozik ----------

_FAMILY_SQL = """
WITH RECURSIVE down(file_id, d) AS (
    SELECT %(r)s::text, 0
  UNION
    SELECT f.file_id, down.d + 1 FROM file_versions f JOIN down ON f.source_file_id = down.file_id
    WHERE f.owner_id = %(o)s AND down.d < 60
), up(file_id, d) AS (
    SELECT f.source_file_id, 0 FROM file_versions f
    WHERE f.file_id = %(r)s AND f.owner_id = %(o)s AND f.source_file_id IS NOT NULL
  UNION
    SELECT f.source_file_id, up.d + 1 FROM file_versions f JOIN up ON f.file_id = up.file_id
    WHERE f.owner_id = %(o)s AND f.source_file_id IS NOT NULL AND up.d < 60
)
SELECT DISTINCT file_id, 'down' AS k FROM down
UNION
SELECT DISTINCT a.value, 'down' FROM down JOIN file_versions f ON f.file_id = down.file_id AND f.owner_id = %(o)s
  CROSS JOIN LATERAL jsonb_each_text(CASE WHEN jsonb_typeof(f.extra->'assets') = 'object' THEN f.extra->'assets'
                                          ELSE '{}'::jsonb END) a
UNION
SELECT DISTINCT file_id, 'up' FROM up
"""


async def _family(owner: str, root: str) -> dict[str, str]:
    """A megosztott fájl családja a tulajdonos munkaterületén: fid → 'down' (maga, változatai, renderjei, a projekt
    médiafájljai) | 'up' (amiből készült: a nyersanyag — a meghívott csak nézheti)."""
    from app import db
    out: dict[str, str] = {}
    for r in await db._rows(_FAMILY_SQL, {"r": root, "o": owner}):
        if r["file_id"] and (r["k"] == "down" or r["file_id"] not in out):
            out[r["file_id"]] = r["k"]
    return out


async def _active_shares(owner: str, grantee: Optional[str] = None) -> list[dict[str, Any]]:
    from app import db
    if grantee is None:
        return await db._rows("SELECT * FROM shares WHERE owner_id = %s AND revoked_at IS NULL ORDER BY created_at", (owner,))
    return await db._rows("SELECT * FROM shares WHERE owner_id = %s AND grantee_id = %s AND revoked_at IS NULL", (owner, grantee))


async def _grants(owner: str, grantee: str) -> tuple[dict[str, str], dict[str, str]]:
    """(minden fájl → legerősebb szerep, csak a 'down' tagok → szerep) a tulajdonos megosztásaiból a meghívottnak."""
    roles: dict[str, str] = {}
    down: dict[str, str] = {}
    for s in await _active_shares(owner, grantee):
        for fid, k in (await _family(owner, s["file_id"])).items():
            r = s["role"] if k == "down" else "viewer"
            if _rank(r) > _rank(roles.get(fid)):
                roles[fid] = r
            if k == "down" and _rank(r) > _rank(down.get(fid)):
                down[fid] = r
    return roles, down


async def family_roles(owner: str, grantee: str) -> dict[str, str]:
    """Minden fájl, amit a tulajdonos megosztásai a meghívottnak adnak: fid → a legerősebb szerep."""
    return (await _grants(owner, grantee))[0]


def _inherit(owner: str, fid: str, down: dict[str, str]) -> Optional[str]:
    """Az épp most készült változat (a verzió-sora még íródik a háttérben): a metafájl forrás-láncán felfelé az első
    megosztott ('down') ős szerepe — a nyersanyag (up) ágaiba így sem lehet átlépni."""
    root, cur = tenancy.user_root(owner), fid
    for _ in range(20):
        if cur in down:
            return down[cur]
        meta = None
        for sub in ("outputs", "inputs"):
            m = root / sub / f"{cur}.meta.json"
            if m.exists():
                try:
                    meta = json.loads(m.read_text("utf-8"))
                except (OSError, ValueError):
                    meta = None
                break
        cur = (meta or {}).get("source_file_id")
        if not cur:
            return None
    return None


async def owner_of(fid: str, actor: Optional[str] = None) -> Optional[str]:
    from app import db
    rows = await db._rows("SELECT owner_id FROM file_versions WHERE file_id = %s", (fid,))
    if rows and rows[0]["owner_id"]:
        return rows[0]["owner_id"]
    if actor:                       # a sor még nincs meg (háttérben íródik): a velem megosztó munkaterületek a lemezen
        for r in await db._rows("SELECT DISTINCT owner_id FROM shares WHERE grantee_id = %s AND revoked_at IS NULL", (actor,)):
            if _exists_in(r["owner_id"], fid):
                return r["owner_id"]
    return None


async def access(actor: str, fid: str) -> tuple[Optional[str], Optional[str]]:
    """(tulajdonos, szerep) — saját fájl: (actor, owner); megosztott: (tulajdonos, szerep); egyébként (tulajdonos|None, None)."""
    if _exists_in(actor, fid):
        return actor, "owner"
    if not enabled():
        return None, None
    owner = await owner_of(fid, actor)
    if owner is None or owner == actor:
        return owner, ("owner" if owner == actor else None)
    roles, down = await _grants(owner, actor)
    return owner, roles.get(fid) or _inherit(owner, fid, down)


async def resolve(actor: str, fids: set[str], need: str = "viewer") -> Optional[Shared]:
    """AZ ellenőrzés: a kérés összes fájl-azonosítója → None (mind saját / ismeretlen) vagy a megosztott munkaterület.
    Ha valamelyikhez nincs (elég) jog → AccessDenied."""
    owners: dict[str, list[str]] = {}
    for fid in fids:
        if _exists_in(actor, fid):
            continue
        owner = await owner_of(fid, actor)
        if owner is None or owner == actor:
            continue                       # ismeretlen azonosító: a művelet maga mondja, hogy nincs ilyen fájl
        owners.setdefault(owner, []).append(fid)
    if not owners:
        return None
    if len(owners) > 1:
        raise AccessDenied(MIXED)
    owner, mine = next(iter(owners.items()))
    roles, down = await _grants(owner, actor)
    for fid in mine:
        r = roles.get(fid) or _inherit(owner, fid, down)
        if r is not None:
            roles[fid] = r
        if r is None:
            raise AccessDenied(NOT_SHARED)
        if _rank(r) < _rank(need):
            raise AccessDenied(VIEW_ONLY if need == "editor" else OWNER_ONLY)
    return Shared(owner, actor, roles)


# ---------- a belépési pont: minden kérés (web, szerkesztő, render, MCP) ----------

_SKIP = ("/static/", "/editor/", "/v1/", "/share/", "/api/events", "/api/account", "/api/jobs/", "/api/billing")
_MCP_VIEW_TOOLS = {"extract", "ocr_image", "describe_image", "extract_table_from_image", "validate_document", "catalog", "units"}
_MCP_VIEW_ACTIONS = {"files": {"list", "find", "search", "preview", "compare", "share", "shared"},
                     "history": {"lineage", "branches", "compare", "inbox"},
                     "video": {"info", "status", "job", "schema"}, "jobs": {"status"}}
_MCP_DEFAULT_ACTION = {"files": "list", "history": "inbox", "video": "make", "jobs": "create"}


def _collect(obj: Any, out: set[str], depth: int = 0) -> None:
    if depth > 8 or len(out) > 200:
        return
    if isinstance(obj, str):
        if FID_RE.match(obj):
            out.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect(v, out, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect(v, out, depth + 1)


def mcp_need(tool: str, args: dict[str, Any]) -> str:
    if tool in _MCP_VIEW_TOOLS:
        return "viewer"
    act = str(args.get("action") or _MCP_DEFAULT_ACTION.get(tool, "")).lower()
    return "viewer" if act in _MCP_VIEW_ACTIONS.get(tool, ()) else "editor"


async def _request_ids(request) -> tuple[set[str], str, Optional[str], Any]:
    """→ (fájl-azonosítók, kellő szerep, if_version, MCP-n a JSON-RPC üzenet(ek) azonosítója — webes kérésnél False)."""
    path, method = request.url.path, request.method
    ids: set[str] = set()
    if_version = request.query_params.get("if_version")
    if path.startswith("/mcp"):
        need, body, rpc_ids = "viewer", None, []
        if method == "POST" and int(request.headers.get("content-length") or 0) <= 64 * 1024 * 1024:
            try:
                body = json.loads(await request.body() or b"null")
            except ValueError:
                body = None
        for msg in body if isinstance(body, list) else [body]:
            if not isinstance(msg, dict) or msg.get("method") != "tools/call":
                continue
            rpc_ids.append(msg.get("id"))
            p = msg.get("params") or {}
            args = p.get("arguments") if isinstance(p.get("arguments"), dict) else {}
            _collect(args, ids)
            if_version = if_version or (args.get("if_version") if isinstance(args.get("if_version"), str) else None)
            n = mcp_need(str(p.get("name") or ""), args)
            need = n if _rank(n) > _rank(need) else need
        return ids, need, if_version, (rpc_ids if not isinstance(body, list) else [])
    for seg in path.split("/"):
        s = unquote(seg)
        if FID_RE.match(s):
            ids.add(s)
    for _, v in request.query_params.multi_items():
        if FID_RE.match(v):
            ids.add(v)
    if method in ("POST", "PUT", "PATCH", "DELETE") and "json" in request.headers.get("content-type", "") \
            and int(request.headers.get("content-length") or 0) <= 2 * 1024 * 1024:
        try:
            body = json.loads(await request.body() or b"null")
        except ValueError:
            body = None
        _collect(body, ids)
        if isinstance(body, dict) and isinstance(body.get("if_version"), str):
            if_version = if_version or body["if_version"]
    need = "viewer" if method in ("GET", "HEAD") else "editor"
    if method == "DELETE" and path.startswith("/api/files/"):
        need = "owner"
    return ids, need, if_version, False


async def enter_request(request, actor: str):
    """Az auth-middleware hívja a belépett felhasználóval: más munkaterületének fájlja → jogellenőrzés, és a kérés
    idejére a tulajdonos munkaterülete. → token (leave_request-nek) vagy None. Webes kérésnél AccessDenied → 403;
    MCP-n a hívott eszköz kapja a hibát (érthető üzenettel, a JSON-RPC válaszban)."""
    if not enabled():
        return None
    path = request.url.path
    if path == "/app" or path.startswith(("/app/",) + _SKIP):
        return None
    ids, need, if_version, is_mcp = await _request_ids(request)
    if not ids:
        return None
    try:
        sh = await resolve(actor, ids, need)
    except AccessDenied as e:
        if is_mcp is False:
            raise
        if len(is_mcp) == 1:           # egy eszközhívás: a válasz maga az eszköz hibája
            raise AccessDenied(str(e), mcp={"jsonrpc": "2.0", "id": is_mcp[0], "result": {
                "content": [{"type": "text", "text": str(e)}], "isError": True}}) from None
        sh = Shared(actor, actor, {}, denied={f: str(e) for f in ids if not _exists_in(actor, f)})
    if sh is None:
        return None
    sh.if_version = if_version if isinstance(if_version, str) and FID_RE.match(if_version) else None
    return tenancy.enter_shared(sh)


def leave_request(token) -> None:
    if token is not None:
        tenancy.leave_shared(token)


class workspace:
    """`with sharing.workspace(owner, actor, roles):` — szerveroldali kód a tulajdonos munkaterületén (lista, napló)."""

    def __init__(self, owner: str, actor: str, roles: dict[str, str]):
        self.sh = Shared(owner, actor, roles)

    def __enter__(self):
        self.tok = tenancy.enter_shared(self.sh)
        return self.sh

    def __exit__(self, *a):
        tenancy.leave_shared(self.tok)


# ---------- nevek, napló ----------

async def names(ids: set[str] | list[str]) -> dict[str, str]:
    from app import db
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    rows = await db._rows("SELECT id, name, email FROM users WHERE id = ANY(%s)", (ids,))
    out = {r["id"]: (r["name"] or (r["email"] or "").split("@")[0] or r["id"]) for r in rows}
    return {i: out.get(i) or ("Admin" if i == tenancy.ADMIN_ID else i) for i in ids}


async def audit(owner: str, actor: str, fid: Optional[str], action: str, detail: Optional[dict[str, Any]] = None) -> None:
    from app import db
    await db._rows("INSERT INTO share_audit (owner_id, actor_id, file_id, action, detail) VALUES (%s, %s, %s, %s, %s)",
                   (owner, actor, fid, action, json.dumps(detail or {}, ensure_ascii=False, default=str)))


async def activity(owner: str, fids: set[str], limit: int = 30) -> list[dict[str, Any]]:
    from app import db
    rows = await db._rows("SELECT actor_id, file_id, action, detail, created_at FROM share_audit WHERE owner_id = %s "
                          "AND (file_id = ANY(%s) OR file_id IS NULL) ORDER BY created_at DESC, id DESC LIMIT %s",
                          (owner, list(fids), limit))
    nm = await names({r["actor_id"] for r in rows})
    return [{**r, "who": nm.get(r["actor_id"], r["actor_id"])} for r in rows]


async def _has_shares(owner: str) -> bool:
    from app import db
    return bool(await db._rows("SELECT 1 FROM shares WHERE owner_id = %s AND revoked_at IS NULL LIMIT 1", (owner,)))


async def shared_family(owner: str) -> set[str]:
    """A tulajdonos összes élő megosztásának családja (megőrzés: ezek nem járnak le)."""
    out: set[str] = set()
    for s in await _active_shares(owner):
        out |= set(await _family(owner, s["file_id"]))
    return out


# ---------- megosztás, visszavonás, link, meghívó ----------

async def index_workspace(owner: str) -> int:
    """A tulajdonos fájljainak verzió-sorai (a megosztás családja ebből épül): ami hiányzik vagy gazdátlan
    (a 10-11 előtti sorok), azt a metafájlokból pótolja. → hány sort írt."""
    from app import db
    root = tenancy.user_root(owner)
    found: dict[str, tuple[dict[str, Any], int]] = {}
    for sub in ("inputs", "outputs"):
        d = root / sub
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if not p.is_file() or p.name.endswith(".meta.json"):
                continue
            m = p.with_name(p.name + ".meta.json")
            try:
                meta = json.loads(m.read_text("utf-8")) if m.exists() else {}
            except (OSError, ValueError):
                meta = {}
            found[p.name] = (meta, p.stat().st_size)
    if not found:
        return 0
    have = {r["file_id"] for r in await db._rows(
        "SELECT file_id FROM file_versions WHERE file_id = ANY(%s) AND owner_id IS NOT NULL", (list(found),))}
    n = 0
    for fid, (meta, size) in found.items():
        if fid in have:
            continue
        extra = {k: v for k, v in meta.items() if k not in db._KNOWN}
        await db._rows(
            """INSERT INTO file_versions (file_id, source_file_id, operation, label, ext, size_bytes, extra, owner_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (file_id) DO UPDATE SET owner_id = COALESCE(file_versions.owner_id, EXCLUDED.owner_id),
                 source_file_id = COALESCE(file_versions.source_file_id, EXCLUDED.source_file_id),
                 extra = EXCLUDED.extra || file_versions.extra""",
            (fid, meta.get("source_file_id"), meta.get("operation"), meta.get("label") or meta.get("original_filename"),
             fid.rsplit(".", 1)[-1].lower() if "." in fid else None, size, json.dumps(extra, ensure_ascii=False, default=str), owner))
        n += 1
    return n


async def _manage_check(actor: str, fid: str) -> str:
    """Megoszthatja-e (owner szerep)? → a munkaterület tulajdonosa."""
    owner, role = await access(actor, fid)
    if owner is None or role is None:
        raise AccessDenied(NOT_SHARED)
    if role != "owner":
        raise AccessDenied(OWNER_ONLY)
    return owner


async def share(actor: str, fid: str, *, email: Optional[str] = None, user_id: Optional[str] = None,
                role: str = "editor", keep_location: bool = False) -> dict[str, Any]:
    """Meghívás (felhasználó vagy e-mail) vagy szerepváltás; role="none" = visszavonás. Regisztrálatlan e-mail →
    függő meghívó (regisztráláskor / belépéskor oldódik fel; levelet nem küldünk)."""
    from app import db
    role = (role or "editor").strip().lower()
    if role in ("remove", "revoke", "none"):
        role = "none"
    elif role not in ROLES:
        raise ValueError("role: viewer, editor or owner (or none to remove)")
    owner = await _manage_check(actor, fid)
    email = (email or "").strip().lower() or None
    grantee: Optional[str] = None
    if user_id:
        u = await db.user_get(user_id)
        if not u:
            raise ValueError("no such user")
        grantee, email = u["id"], (u.get("email") or email)
    elif email:
        if "@" not in email:
            raise ValueError("give an e-mail address (or the person's user id)")
        rows = await db._rows("SELECT id FROM users WHERE email = lower(%s)", (email,))
        grantee = rows[0]["id"] if rows else None
    else:
        raise ValueError("whom to share with: email (or user_id)")
    if grantee == owner:
        raise ValueError("that's the owner of this file")
    if grantee == actor:
        raise ValueError("that's you")
    if grantee and tenancy.is_guest(grantee):
        raise ValueError("guests can't receive shared files — they need an account")
    if grantee:
        cur = await db._rows("SELECT id FROM shares WHERE owner_id = %s AND file_id = %s AND grantee_id = %s "
                             "AND revoked_at IS NULL", (owner, fid, grantee))
    else:
        cur = await db._rows("SELECT id FROM shares WHERE owner_id = %s AND file_id = %s AND grantee_id IS NULL "
                             "AND lower(grantee_email) = %s AND revoked_at IS NULL", (owner, fid, email))
    if role == "none":
        for r in cur:
            await db._rows("UPDATE shares SET revoked_at = now() WHERE id = %s", (r["id"],))
        await audit(owner, actor, fid, "unshare", {"grantee": grantee, "email": email})
        return {"removed": len(cur), "file_id": fid}
    await index_workspace(owner)
    if cur:
        rows = await db._rows("UPDATE shares SET role = %s WHERE id = %s RETURNING *", (role, cur[0]["id"]))
        act = "role"
    else:
        rows = await db._rows("""INSERT INTO shares (owner_id, file_id, grantee_id, grantee_email, role, created_by, accepted_at)
                                 VALUES (%s, %s, %s, %s, %s, %s, CASE WHEN %s::text IS NULL THEN NULL ELSE now() END)
                                 RETURNING *""", (owner, fid, grantee, email, role, actor, grantee))
        act = "share"
    removed = 0 if keep_location else await _strip_family_location(owner, fid)
    await audit(owner, actor, fid, act, {"grantee": grantee, "email": email, "role": role, "location_removed": removed})
    s = rows[0]
    out = {"share_id": s["id"], "file_id": fid, "role": role, "pending": grantee is None, "location_removed": removed}
    if grantee is None:
        from app.config import settings
        out["note"] = (f"{email} has no account yet — the invitation waits for them. No e-mail is sent: tell them to sign "
                       f"up with this address at {settings.PUBLIC_BASE_URL.rstrip('/')}/signup — the file then shows "
                       "under Shared with me.")
    return out


async def _strip_family_location(owner: str, fid: str) -> int:
    """Megosztáskor a család felvételeiből/fotóiból kikerül a GPS-/helyadat (a privacy-réteg; 0, ha nincs)."""
    try:
        from app.storage import privacy
    except ImportError:
        return 0
    return await privacy.strip_files(owner, list(await _family(owner, fid)))


async def unshare(actor: str, share_id: int) -> dict[str, Any]:
    from app import db
    rows = await db._rows("SELECT * FROM shares WHERE id = %s AND revoked_at IS NULL", (share_id,))
    if not rows:
        raise ValueError("no such share")
    s = rows[0]
    owner = await _manage_check(actor, s["file_id"]) if actor != s["owner_id"] else actor
    if owner != s["owner_id"]:
        raise AccessDenied(OWNER_ONLY)
    await db._rows("UPDATE shares SET revoked_at = now() WHERE id = %s", (share_id,))
    await audit(owner, actor, s["file_id"], "unshare", {"grantee": s["grantee_id"], "email": s["grantee_email"]})
    return {"removed": share_id}


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_link(actor: str, fid: str, role: str = "viewer") -> dict[str, Any]:
    """Szerepkörös link: csak BELÉPETT felhasználónak ér valamit (a /share/<token> belépést kér), és onnantól az ő
    fiókjához kötött megosztás lesz belőle. (Az aláírt letöltő-link önmagában soha nem ad szerkesztési jogot.)"""
    from app import db
    from app.config import settings
    role = (role or "viewer").lower()
    if role not in ("viewer", "editor"):
        raise ValueError("a link gives viewer or editor access")
    owner = await _manage_check(actor, fid)
    await index_workspace(owner)
    token = secrets.token_urlsafe(24)
    rows = await db._rows("INSERT INTO share_links (token_hash, owner_id, file_id, role, created_by) VALUES (%s, %s, %s, %s, %s) "
                          "RETURNING id, role, created_at", (_token_hash(token), owner, fid, role, actor))
    removed = await _strip_family_location(owner, fid)
    await audit(owner, actor, fid, "link", {"role": role, "link_id": rows[0]["id"], "location_removed": removed})
    return {"link_id": rows[0]["id"], "role": role, "url": f"{settings.PUBLIC_BASE_URL.rstrip('/')}/share/{token}",
            "note": "Works only for people who sign in — they get their own access to it."}


async def revoke_link(actor: str, link_id: int) -> dict[str, Any]:
    from app import db
    rows = await db._rows("SELECT * FROM share_links WHERE id = %s AND revoked_at IS NULL", (link_id,))
    if not rows:
        raise ValueError("no such link")
    await _manage_check(actor, rows[0]["file_id"])
    await db._rows("UPDATE share_links SET revoked_at = now() WHERE id = %s", (link_id,))
    await audit(rows[0]["owner_id"], actor, rows[0]["file_id"], "link revoked", {"link_id": link_id})
    return {"revoked": link_id}


async def join_link(actor: str, token: str) -> str:
    """A link megnyitása belépve → megosztás a belépett felhasználónak (a meglévőnél gyengébbet nem ad). → file_id."""
    from app import db
    rows = await db._rows("SELECT * FROM share_links WHERE token_hash = %s AND revoked_at IS NULL", (_token_hash(token),))
    if not rows:
        raise ValueError("This link doesn't work any more — ask for a new one.")
    ln = rows[0]
    if tenancy.is_guest(actor):
        raise AccessDenied("Sign in or create an account to open a shared file.")
    if actor != ln["owner_id"]:
        cur = await db._rows("SELECT id, role FROM shares WHERE owner_id = %s AND file_id = %s AND grantee_id = %s "
                             "AND revoked_at IS NULL", (ln["owner_id"], ln["file_id"], actor))
        if not cur:
            await db._rows("INSERT INTO shares (owner_id, file_id, grantee_id, role, created_by, accepted_at) "
                           "VALUES (%s, %s, %s, %s, %s, now()) RETURNING id",
                           (ln["owner_id"], ln["file_id"], actor, ln["role"], ln["created_by"]))
        elif _rank(ln["role"]) > _rank(cur[0]["role"]):
            await db._rows("UPDATE shares SET role = %s WHERE id = %s", (ln["role"], cur[0]["id"]))
        await audit(ln["owner_id"], actor, ln["file_id"], "joined by link", {"role": ln["role"], "link_id": ln["id"]})
    return ln["file_id"]


async def claim_invites(uid: str, email: Optional[str]) -> int:
    """Regisztráció / belépés: az e-mail címre szóló függő meghívók a fiókhoz kerülnek."""
    from app import db
    if not email or not db.enabled():
        return 0
    rows = await db._rows("""UPDATE shares SET grantee_id = %s, accepted_at = now()
                             WHERE grantee_id IS NULL AND revoked_at IS NULL AND lower(grantee_email) = lower(%s)
                             RETURNING owner_id, file_id, role""", (uid, email.strip()))
    for r in rows:
        await audit(r["owner_id"], uid, r["file_id"], "invite accepted", {"role": r["role"]})
    return len(rows)


async def people(actor: str, fid: str) -> dict[str, Any]:
    """Ki fér hozzá ehhez a fájlhoz (a családját lefedő megosztások), a linkek és a napló."""
    from app import db
    owner, role = await access(actor, fid)
    if owner is None or role is None:
        raise AccessDenied(NOT_SHARED)
    rows, fam_all = [], {fid}
    for s in await _active_shares(owner):
        fam = await _family(owner, s["file_id"])
        if fid in fam:
            rows.append(s)
            fam_all |= set(fam)
    nm = await names({owner, *[r["grantee_id"] for r in rows if r["grantee_id"]]})
    out: dict[str, Any] = {
        "file_id": fid, "owner": {"id": owner, "name": nm.get(owner, owner)}, "your_role": role, "can_manage": role == "owner",
        "people": [{"share_id": r["id"], "user_id": r["grantee_id"], "name": nm.get(r["grantee_id"]) if r["grantee_id"] else None,
                    "email": r["grantee_email"], "role": r["role"], "pending": r["grantee_id"] is None,
                    "shared_file_id": r["file_id"]} for r in rows],
        "activity": await activity(owner, fam_all),
    }
    if role == "owner":
        links = await db._rows("SELECT id, file_id, role, created_at FROM share_links WHERE owner_id = %s AND revoked_at IS NULL "
                               "ORDER BY created_at DESC", (owner,))
        out["links"] = [ln for ln in links if ln["file_id"] in fam_all]
    return out


# ---------- „megosztva velem” ----------

_KINDS = ("inputs", "outputs")


async def shared_with_me(actor: str) -> list[dict[str, Any]]:
    """A velem megosztott fájlok (a családjukkal), ugyanabban az alakban, mint a /api/files elemei, + shared:
    {owner_id, owner_name, role}. A változatok úgy csoportosulnak, mint a saját fájloknál (superseded_by)."""
    from app import db
    from app.routes.dashboard import _list_dir, _mark_versions
    if not enabled() or tenancy.is_guest(actor):
        return []
    owners = [r["owner_id"] for r in await db._rows(
        "SELECT DISTINCT owner_id FROM shares WHERE grantee_id = %s AND revoked_at IS NULL", (actor,))]
    if not owners:
        return []
    nm = await names(owners)
    out: list[dict[str, Any]] = []
    for owner in owners:
        roles = await family_roles(owner, actor)
        if not roles:
            continue
        with workspace(owner, actor, roles):
            root = tenancy.user_root(owner)
            items = [it for kind, sub in (("input", "inputs"), ("output", "outputs"))
                     for it in _list_dir(root / sub, kind, only=set(roles))]
            eds = await names({it["edited_by"] for it in items if it.get("edited_by")})
            for it in items:
                it["shared"] = {"owner_id": owner, "owner_name": nm.get(owner, owner), "role": roles[it["file_id"]]}
                if it.get("edited_by"):
                    it["edited_by_name"] = eds.get(it["edited_by"])
            _mark_versions(items)
        out += items
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


# ---------- napló: új változat a megosztott családban ----------

async def on_new_version(fid: str, owner: Optional[str], meta: dict[str, Any]) -> None:
    """db.record_version hívja minden ÚJ fájlra: megosztott családban napló (ki, mit, mikor)."""
    if not enabled() or not owner:
        return
    try:
        if not await _has_shares(owner):
            return
        fam = await shared_family(owner)
        if fid not in fam:
            return
        sh = tenancy.shared()
        actor = sh.actor if sh is not None else tenancy.current_user_id()
        await audit(owner, actor, fid, "version", {k: meta.get(k) for k in ("operation", "source_file_id", "label")})
    except Exception as e:                      # a napló soha ne állítsa meg a szerkesztést
        log.warning("megosztás-napló kimaradt (%s): %s", fid, e)


# ---------- MCP ----------

def mcp_shared_items(items: list[dict[str, Any]], *, query: Optional[str] = None, kind: Optional[set] = None,
                     ext: Optional[str] = None, versions: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    """files(action="list"): a velem megosztottak, a saját lista szűrőivel, a tulajdonos nevével."""
    import datetime
    from app.mcp_server import _STOP, _name_words, _word_hits
    if not items:
        return []
    ids = {f["file_id"] for f in items}
    if not versions:
        items = [f for f in items if not (f.get("superseded_by") and f["superseded_by"] in ids)]
    if ext:
        items = [f for f in items if f.get("ext") == ext.lower().lstrip(".")]
    if kind:
        items = [f for f in items if f.get("ext") in kind]
    if query and query.strip():
        words = [w for w in _name_words(query) if w not in _STOP] or _name_words(query)
        items = [f for f in items if sum(_word_hits(w, _name_words(f"{f.get('label') or ''} {f.get('original_filename') or ''}"))
                                         for w in words) > 0]
    return [{"file_id": f["file_id"], "name": f.get("label"), "ext": f.get("ext"), "size_kb": f.get("size_kb"),
             "when": datetime.datetime.fromtimestamp(f.get("mtime", 0), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
             "shared_by": f["shared"]["owner_name"], "role": f["shared"]["role"],
             **({"edited_by": f["edited_by_name"]} if f.get("edited_by_name") else {})} for f in items[:limit]]


async def mcp_action(action: str, file_id: Optional[str], *, email: Optional[str] = None,
                     role: Optional[str] = None) -> dict[str, Any]:
    if not enabled():
        raise ValueError("Sharing isn't switched on on this server.")
    actor = tenancy.current_user_id()
    if tenancy.is_guest(actor):
        raise ValueError("Sharing needs an account.")
    if action == "shared":
        return {"shared_with_me": mcp_shared_items(await shared_with_me(actor))}
    if not file_id:
        raise ValueError(f"{action} needs file_id")
    if action == "share_link":
        return await create_link(actor, file_id, role or "viewer")
    if email:
        return await share(actor, file_id, email=email, role=role or "editor")
    return await people(actor, file_id)


# MCP-referencia (a „schema” akcióba köthető — a vezető dönt, hová): a files eszköz megosztó akciói
SHARE_SCHEMA_REF = """Sharing (files tool; when the server has sharing on):
  files(action="share", file_id, email, role="viewer"|"editor"|"owner"|"none") — invite someone (an e-mail without an
    account waits until they sign up; no e-mail is sent), change their role, or remove them (role="none"). Without
    email: who has access + recent activity (who changed what).
  files(action="share_link", file_id, role="viewer"|"editor") — a link that works only for signed-in people.
  files(action="list") — your files, plus shared_with_me [{file_id, name, shared_by, role}].
  Editing a shared file works like your own: every edit is a new version in the owner's workspace, paid by you."""
