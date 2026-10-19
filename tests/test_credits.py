"""Kreditkönyv (OMLETT 2.3): a meglévő egyenlegek megmaradnak (nyitó „adjust” sor, idempotensen), minden mozgás egy
könyvsor + users.credits ugyanabban a tranzakcióban (sorzár), idempotencia-kulcs, hold → capture/release, árszabályok,
napi egyeztetés (alacsony egyenleg, eltérés) és a végpontok (történet, becslés, admin-árazás)."""
from __future__ import annotations

import asyncio
import secrets

import pytest


def _uid() -> str:
    return secrets.token_hex(8)


def _ledger_sum(call, uid):
    from app import db
    rows = call(db._rows, "SELECT COALESCE(sum(delta), 0) AS s, count(*) AS n FROM credit_ledger WHERE user_id = %s", (uid,))
    return int(rows[0]["s"]), int(rows[0]["n"])


def _balance(call, uid):
    from app import db
    return call(db.user_get, uid)["credits"]


def test_migration_keeps_existing_balances_idempotently(db_client):
    """Élesben vannak kézzel (SQL-ből) jóváírt egyenlegek, napló nélkül: a séma újrafuttatása egy nyitó sort ír, a
    users.credits nem változik, és másodszor sem ír újat."""
    from app import db
    call = db_client.portal.call
    a, b = _uid(), _uid()
    call(db._rows, "INSERT INTO users (id, email, credits) VALUES (%s, %s, 40), (%s, %s, 7) RETURNING id",
         (a, f"{a}@x.hu", b, f"{b}@x.hu"))
    # b-nek van egy régi (bővítés előtti alakú) sora is: 5 a naplóban, 7 az egyenleg → a nyitó sor csak a +2 különbség
    call(db._rows, "INSERT INTO credit_ledger (user_id, delta, balance_after, reason) VALUES (%s, 5, 5, 'signup') RETURNING id", (b,))
    for _ in range(2):                                   # újraindítás kétszer: ugyanaz az eredmény
        call(db.stop)
        call(db.start)
    assert _balance(call, a) == 40 and _balance(call, b) == 7
    assert _ledger_sum(call, a) == (40, 1) and _ledger_sum(call, b) == (7, 2)
    op = call(db._rows, "SELECT kind, idempotency_key, delta FROM credit_ledger WHERE user_id = %s AND reason = 'opening balance'", (b,))
    assert op == [{"kind": "adjust", "idempotency_key": f"opening:{b}", "delta": 2}]


def test_new_account_grant_is_in_the_ledger(db_client):
    from app import db
    call = db_client.portal.call
    u = call(db.user_create, f"{_uid()}@example.com", "jelszó123456", "Új", 30)
    assert _balance(call, u["id"]) == 30 and _ledger_sum(call, u["id"]) == (30, 1)


def test_apply_idempotent_insufficient_and_locked(db_client):
    from app import credits, db
    call = db_client.portal.call
    u = call(db.user_create, f"{_uid()}@example.com", "jelszó123456", None, 5)["id"]
    r1 = call(lambda: credits.apply(u, -2, "capture", "teszt", idempotency_key=f"k:{u}"))
    r2 = call(lambda: credits.apply(u, -2, "capture", "teszt", idempotency_key=f"k:{u}"))
    assert r1["balance"] == 3 and r2["duplicate"] and _balance(call, u) == 3
    with pytest.raises(db.InsufficientCredits) as e:
        call(lambda: credits.apply(u, -9, "capture", "túl sok"))
    assert (e.value.needed, e.value.available) == (9, 3) and "insufficient_credits" in str(e.value)
    assert _balance(call, u) == 3

    async def race():                                   # 10 párhuzamos 1-es levonás 3 kreditre: pontosan 3 megy át
        res = await asyncio.gather(*(credits.apply(u, -1, "capture", "verseny") for _ in range(10)), return_exceptions=True)
        return sum(1 for x in res if isinstance(x, dict)), sum(1 for x in res if isinstance(x, db.InsufficientCredits))
    assert call(race) == (3, 7)
    assert _balance(call, u) == 0 and _ledger_sum(call, u)[0] == 0


def test_hold_capture_release(db_client):
    from app import credits, db
    call = db_client.portal.call
    u = call(db.user_create, f"{_uid()}@example.com", "jelszó123456", None, 50)["id"]
    h = call(lambda: credits.hold(u, 20, "op1", operation="ai_clip", provider="kie"))
    assert h == {"balance": 30, "held": 20}
    # siker: a tényleges ár (12) — a webhook és a lekérdezés is elszámolna: másodszor semmi
    for _ in range(2):
        call(lambda: credits.settle(u, "op1", 20, 12, operation="ai_clip", provider="kie", provider_task_id="t1", provider_cost=80))
    assert _balance(call, u) == 38 and _ledger_sum(call, u) == (38, 4)
    kinds = [r["kind"] for r in call(db._rows, "SELECT kind FROM credit_ledger WHERE user_id = %s ORDER BY id", (u,))]
    assert kinds == ["grant", "hold", "release", "capture"]
    # hiba: teljes feloldás
    call(lambda: credits.hold(u, 30, "op2", operation="enhance"))
    assert _balance(call, u) == 8
    call(lambda: credits.release(u, "op2", 30, operation="enhance", provider_cost=0))
    assert _balance(call, u) == 38
    # a foglalt összegnél többet sosem von le
    call(lambda: credits.hold(u, 5, "op3", operation="x"))
    assert call(lambda: credits.settle(u, "op3", 5, 99, operation="x"))["charged"] == 5 and _balance(call, u) == 33
    # fedezet nélkül a hold el sem indul
    with pytest.raises(db.InsufficientCredits):
        call(lambda: credits.hold(u, 1000, "op4", operation="x"))
    assert _balance(call, u) == 33


def test_admin_usage_is_logged_not_charged(db_client):
    from app import credits, db
    call = db_client.portal.call
    assert call(lambda: credits.hold("admin", 50, "adm1", operation="ai_clip"))["held"] == 0
    call(lambda: credits.settle("admin", "adm1", 0, 17, operation="ai_clip", provider="kie", provider_cost=112))
    rows = call(db._rows, "SELECT kind, delta, provider_cost, meta FROM credit_ledger WHERE idempotency_key = 'admin:adm1'")
    assert rows[0]["kind"] == "admin_usage" and rows[0]["delta"] == 0 and rows[0]["meta"]["would_charge"] == 17
    assert float(rows[0]["provider_cost"]) == 112


def test_pricing_rules_price_and_fallback(db_client):
    from app import credits
    call = db_client.portal.call
    call(lambda: credits.rule_save({"operation": "t_op", "unit": "second", "provider_cost": 28, "multiplier": 0.15, "min_charge": 3}))
    call(lambda: credits.rule_save({"operation": "t_op", "model": "m2", "unit": "second", "provider_cost": 63, "multiplier": 0.15}))
    r = call(lambda: credits.rule_for("t_op", "m1"))              # nincs m1-szabály → a művelet általános sora
    assert float(r["provider_cost"]) == 28
    assert credits.price(r, 4) == 17                                # ceil(28 × 4 × 0.15 = 16.8)
    assert credits.price(r, 0.1) == 3                               # min_charge
    assert credits.price(call(lambda: credits.rule_for("t_op", "m2")), 5) == 48   # ceil(47.25)
    assert credits.price(r, provider_units=80) == 12               # a tényleges szolgáltatói költségből
    assert credits.price(None, 9) == 0                              # nincs szabály: helyi művelet, ingyen
    call(lambda: credits.rule_save({"operation": "t_free", "provider_cost": 0, "min_charge": 5}))
    assert credits.price(call(lambda: credits.rule_for("t_free")), 3) == 0     # 0 ár: a min_charge sem számít


def test_endpoints_history_estimate_and_admin_pricing(db_client):
    from app import credits, db
    from app.auth import COOKIE, new_session
    call = db_client.portal.call
    call(lambda: credits.rule_save({"operation": "t_est", "unit": "second", "provider_cost": 10, "multiplier": 0.5}))
    u = call(db.user_create, f"{_uid()}@example.com", "jelszó123456", None, 12)["id"]
    call(lambda: credits.apply(u, -2, "capture", "valami"))
    db_client.cookies.set(COOKIE, new_session(u))
    h = db_client.get("/api/account/ledger").json()
    assert h["balance"] == 10 and [e["delta"] for e in h["entries"]] == [-2, 12]
    est = db_client.get("/api/estimate", params={"operation": "t_est", "units": 3}).json()
    assert est["credits"] == 15 and est["balance"] == 10
    assert db_client.get("/api/admin/pricing").status_code == 403
    assert db_client.post("/api/admin/pricing", json={"operation": "hack"}).status_code == 403
    db_client.cookies.clear()                       # DASHBOARD_PASSWORD nélkül: admin
    r = db_client.post("/api/admin/pricing", json={"operation": "mcp:t_tool", "provider_cost": 2, "multiplier": 1})
    assert r.status_code == 200 and r.json()["operation"] == "mcp:t_tool"
    assert any(x["operation"] == "mcp:t_tool" for x in db_client.get("/api/admin/pricing").json()["rules"])
    assert db_client.post("/api/admin/pricing", json={"operation": "x", "unit": "parsec"}).status_code == 400
    assert db_client.delete(f"/api/admin/pricing/{r.json()['id']}").status_code == 200
    st = db_client.get("/api/admin/credits/status").json()
    assert {"notices", "balances", "ledger", "pricing" if False else "open_tasks"} <= set(st)


def test_reconcile_low_balance_and_drift(db_client, monkeypatch):
    from app import credits, db, providers
    call = db_client.portal.call
    call(db._rows, "DELETE FROM provider_balance WHERE provider = 'fake' RETURNING id")
    call(db._rows, "UPDATE admin_notices SET resolved_at = now() WHERE resolved_at IS NULL RETURNING id")

    class Fake:
        bal = 5000.0

        async def balance(self):
            return self.bal
    fake = Fake()
    monkeypatch.setattr(providers, "active", lambda: {"fake": fake})
    out = call(credits.reconcile)
    assert out["providers"]["fake"]["balance"] == 5000 and out["providers"]["fake"]["drift"] is None
    # a könyv szerint 300 ment el (egy elszámolt feladat), a szolgáltató viszont 1000-et vont le → eltérés, és alacsony
    u = call(db.user_create, f"{_uid()}@example.com", "jelszó123456", None, 100)["id"]
    call(lambda: credits.hold(u, 50, "rc1", operation="ai_clip", provider="fake"))
    call(lambda: credits.settle(u, "rc1", 50, 45, operation="ai_clip", provider="fake", provider_cost=300))
    fake.bal = 1000.0
    out = call(credits.reconcile)
    assert out["providers"]["fake"]["ledger_cost"] == 300 and out["providers"]["fake"]["drift"] == 3700
    kinds = {n["kind"] for n in call(credits.notices)}
    assert {"fake_low_balance", "fake_drift"} <= kinds
    call(credits.reconcile)                          # nem ismétli ugyanazt a figyelmeztetést
    assert sum(1 for n in call(credits.notices) if n["kind"] == "fake_low_balance") == 1


def test_free_topup_goes_through_the_ledger(db_client):
    from app import billing, db
    call = db_client.portal.call
    u = call(db.user_create, f"{_uid()}@example.com", "jelszó123456", None, 3)["id"]
    assert call(db.free_topup, u, "2099-01", 10) == 7
    assert call(db.free_topup, u, "2099-01", 10) == 0            # ugyanabban a hónapban nem még egyszer
    assert _balance(call, u) == 10 and _ledger_sum(call, u) == (10, 2)
    assert billing.PLANS["free"]["monthly_credits"] == 10


def test_reconcile_loop_runs_under_an_advisory_lock(db_client, monkeypatch):
    """A napi egyeztetés a lifespan háttérhurkában fut; a zár ugyanazon a kapcsolaton fog és enged (több példány: egy fut)."""
    from app import credits
    ran = []

    async def fake():
        ran.append(1)
        return {}
    monkeypatch.setattr(credits, "reconcile", fake)
    monkeypatch.setenv("RECONCILE_FIRST_S", "0")

    async def once():
        t = asyncio.create_task(credits._reconcile_loop())
        for _ in range(100):
            if ran:
                break
            await asyncio.sleep(0.02)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        from app import db
        async with db._pool.connection() as conn:                                  # a zár elengedve
            free = (await (await conn.execute("SELECT pg_try_advisory_lock(7351902)")).fetchone())[0]
            await conn.execute("SELECT pg_advisory_unlock(7351902)")
        return free
    assert db_client.portal.call(once) is True and ran == [1]
