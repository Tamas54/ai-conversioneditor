"""OMLETT 1.5: minden MCP-eszközhívás a webes felület kreditútján megy (token → felhasználó): a nem-admin hívása levon, az
admin nem fizet, de naplózva van (admin_usage, delta 0), kevés kreditnél insufficient_credits (szükséges + elérhető) és a
munka el sem indul; az eredmény studio_ai mezője megmondja a felhasznált kreditet és az egyenleget."""
from __future__ import annotations

import json
import secrets

HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _call(client, name, args, key=None):
    hs = {**HDR, **({"Authorization": f"Bearer {key}"} if key else {})}
    client.post("/mcp/", headers=hs, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    r = client.post("/mcp/", headers=hs, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                               "params": {"name": name, "arguments": args}})
    res = r.json()["result"]
    text = res["content"][0]["text"]
    return res.get("isError"), (json.loads(text) if text.startswith("{") else text)


def _user(call, credits):
    from app import db
    u = call(db.user_create, f"{secrets.token_hex(6)}@example.com", "jelszó123456", None, credits)
    return u["id"], call(db.api_key_create, u["id"], "mcp")["key"]


def _outputs(uid):
    from app import tenancy
    return sorted(p.name for p in (tenancy.user_root(uid) / "outputs").glob("*") if not p.name.endswith(".json"))


def test_mcp_call_charges_the_token_user_admin_is_logged_low_balance_never_starts(db_client):
    from app import credits, db
    call = db_client.portal.call
    call(lambda: credits.rule_save({"operation": "mcp:files.new", "provider_cost": 3, "multiplier": 1, "unit": "job"}))
    try:
        # nem-admin: levon, és az eredmény megmondja
        uid, key = _user(call, 10)
        err, res = _call(db_client, "files", {"action": "new", "kind": "document"}, key)
        assert not err and res["file_id"].endswith(".docx")
        assert res["studio_ai"]["credits_used"] == 3 and res["studio_ai"]["balance"] == 7
        assert call(db.user_get, uid)["credits"] == 7
        kinds = [r["kind"] for r in call(db._rows, "SELECT kind FROM credit_ledger WHERE user_id = %s ORDER BY id", (uid,))]
        assert kinds == ["grant", "hold", "release", "capture"]

        # admin (helyi fejlesztés: kulcs nélkül admin): nem fizet, de egy admin_usage sor a „kiszámlázandó” összeggel
        before = call(db._rows, "SELECT count(*) AS n FROM credit_ledger WHERE user_id = 'admin' AND kind = 'admin_usage'")[0]["n"]
        err, res = _call(db_client, "files", {"action": "new", "kind": "document"})
        assert not err and res["studio_ai"]["admin"] is True and res["studio_ai"]["credits_used"] == 0
        assert res["studio_ai"]["would_charge"] == 3
        rows = call(db._rows, "SELECT delta, meta FROM credit_ledger WHERE user_id = 'admin' AND kind = 'admin_usage' ORDER BY id")
        assert len(rows) == before + 1 and rows[-1]["delta"] == 0 and rows[-1]["meta"]["would_charge"] == 3

        # kevés kredit: insufficient_credits a szükséges és az elérhető összeggel, és a fájl el sem készül
        poor, pkey = _user(call, 2)
        files_before = _outputs(poor)
        err, text = _call(db_client, "files", {"action": "new", "kind": "document"}, pkey)
        assert err and "insufficient_credits" in text and "needs 3 credits" in text and "you have 2" in text
        assert _outputs(poor) == files_before and call(db.user_get, poor)["credits"] == 2

        # a hibával végződő hívás nem fizet (a foglalás visszajár)
        call(lambda: credits.rule_save({"operation": "mcp:files.bogus", "provider_cost": 1, "multiplier": 1}))
        err, _ = _call(db_client, "files", {"action": "bogus"}, key)
        assert err and call(db.user_get, uid)["credits"] == 7
    finally:
        for r in call(credits.rules):
            if r["operation"] in ("mcp:files.new", "mcp:files.bogus"):
                call(lambda: credits.rule_delete(r["id"]))


def test_free_mcp_call_has_no_credit_noise(db_client):
    """Ár nélküli (helyi) eszköz: nincs levonás, és az eredmény sem kap studio_ai mezőt."""
    from app import db
    call = db_client.portal.call
    uid, key = _user(call, 5)
    err, res = _call(db_client, "files", {"action": "new", "kind": "document"}, key)
    assert not err and "studio_ai" not in res and call(db.user_get, uid)["credits"] == 5
