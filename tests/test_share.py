"""Megosztott munkaterület (OMLETT 4): A megoszt B-vel, B szerkeszt (új verzió A munkaterületén, A látja, B fizet),
C-t a web és az MCP is elutasítja; „megosztva velem” a tulajdonos nevével; a megosztott projekt nem jár le.
Postgres (TEST_DATABASE_URL / pgtemp), SQLite sehol."""
from __future__ import annotations

import json
import os
import time

import pytest

HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture()
def world(db_client, monkeypatch):
    """Három fiók (A tulajdonos, B szerkesztő, C kívülálló) bekapcsolt megosztással."""
    from app import db
    from app.auth import COOKIE, new_session
    monkeypatch.setenv("SHARING_ENABLED", "1")
    call = db_client.portal.call
    tag = os.urandom(3).hex()
    users = {}
    for k, name in (("a", "Tamás"), ("b", "Zoli"), ("c", "Cecil")):
        u = call(db.user_create, f"{k}{tag}@example.com", "jelszó12345", name, 5)
        users[k] = {**u, "key": call(db.api_key_create, u["id"], "mcp")["key"]}

    class W:
        client, u = db_client, users

        def req(self, who, method, url, **kw):
            db_client.cookies.clear()       # a süti most készül (a jelszó-csere a régit érvényteleníti)
            cookie = {"Cookie": f"{COOKIE}={new_session(users[who]['id'])}"}
            return db_client.request(method, url, headers={**kw.pop("headers", {}), **cookie}, **kw)

        def upload(self, who, name, data: bytes):
            r = self.req(who, "POST", "/api/upload", files={"file": (name, data, "application/octet-stream")})
            assert r.status_code == 200, r.text
            return r.json()["file_id"]

        def mcp(self, who, tool, args):
            h = {**HDR, "Authorization": f"Bearer {users[who]['key']}"}
            db_client.cookies.clear()
            db_client.post("/mcp/", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
            r = db_client.post("/mcp/", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                         "params": {"name": tool, "arguments": args}})
            res = r.json()["result"]
            text = res["content"][0]["text"]
            return bool(res.get("isError")), (json.loads(text) if text.startswith("{") else text)

        def call(self, fn, *a):
            return call(fn, *a)

    yield W()
    db_client.cookies.clear()


def _settle(w, fid, owner_uid, tries=100):
    """A verzió-sor háttérben íródik (write_meta → record_version_bg) — megvárjuk."""
    from app import db
    for _ in range(tries):
        rows = w.call(db._rows, "SELECT owner_id FROM file_versions WHERE file_id = %s", (fid,))
        if rows and rows[0]["owner_id"] == owner_uid:
            return
        time.sleep(0.02)
    raise AssertionError(f"no version row for {fid}")


def _project(w):
    """A: egy videóprojekt (.ecproj) + a felvétel mint a projekt médiafájlja."""
    proj = json.dumps({"items": {"1": {"kind": {"type": "Footage", "path": "clip.mp4"}}}}).encode()
    r = w.req("a", "POST", "/api/editor/save?name=Omlett.ecproj", content=proj)     # a projekt az outputs-ban él
    assert r.status_code == 200, r.text
    pid = r.json()["file_id"]
    r = w.req("a", "POST", f"/api/video/asset?project={pid}&name=clip.mp4", content=b"\x00\x00\x00\x18ftypmp42fake")
    assert r.status_code == 200, r.text
    return pid, r.json()["file_id"]


def test_a_shares_project_b_edits_new_version_a_sees_it(world):
    w = world
    pid, asset = _project(w)
    r = w.req("a", "POST", "/api/share", json={"file_id": pid, "email": w.u["b"]["email"], "role": "editor"})
    assert r.status_code == 200, r.text
    assert r.json()["pending"] is False and r.json()["role"] == "editor"

    # B: a „megosztva velem” listában A nevével; a projekt médiafájljai is elérhetők neki
    shared = w.req("b", "GET", "/api/files").json()["shared"]
    item = next(f for f in shared if f["file_id"] == pid)
    assert item["shared"] == {"owner_id": w.u["a"]["id"], "owner_name": "Tamás", "role": "editor"}
    assets = w.req("b", "GET", f"/api/video/assets?file_id={pid}").json()["assets"]
    assert [a["name"] for a in assets] == ["clip.mp4"] and f"u={w.u['a']['id']}" in assets[0]["url"]
    assert w.req("b", "GET", assets[0]["url"]).status_code == 200            # az aláírt link a tulajdonos munkaterületére

    # B a szerkesztőben ment → új verzió A munkaterületén, B a szerzője
    body = json.dumps({"items": {"1": {"kind": {"type": "Footage", "path": "clip.mp4"}}}, "edited": "Zoli"}).encode()
    r = w.req("b", "POST", f"/api/editor/save?name=Omlett.ecproj&source={pid}", content=body)
    assert r.status_code == 200, r.text
    v2 = r.json()["file_id"]
    assert "conflict" not in r.json()
    from app import tenancy
    from app.config import settings
    a_root = settings.DATA_DIR / "u" / w.u["a"]["id"]
    assert (a_root / "outputs" / v2).exists() and not (tenancy.user_root(w.u["b"]["id"]) / "outputs" / v2).exists()
    meta = json.loads((a_root / "outputs" / f"{v2}.meta.json").read_text())
    assert meta["edited_by"] == w.u["b"]["id"] and meta["source_file_id"] == pid and meta["assets"] == {"clip.mp4": asset}

    # A látja (a saját listájában, „Zoli” szerzővel), és a napló is mondja, ki mit csinált
    _settle(w, v2, w.u["a"]["id"])
    mine = w.req("a", "GET", "/api/files").json()
    got = next(f for f in mine["outputs"] if f["file_id"] == v2)
    assert got["edited_by_name"] == "Zoli"
    info = w.req("a", "GET", f"/api/share?file_id={pid}").json()
    assert info["can_manage"] and [p["name"] for p in info["people"]] == ["Zoli"]
    for _ in range(50):
        info = w.req("a", "GET", f"/api/share?file_id={pid}").json()
        if any(x["action"] == "version" and x["file_id"] == v2 for x in info["activity"]):
            break
        time.sleep(0.05)
    ev = next(x for x in info["activity"] if x["action"] == "version" and x["file_id"] == v2)
    assert ev["actor_id"] == w.u["b"]["id"] and ev["who"] == "Zoli"
    # B a saját listájában az új verziót is látja (a megosztott család része)
    assert any(f["file_id"] == v2 for f in w.req("b", "GET", "/api/files").json()["shared"])


def test_c_denied_in_web_routes_and_mcp(world):
    w = world
    pid, asset = _project(w)
    doc = w.upload("a", "jegyzet.md", "# Omlett\n\nTojás, sonka.".encode())
    w.req("a", "POST", "/api/share", json={"file_id": pid, "email": w.u["b"]["email"], "role": "editor"})
    w.req("a", "POST", "/api/share", json={"file_id": doc, "email": w.u["b"]["email"], "role": "viewer"})
    for method, url, kw in (("GET", f"/preview/{doc}", {}), ("GET", f"/api/thumb/{doc}", {}),
                            ("GET", f"/api/print/{doc}", {}), ("GET", f"/api/revisions?file_id={doc}", {}),
                            ("GET", f"/api/video/assets?file_id={pid}", {}), ("GET", f"/api/share?file_id={pid}", {}),
                            ("POST", f"/api/editor/save?name=x.md&source={doc}", {"content": b"# x"}),
                            ("POST", "/api/video/render", {"json": {"file_id": pid}}),
                            ("POST", "/api/make/convert", {"json": {"file_id": doc, "target": "html"}}),
                            ("DELETE", f"/api/files/{doc}", {})):
        r = w.req("c", method, url, **kw)
        assert r.status_code == 403, (url, r.status_code, r.text)
        assert "isn't shared with you" in r.json()["detail"]
    err, res = w.mcp("c", "files", {"action": "preview", "file_id": doc})
    assert err and "isn't shared with you" in res
    err, res = w.mcp("c", "find_replace", {"file_id": doc, "pattern": "sonka", "replacement": "bacon"})
    assert err and "isn't shared with you" in res
    err, res = w.mcp("c", "video", {"action": "render", "file_id": pid})
    assert err and "isn't shared with you" in res
    # B viewerként (doc): néz igen, szerkeszt nem — weben és MCP-n sem
    assert w.req("b", "GET", f"/preview/{doc}").status_code == 200
    r = w.req("b", "POST", f"/api/editor/save?name=x.md&source={doc}", content=b"# x")
    assert r.status_code == 403 and "viewing only" in r.json()["detail"]
    err, res = w.mcp("b", "find_replace", {"file_id": doc, "pattern": "sonka", "replacement": "bacon"})
    assert err and "viewing only" in res
    assert w.req("b", "DELETE", f"/api/files/{pid}").status_code == 403       # szerkesztő sem töröl
    # a tulajdonos fájlja érintetlen
    from app.config import settings
    assert (settings.DATA_DIR / "u" / w.u["a"]["id"] / "inputs" / doc).exists()


def test_b_pays_for_his_operation_and_mcp_edit_is_his(world):
    from app import billing, db
    w = world
    from docx import Document
    import io
    d = Document()
    d.add_paragraph("Az omlett-hadművelet: tojás, sonka, hagyma.")
    buf = io.BytesIO()
    d.save(buf)
    doc = w.upload("a", "recept.docx", buf.getvalue())
    w.req("a", "POST", "/api/share", json={"file_id": doc, "email": w.u["b"]["email"], "role": "editor"})
    # webes, keretet fogyasztó művelet (tartalmi ellenőrzés) A dokumentumán: B kerete fogy, A-é nem
    r = w.req("b", "POST", "/api/check", json={"file_id": doc})
    assert r.status_code == 200, r.text
    month = billing.month_key()
    assert w.call(db.usage_get, w.u["b"]["id"], month, "check") == 1
    assert w.call(db.usage_get, w.u["a"]["id"], month, "check") == 0
    # MCP: B szerkeszti A dokumentumát → új verzió A-nál, B a szerzője (a művelet gazdája végig B)
    seen = {}
    from app import tenancy
    real = billing.count_steps

    def spy():
        seen["actor"], seen["workspace"] = tenancy.current_user_id(), tenancy.workspace_id()
        return real()
    import unittest.mock as um
    with um.patch.object(billing, "count_steps", spy):
        err, res = w.mcp("b", "find_replace", {"file_id": doc, "pattern": "sonka", "replacement": "bacon"})
    assert not err, res
    assert seen == {"actor": w.u["b"]["id"], "workspace": w.u["a"]["id"]}
    from app.config import settings
    a_root = settings.DATA_DIR / "u" / w.u["a"]["id"]
    new = res["file_id"]
    assert (a_root / "outputs" / new).exists()
    assert json.loads((a_root / "outputs" / f"{new}.meta.json").read_text())["edited_by"] == w.u["b"]["id"]
    assert f"u={w.u['a']['id']}" in res["url"]


def test_mcp_files_list_shows_shared_with_me_with_owner_name(world):
    w = world
    doc = w.upload("a", "Omlett forgatókönyv.md", "# Omlett".encode())
    w.upload("c", "Más fájlja.md", b"# x")
    err, res = w.mcp("b", "files", {"action": "list"})
    assert not err and "shared_with_me" not in res
    w.req("a", "POST", "/api/share", json={"file_id": doc, "email": w.u["b"]["email"], "role": "viewer"})
    err, res = w.mcp("b", "files", {"action": "list"})
    assert not err, res
    got = [{k: f[k] for k in ("file_id", "name", "ext", "shared_by", "role")} for f in res["shared_with_me"]]
    assert got == [{"file_id": doc, "name": "Omlett forgatókönyv.md", "ext": "md", "shared_by": "Tamás", "role": "viewer"}]
    err, res = w.mcp("b", "files", {"action": "list", "query": "forgatókönyv"})
    assert [f["file_id"] for f in res["shared_with_me"]] == [doc]
    err, res = w.mcp("b", "files", {"action": "list", "query": "pizza"})
    assert "shared_with_me" not in res
    err, res = w.mcp("b", "files", {"action": "preview", "file_id": doc})      # a megosztott fájlt látja is
    assert not err, res
    err, res = w.mcp("c", "files", {"action": "list"})
    assert "shared_with_me" not in res
    # megosztás MCP-n: A meghívja C-t, majd visszavonja
    err, res = w.mcp("a", "files", {"action": "share", "file_id": doc, "email": w.u["c"]["email"], "role": "viewer"})
    assert not err and res["role"] == "viewer", res
    err, res = w.mcp("a", "files", {"action": "share", "file_id": doc})
    assert sorted(p["name"] for p in res["people"]) == ["Cecil", "Zoli"]
    err, res = w.mcp("b", "files", {"action": "share", "file_id": doc, "email": "x@example.com", "role": "editor"})
    assert err and "Only the owner" in res
    err, res = w.mcp("a", "files", {"action": "share", "file_id": doc, "email": w.u["c"]["email"], "role": "none"})
    assert not err and res["removed"] == 1
    err, res = w.mcp("c", "files", {"action": "preview", "file_id": doc})
    assert err and "isn't shared with you" in res


def test_shared_project_survives_retention_cleanup(world):
    from app.storage import cleanup
    w = world
    pid, asset = _project(w)
    lone = w.upload("a", "régi.md", b"# old")
    w.req("a", "POST", "/api/share", json={"file_id": pid, "email": w.u["b"]["email"], "role": "editor"})
    from app.config import settings
    root = settings.DATA_DIR / "u" / w.u["a"]["id"]
    old = time.time() - 40 * 86400
    for p in [*root.glob("inputs/*"), *root.glob("outputs/*")]:
        os.utime(p, (old, old))
    keep = w.call(cleanup._shared_keep, root)
    assert {pid, asset} <= keep
    removed = cleanup._sweep(root, 30 * 86400, keep)
    assert removed >= 2                                   # a magányos régi fájl és a metája
    assert (root / "outputs" / pid).exists() and (root / "outputs" / asset).exists()
    assert not (root / "inputs" / lone).exists()
    # visszavonva: a következő körben már lejár
    sid = w.req("a", "GET", f"/api/share?file_id={pid}").json()["people"][0]["share_id"]
    assert w.req("a", "DELETE", f"/api/share/{sid}").status_code == 200
    assert pid not in w.call(cleanup._shared_keep, root)


def test_pending_email_invite_resolves_on_signup(world):
    w = world
    doc = w.upload("a", "meghívó.md", b"# hi")
    email = f"zoli{os.urandom(3).hex()}@example.com"
    r = w.req("a", "POST", "/api/share", json={"file_id": doc, "email": email, "role": "editor"}).json()
    assert r["pending"] is True and "No e-mail is sent" in r["note"]
    w.client.cookies.clear()
    r = w.client.post("/signup", data={"email": email, "password": "hosszújelszó1", "name": "Új Zoli", "next": "/app"},
                      follow_redirects=False)
    assert r.status_code == 303
    from app.auth import COOKIE
    cookie = r.cookies.get(COOKIE) or w.client.cookies.get(COOKIE)
    w.client.cookies.clear()
    shared = w.client.get("/api/files", headers={"Cookie": f"{COOKIE}={cookie}"}).json()["shared"]
    assert [f["file_id"] for f in shared] == [doc] and shared[0]["shared"]["role"] == "editor"


def test_role_link_needs_a_signed_in_user(world, monkeypatch):
    from app.config import settings
    w = world
    doc = w.upload("a", "link.md", b"# link")
    link = w.req("a", "POST", "/api/share/link", json={"file_id": doc, "role": "editor"}).json()
    path = link["url"].replace(settings.PUBLIC_BASE_URL.rstrip("/"), "")
    monkeypatch.setattr(settings, "DASHBOARD_PASSWORD", "pw")           # élesben: belépés nélkül nem admin
    w.client.cookies.clear()
    r = w.client.get(path, headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    assert w.req("c", "GET", f"/preview/{doc}").status_code == 403
    r = w.req("c", "GET", path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/app/file/{doc}"
    assert w.req("c", "GET", f"/preview/{doc}").status_code == 200
    # az aláírt letöltő-link önmagában csak olvasni enged — szerkeszteni a link birtokában sem (B-nek nincs megosztása)
    a_url = w.req("a", "GET", "/api/files").json()["inputs"][0]["url"]
    assert w.client.get(a_url[a_url.index("/files/"):]).status_code == 200
    assert w.req("b", "POST", f"/api/editor/save?name=x.md&source={doc}", content=b"# x").status_code == 403


def test_sharing_off_keeps_workspaces_isolated(db_client, monkeypatch):
    """Kikapcsolt flaggel (alapértelmezés) semmi nem változik: más fájlja nem érhető el, a megosztó API 404."""
    from app import db
    from app.auth import COOKIE, new_session
    monkeypatch.delenv("SHARING_ENABLED", raising=False)
    call = db_client.portal.call
    a = call(db.user_create, f"off{os.urandom(3).hex()}@example.com", "jelszó12345", "Off", 0)
    b = call(db.user_create, f"off{os.urandom(3).hex()}@example.com", "jelszó12345", "Off B", 0)
    db_client.cookies.clear()
    up = db_client.post("/api/upload", files={"file": ("x.md", b"# x", "text/markdown")},
                        headers={"Cookie": f"{COOKIE}={new_session(a['id'])}"}).json()
    hb = {"Cookie": f"{COOKIE}={new_session(b['id'])}"}
    assert db_client.get(f"/preview/{up['file_id']}", headers=hb).status_code == 404
    assert db_client.post("/api/share", json={"file_id": up["file_id"], "email": "x@example.com"}, headers=hb).status_code == 404
    assert "shared" not in db_client.get("/api/files", headers=hb).json()
