"""Megosztott projekt változatai (OMLETT 4): optimista zárolás — ha közben más mentett újabbat, az új változat
„<Név> változata” lesz (semmi nem vész el), a felhasználó választ; if_version MCP-n; tanácsadó szerkesztési zár."""
from __future__ import annotations

import json
import os
import time

from test_share import _settle, world  # noqa: F401  (közös fixture: A, B, C fiók, bekapcsolt megosztás)


def _save(w, who, source, body: bytes, name="Omlett.ecproj"):
    url = f"/api/editor/save?name={name}" + (f"&source={source}" if source else "")
    r = w.req(who, "POST", url, content=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_parallel_edits_fork_and_the_user_chooses(world):
    w = world
    pid = _save(w, "a", None, b'{"items": {}}')["file_id"]
    w.req("a", "POST", "/api/share", json={"file_id": pid, "email": w.u["b"]["email"], "role": "editor"})
    # mindketten pid-ből indulnak; A ment előbb
    v2a = _save(w, "a", pid, b'{"items": {}, "by": "Tamas"}')
    assert "conflict" not in v2a
    _settle(w, v2a["file_id"], w.u["a"]["id"])
    v2b = _save(w, "b", pid, b'{"items": {}, "by": "Zoli"}')
    c = v2b["conflict"]
    assert c["fork"]["file_id"] == v2b["file_id"] and c["other"]["file_id"] == v2a["file_id"]
    assert c["fork"]["by"] == "Zoli" and c["other"]["by"] == "Tamás"
    assert "Zoli's version" in v2b["label"] and "Zoli's version" in c["fork"]["label"]
    # mindkét változat megvan A munkaterületén
    from app.config import settings
    out = settings.DATA_DIR / "u" / w.u["a"]["id"] / "outputs"
    assert (out / v2a["file_id"]).exists() and (out / v2b["file_id"]).exists()
    # A is látja az ütközést (a saját változatánál), és választ: Zolié legyen az aktuális
    _settle(w, v2b["file_id"], w.u["a"]["id"])
    for _ in range(100):                 # a fork-jelölés a verzió-sorba a háttérben is íródik
        seen = w.req("a", "GET", f"/api/share/conflict?file_id={v2a['file_id']}").json()["conflict"]
        if seen:
            break
        time.sleep(0.02)
    assert seen and seen["fork"]["file_id"] == v2b["file_id"]
    r = w.req("a", "POST", "/api/share/choose", json={"file_id": v2b["file_id"]}).json()
    assert r["resolved"] is True and r["current"] is True
    assert w.req("a", "GET", f"/api/share/conflict?file_id={v2a['file_id']}").json()["conflict"] is None
    assert w.req("b", "GET", f"/api/share/conflict?file_id={v2b['file_id']}").json()["conflict"] is None
    files = {f["file_id"]: f for f in w.req("a", "GET", "/api/files").json()["outputs"]}
    assert files[v2a["file_id"]].get("superseded_by") == v2b["file_id"]          # a választott a lánc éle
    # B tovább szerkeszti a sajátját (sorban) → nincs ütközés
    v3b = _save(w, "b", v2b["file_id"], b'{"items": {}, "by": "Zoli 2"}')
    assert "conflict" not in v3b
    # napló: ütközés és választás
    acts = [x["action"] for x in w.req("a", "GET", f"/api/share?file_id={pid}").json()["activity"]]
    assert "conflict" in acts and "choose" in acts


def test_same_person_branching_is_not_a_conflict(world):
    w = world
    pid = _save(w, "a", None, b'{"items": {}}')["file_id"]
    w.req("a", "POST", "/api/share", json={"file_id": pid, "email": w.u["b"]["email"], "role": "editor"})
    v2 = _save(w, "a", pid, b'{"v": 2}')
    _settle(w, v2["file_id"], w.u["a"]["id"])
    v2x = _save(w, "a", pid, b'{"v": "2x"}')       # szándékos ág ugyanattól a felhasználótól
    assert "conflict" not in v2x


def test_mcp_if_version_and_conflict_in_result(world):
    w = world
    doc = w.upload("a", "recept.md", "# Omlett\n\ntojás, sonka".encode())
    w.req("a", "POST", "/api/share", json={"file_id": doc, "email": w.u["b"]["email"], "role": "editor"})
    err, v2 = w.mcp("a", "find_replace", {"file_id": doc, "pattern": "sonka", "replacement": "bacon"})
    assert not err, v2
    _settle(w, v2["file_id"], w.u["a"]["id"])
    # B a régiből indult (if_version = amit megnyitott) → ütközés, az eredményben conflict
    err, res = w.mcp("b", "find_replace", {"file_id": doc, "pattern": "tojás", "replacement": "3 tojás", "if_version": doc})
    assert not err, res
    assert res["conflict"]["other"]["file_id"] == v2["file_id"] and res["conflict"]["fork"]["by"] == "Zoli"
    # B a legfrissebbet szerkeszti, és azt is mondja → nincs ütközés
    err, res2 = w.mcp("b", "find_replace", {"file_id": v2["file_id"], "pattern": "tojás", "replacement": "4 tojás",
                                            "if_version": v2["file_id"]})
    assert not err and "conflict" not in res2, res2
    # választás MCP-n
    _settle(w, res["file_id"], w.u["a"]["id"])
    err, ch = w.mcp("b", "files", {"action": "choose", "file_id": res["file_id"]})
    assert not err and ch["resolved"] is True, ch


def test_edit_lock_tells_who_is_editing(world):
    w = world
    pid = _save(w, "a", None, b'{"items": {}}')["file_id"]
    w.req("a", "POST", "/api/share", json={"file_id": pid, "email": w.u["b"]["email"], "role": "editor"})
    v2 = _save(w, "a", pid, b'{"v": 2}')["file_id"]
    assert w.req("b", "POST", "/api/share/lock", json={"file_id": pid}).json() == {"locked": True, "yours": True}
    held = w.req("a", "POST", "/api/share/lock", json={"file_id": v2}).json()     # ugyanaz a lánc → ugyanaz a zár
    assert held["yours"] is False and held["by"] == "Zoli" and "both versions are kept" in held["note"]
    w.req("b", "DELETE", f"/api/share/lock?file_id={pid}")
    assert w.req("a", "POST", "/api/share/lock", json={"file_id": v2}).json()["yours"] is True
    # C nem is kérdezheti
    assert w.req("c", "POST", "/api/share/lock", json={"file_id": pid}).status_code == 403


def test_owner_if_version_on_own_file(world):
    """A tulajdonos saját fájljánál is él az if_version (nincs megosztott munkaterület a kérésben)."""
    w = world
    doc = w.upload("a", "lista.md", "# Bevásárlás\n\ntojás".encode())
    w.req("a", "POST", "/api/share", json={"file_id": doc, "email": w.u["b"]["email"], "role": "editor"})
    err, vb = w.mcp("b", "find_replace", {"file_id": doc, "pattern": "tojás", "replacement": "10 tojás"})
    assert not err, vb
    _settle(w, vb["file_id"], w.u["a"]["id"])
    # A kimondja, hogy B változatát már látta (if_version = az él) → nem ütközés, pedig a forrásnak van más ága
    err, ok = w.mcp("a", "find_replace", {"file_id": doc, "pattern": "tojás", "replacement": "8 tojás", "if_version": vb["file_id"]})
    assert not err and "conflict" not in ok, ok
    # if_version nélkül (vagy a régivel) ugyanez ütközés: „Tamás's version”
    err, va = w.mcp("a", "find_replace", {"file_id": doc, "pattern": "tojás", "replacement": "6 tojás", "if_version": doc})
    assert not err and va["conflict"]["fork"]["by"] == "Tamás" and va["conflict"]["other"]["file_id"] == vb["file_id"], va
