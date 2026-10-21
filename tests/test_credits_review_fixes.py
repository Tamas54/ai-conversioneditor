"""A kredit + kie felülvizsgálat (OMLETT) regressziós tesztjei — kreditkönyv, árazás, végpontok:

 3    a folytatás (resume_loop) a kapcsoló nélkül is fut.
 7    a törölt induló ár nem tér vissza; a levonás sosem több a foglaltnál.
 8    a felhasználó nem látja a költségalapunkat (provider_cost, kie-azonosító, meta).
 9    a webhook-titok nem kerül a hozzáférési naplóba.
 10   fiók nélküli levonás = nincs fedezet; ingyenes feltöltés egy tranzakcióban; az AI-becslés is kapcsoló mögött.
"""
from __future__ import annotations

import asyncio
import logging
import secrets

import pytest

from test_ai_enhance import UpscaleProv, _clip, _put_as, _user  # noqa: F401


@pytest.fixture()
def prov(db_client, monkeypatch):
    from app import credits, providers
    from app.tools import ai_video
    p = UpscaleProv()
    monkeypatch.setattr(providers, "get", lambda name="kie": p)
    monkeypatch.setattr(providers, "enabled_for", lambda uid=None: True)
    monkeypatch.setattr(ai_video, "POLL_FIRST_S", 0.05)
    db_client.portal.call(credits.seed_rules)
    return p


# ---------- 3: a folytatás a kapcsoló nélkül is ----------

def test_resume_loop_starts_even_with_ai_video_off(db_client, monkeypatch):
    from app import credits, providers
    monkeypatch.delenv("AI_VIDEO", raising=False)
    monkeypatch.setattr(credits, "_reconcile_loop", lambda: asyncio.sleep(0))
    assert providers.active() == {}

    async def go():
        await credits.start()
        names = [t.get_coro().__qualname__ for t in credits._tasks]
        await credits.stop()
        return names
    assert "resume_loop" in db_client.portal.call(go)


# ---------- 7: ár nélkül nincs ingyen kie; a levonás a foglalt összegen belül ----------

def test_deleted_seeded_rule_stays_deleted(db_client):
    from app import credits, db
    call = db_client.portal.call
    call(credits.seed_rules)
    r = call(lambda: credits.rule_for("enhance", "topaz/video-upscale", "4x"))
    assert call(lambda: credits.rule_delete(r["id"]))
    try:
        call(db.stop)
        call(db.start)
        call(credits.seed_rules)                                         # amit a lifespan minden induláskor tesz
        assert call(lambda: credits.rule_for("enhance", "topaz/video-upscale", "4x")) is None
    finally:                                                             # visszaállítás (a többi teszt ára)
        call(lambda: credits.rule_save({k: r[k] for k in ("operation", "model", "tier", "unit", "provider", "provider_cost",
                                                           "multiplier", "min_charge", "note")}))
    assert call(lambda: credits.rule_for("enhance", "topaz/video-upscale", "4x")) is not None


def test_settle_never_charges_more_than_was_held(db_client):
    from app import credits, db
    call = db_client.portal.call
    u = call(db.user_create, f"{secrets.token_hex(6)}@example.com", "jelszó123456", None, 3)["id"]
    assert call(lambda: credits.settle(u, "opx", 0, 50, operation="enhance", provider="kie"))["charged"] == 0
    assert call(db.user_get, u)["credits"] == 3


# ---------- 8: a költségalap csak az adminé ----------

def test_users_do_not_see_our_cost_basis(db_client, prov, tmp_path):
    from app import credits
    from app.auth import COOKIE, new_session
    call = db_client.portal.call
    uid, _ = _user(call, 50)
    call(lambda: credits.hold(uid, 20, f"cb{uid}", operation="ai_clip", provider="kie"))
    call(lambda: credits.settle(uid, f"cb{uid}", 20, 12, operation="ai_clip", provider="kie", provider_task_id="kie-task-123",
                                provider_cost=80, meta={"estimate": 20}))
    hs = {"Cookie": f"{COOKIE}={new_session(uid)}"}
    db_client.cookies.clear()
    entries = db_client.get("/api/account/ledger", headers=hs).json()["entries"]
    assert entries and not any(k in e for e in entries for k in ("provider_cost", "provider_task_id", "meta"))
    q = {"operation": "ai_clip", "model": "wan/3-0-video", "tier": "720p", "units": 5}
    est = db_client.get("/api/estimate", params=q, headers=hs).json()
    assert est["credits"] == 26 and "provider_cost" not in est
    assert "provider_cost" in db_client.get("/api/estimate", params=q).json()               # az admin látja
    fid = _put_as(uid, _clip(tmp_path))
    e2 = db_client.post("/api/ai/estimate", json={"action": "enhance", "file_id": fid, "factor": 2}, headers=hs).json()
    assert e2["credits"] >= 4 and "provider_cost" not in e2


# ---------- 9: a webhook-titok nem kerül a naplóba ----------

def test_webhook_token_is_not_written_to_the_access_log():
    import app.routes.ai_video  # noqa: F401  (a szűrő a modul betöltésekor kerül fel)
    lg = logging.getLogger("uvicorn.access")
    rec = lg.makeRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                        ("1.2.3.4:5", "POST", "/webhooks/kie?token=s3cr3t-value&x=1", "1.1", 200), None)
    assert lg.filter(rec)
    assert "s3cr3t" not in rec.getMessage() and "token=***" in rec.getMessage()


# ---------- 10: apróságok ----------

def test_spend_without_an_account_is_insufficient_credits(db_client):
    from app import db
    with pytest.raises(db.InsufficientCredits):
        db_client.portal.call(lambda: db.credits_spend("g0123456789abcdef", 1, "AI picture"))


def test_free_topup_is_one_transaction(db_client, monkeypatch):
    from app import credits, db
    call = db_client.portal.call
    u = call(db.user_create, f"{secrets.token_hex(6)}@example.com", "jelszó123456", None, 3)["id"]

    async def boom(*a, **k):
        raise RuntimeError("db hiccup")
    monkeypatch.setattr(credits, "_book", boom)
    with pytest.raises(RuntimeError):
        call(lambda: db.free_topup(u, "2099-02", 10))
    monkeypatch.undo()
    assert call(db._rows, "SELECT topup_month FROM users WHERE id = %s", (u,))[0]["topup_month"] is None   # nem „elhasználva”
    assert call(lambda: db.free_topup(u, "2099-02", 10)) == 7


def test_ai_estimate_is_behind_the_flag(db_client, monkeypatch, tmp_path):
    from app import providers
    monkeypatch.setattr(providers, "enabled_for", lambda uid=None: False)
    r = db_client.post("/api/ai/estimate", json={"action": "ai_clip", "prompt": "x"})
    assert r.status_code == 400 and "isn't switched on" in r.json()["detail"]
