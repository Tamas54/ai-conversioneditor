"""Megosztás — ACL-regressziók a független átnézésből (OMLETT 4, 10-19): egy megosztott fájl azonosítója NEM nyithat
utat a tulajdonos többi fájljához (útvonal-feloldás, kihagyott törzsrészek, listázás, napló, lektorálási kérések),
és a megőrzés DB-hiba esetén sem kapcsol ki csendben."""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from test_share import _settle, world  # noqa: F401  (A, B, C fiók, bekapcsolt megosztás)


def _two(w, role="viewer"):
    """A: x (megosztva B-vel) és y (privát)."""
    x = w.upload("a", "megosztott.md", "# Közös\n\ntojás".encode())
    y = w.upload("a", "titkos.md", "# Titok\n\nsonka".encode())
    r = w.req("a", "POST", "/api/share", json={"file_id": x, "email": w.u["b"]["email"], "role": role})
    assert r.status_code == 200, r.text
    _settle(w, y, w.u["a"]["id"])
    return x, y


# ---------- 1. a feloldás csak a megosztott családot adja ----------

def test_root_for_only_resolves_the_shared_family(world):
    from app import sharing, tenancy
    from app.storage import public_url
    from app.tools.crafts import existing
    w = world
    x, y = _two(w)
    a, b = w.u["a"]["id"], w.u["b"]["id"]

    async def probe():           # (a portálban nem assertálunk: a pytest-hiba BaseException, leállítaná a portált)
        out = {}
        tok = tenancy.set_user(b)
        try:
            sh = await sharing.resolve(b, {x}, "viewer")
            st = tenancy.enter_shared(sh)
            try:
                out["x_owner"] = existing(x).parent.parent.name
                try:
                    out["y"] = str(existing(y))
                except sharing.AccessDenied:
                    out["y"] = "denied"
                out["y_link"] = public_url(y)
            finally:
                tenancy.leave_shared(st)
        finally:
            tenancy.reset(tok)
        return out
    got = w.call(probe)
    assert got["x_owner"] == a                      # a megosztott fájl: A munkaterületén
    assert got["y"] == "denied"                     # A másik fájlja: nem
    assert f"u={a}" not in got["y_link"]            # aláírt link sem készül rá A nevében


def test_big_json_body_cannot_smuggle_an_unshared_file(world):
    """A query-ben a megosztott x megnyitja A munkaterületét, a 2 MB-nál nagyobb törzsben az y-t eddig senki nem nézte."""
    w = world
    x, y = _two(w, role="editor")
    r = w.req("b", "POST", f"/api/make/convert?ref={x}",
              json={"file_id": y, "target": "html", "pad": "x" * (2 * 1024 * 1024 + 10)})
    assert r.status_code == 403, r.text[:300]


def test_unscannable_body_is_refused_in_a_shared_request(world, monkeypatch):
    from app import sharing
    w = world
    x, _ = _two(w, role="editor")
    r = w.req("b", "POST", f"/api/make/convert?ref={x}", content=b"\x00\x01binary", headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 415, r.text[:300]
    monkeypatch.setattr(sharing, "BODY_CAP", 1000)
    r = w.req("b", "POST", f"/api/make/convert?ref={x}", json={"file_id": x, "target": "html", "pad": "x" * 2000})
    assert r.status_code == 413, r.text[:300]


# ---------- 2. a kérés-szkenner mindenhol megtalálja az azonosítót ----------

def _request(method: str, path: str, body: bytes = b"", ctype: str = "application/json", query: bytes = b""):
    from starlette.requests import Request
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}
    headers = [(b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode())]
    return Request({"type": "http", "method": method, "path": path, "query_string": query, "headers": headers}, receive)


def test_scanner_finds_ids_in_every_shape():
    from app import sharing
    y = "0123456789abcdef0123456789abcdef.md"
    esc = "\\u00" + format(ord(y[0]), "x") + y[1:]                   # JSON-ben \u-val írva
    cases = [
        ("POST", "/api/x", f"file_id={y}&n=1".encode(), "application/x-www-form-urlencoded"),
        ("POST", "/api/x", f"file_id=%{ord(y[0]):x}{y[1:]}".encode(), "application/x-www-form-urlencoded"),
        ("POST", "/api/x", json.dumps({"file_id": y, "pad": "x" * (2 * 1024 * 1024 + 10)}).encode(), "application/json"),
        ("POST", "/api/x", json.dumps({"image_url": f"https://h/files/{y}?t=1"}).encode(), "application/json"),
        ("POST", "/api/x", json.dumps({"ids": f"aa,{y},bb"}).encode(), "application/json"),
        ("POST", "/api/x", json.dumps({"spec": '{"file_id": "%s"}' % esc}).encode(), "application/json"),
        ("POST", "/api/x", json.dumps({y: 1}).encode(), "application/json"),
        ("POST", "/api/x", (b"--B\r\nContent-Disposition: form-data; name=\"file_id\"\r\n\r\n" + y.encode() + b"\r\n--B--\r\n"),
         "multipart/form-data; boundary=B"),
        ("POST", "/mcp/", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "video", "arguments": {"action": "edit", "spec": json.dumps({"layers": [{"image": {"file_id": y}}]})}}}).encode(),
         "application/json"),
        ("POST", "/mcp/", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "files", "arguments": {"action": "compare", "file_id": f" {y}"}}}).encode(), "application/json"),
    ]
    for method, path, body, ctype in cases:
        got = asyncio.run(sharing._request_ids(_request(method, path, body, ctype)))
        assert y in got[0], (path, ctype, body[:80])
    got = asyncio.run(sharing._request_ids(_request("GET", "/api/x", query=f"ids=aa,{y}".encode())))
    assert y in got[0]


# ---------- 3. a munkaterület-szintű kód a megosztott kérésben is a SAJÁT munkaterületen fut ----------

def test_listing_inside_a_shared_request_is_the_actors_own(world):
    w = world
    x, y = _two(w)
    mine = w.upload("b", "zoli.md", b"# Zoli")
    d = w.req("b", "GET", f"/api/files?ref={x}").json()
    own = {f["file_id"] for f in d["inputs"] + d["outputs"]}
    assert mine in own and y not in own and x not in own, own
    assert [f["file_id"] for f in d["shared"]] == [x]


def test_review_inbox_and_history_stay_inside_what_is_shared(world):
    w = world
    f = w.upload("a", "nyersanyag.md", b"# Nyers")
    p = w.req("a", "POST", f"/api/editor/save?name=projekt.md&source={f}", content=b"# Projekt").json()["file_id"]
    w.req("a", "POST", "/api/share", json={"file_id": f, "email": w.u["c"]["email"], "role": "viewer"})
    p2 = w.req("a", "POST", f"/api/editor/save?name=masik.md&source={f}", content=b"# Masik titok").json()["file_id"]
    w.req("a", "POST", "/api/share", json={"file_id": p, "email": w.u["b"]["email"], "role": "editor"})
    for fid in (f, p, p2):
        _settle(w, fid, w.u["a"]["id"])
    assert w.req("a", "POST", "/api/reviews", json={"file_id": p2, "note": "titkos megjegyzés"}).status_code == 200
    # B (p szerkesztője, f nézője): A lektorálási kérései nem látszanak, p2 sem a történetben, sem a naplóban
    items = w.req("b", "GET", f"/api/reviews?ref={p}").json()["items"]
    assert not any(i["file_id"] == p2 for i in items), items
    err, res = w.mcp("b", "history", {"action": "inbox", "other_file_id": p})
    assert not err and not any(i["file_id"] == p2 for i in res["open"]), res
    hist = w.req("b", "GET", f"/api/history/{f}").json()
    assert p2 not in {d["file_id"] for d in hist["descendants"]}, hist
    acts = w.req("b", "GET", f"/api/share?file_id={p}").json()["activity"]
    assert p2 not in {a["file_id"] for a in acts}, acts


# ---------- 5. megőrzés: DB-hiba nem kapcsolja ki csendben ----------

def test_cleanup_db_error_keeps_the_last_known_shared_files(world, monkeypatch, caplog):
    from app import sharing
    from app.config import settings
    from app.storage import cleanup
    w = world
    x, y = _two(w)
    root = settings.DATA_DIR / "u" / w.u["a"]["id"]
    keep = w.call(cleanup._shared_keep, root)
    assert x in keep and y not in keep

    async def broken(owner):
        raise RuntimeError("db down")
    monkeypatch.setattr(sharing, "shared_family", broken)
    with caplog.at_level(logging.ERROR, logger="aice.cleanup"):
        again = w.call(cleanup._shared_keep, root)
    assert again == keep and "*" not in again           # az utolsó ismert állapot: a többi fájl lejárhat, a megosztott nem
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


# ---------- 3. a pdfcraft --root kimondva, fájlonként ----------

def test_pdf_work_on_a_shared_pdf_and_a_raw_path_to_a_private_one(world, samples):
    from app import sharing, tenancy
    from app.config import settings
    from app.tools import crafts
    w = world
    x = w.upload("a", "megosztott.pdf", samples.pdf("a.pdf").read_bytes())
    y = w.upload("a", "titkos.pdf", samples.pdf("b.pdf").read_bytes())
    mine = w.upload("b", "zoli.pdf", samples.pdf("c.pdf").read_bytes())
    w.req("a", "POST", "/api/share", json={"file_id": x, "email": w.u["b"]["email"], "role": "editor"})
    # B szerkesztőként dolgozik A megosztott PDF-jén (és a sajátjával fűzi) — a homokozó mindkettőt lefedi
    r = w.req("b", "POST", "/api/make/pdf", json={"action": "page_numbers", "file_ids": [x]})
    assert r.status_code == 200, r.text[:300]
    r = w.req("b", "POST", "/api/make/pdf", json={"action": "merge", "file_ids": [x, mine]})
    assert r.status_code == 200, r.text[:300]
    # nyers útvonal A privát fájljára: a kapu már a szövegben megtalálja
    a_root = settings.DATA_DIR / "u" / w.u["a"]["id"]
    err, res = w.mcp("b", "office", {"app": "pdf", "file_id": x, "commands": [
        {"tool": "doc_combine", "args": {"paths": [str(a_root / "inputs" / y)]}}]})
    assert err and "isn't shared with you" in res, res
    # és ha mégis odáig jutna: a lépéssor homokozója sem engedi (csak a megosztott család)

    async def probe():
        b = w.u["b"]["id"]
        tok = tenancy.set_user(b)
        try:
            st = tenancy.enter_shared(await sharing.resolve(b, {x}, "editor"))
            try:
                ok = str(crafts.script_root([{"tool": "doc_open", "args": {"path": str(a_root / "inputs" / x)}}]))
                try:
                    crafts.script_root([{"tool": "doc_open", "args": {"path": str(a_root / "inputs" / y)}}])
                    bad = "allowed"
                except sharing.AccessDenied:
                    bad = "denied"
                return ok, bad
            finally:
                tenancy.leave_shared(st)
        finally:
            tenancy.reset(tok)
    ok, bad = w.call(probe)
    assert ok == str(a_root.resolve()) and bad == "denied"
