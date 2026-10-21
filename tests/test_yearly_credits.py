"""Éves előfizetés (Kommandant 10-21: „a korrektséget fenn kell tartani”): a meghirdetett havi keret minden hónapban jár,
nem csak egyszer a számlával; hónaponként egyszer (a számla és a havi jóváírás ugyanaz a kulcs), fizetett kreditként; a
havi előfizetés a régi úton (számlánként); a lemondott előfizetés után nem jár több."""
from __future__ import annotations

import secrets

import pytest


def _event(kind: str, obj: dict) -> dict:
    return {"id": f"evt_{secrets.token_hex(6)}", "type": kind, "data": {"object": obj}}


def _subscribe(call, item: str, interval: str) -> tuple[str, str]:
    from app import billing, db
    uid = call(db.user_create, f"{secrets.token_hex(6)}@example.com", "jelszó123456", None, 0)["id"]
    sub = f"sub_{secrets.token_hex(5)}"
    assert call(billing.handle, _event("checkout.session.completed", {
        "id": f"cs_{secrets.token_hex(5)}", "client_reference_id": uid, "customer": f"cus_{uid}", "subscription": sub,
        "metadata": {"uid": uid, "item": item, "interval": interval}})) == f"plan {item}"
    return uid, sub


def _invoice(sub: str, interval: str) -> dict:
    return {"id": f"in_{secrets.token_hex(5)}", "subscription": sub,
            "lines": {"data": [{"price": {"recurring": {"interval": interval}}}]}}


def test_yearly_plan_gets_its_credits_every_month_once(db_client, monkeypatch):
    from app import billing, credits, db
    call = db_client.portal.call
    uid, sub = _subscribe(call, "pro", "year")
    assert call(db.user_get, uid)["billing_interval"] == "year"
    monkeypatch.setattr(billing, "month_key", lambda: "2026-10")
    assert call(billing.handle, _event("invoice.paid", _invoice(sub, "year"))) == "credits pro (yearly)"
    assert call(db.user_get, uid)["credits"] == 400
    assert call(lambda: billing.yearly_grant(uid)) == 0 and call(billing.yearly_grants) == 0       # ugyanabban a hónapban nem
    call(lambda: billing.monthly_topup(uid))
    assert call(db.user_get, uid)["credits"] == 400
    monkeypatch.setattr(billing, "month_key", lambda: "2026-11")                                   # a következő hónap
    assert call(billing.yearly_grants) >= 1 and call(db.user_get, uid)["credits"] == 800
    # fizetett kredit: az AI-videó foglalhat belőle
    assert call(lambda: credits.hold(uid, 800, "y1", operation="ai_clip", provider="kie"))["held"] == 800
    keys = [r["idempotency_key"] for r in call(db._rows, "SELECT idempotency_key FROM credit_ledger WHERE user_id = %s "
                                                        "AND kind = 'grant' ORDER BY id", (uid,))]
    assert keys == [f"stripe:yearly:{uid}:2026-10", f"stripe:yearly:{uid}:2026-11"]
    # lemondva (a ciklus végén törölve): nem jár több
    assert call(billing.handle, _event("customer.subscription.deleted", {"id": sub, "customer": f"cus_{uid}"})) == "downgraded"
    monkeypatch.setattr(billing, "month_key", lambda: "2026-12")
    call(billing.yearly_grants)
    assert call(lambda: billing.yearly_grant(uid)) == 0 and call(db.user_get, uid)["credits"] == 0


def test_monthly_plan_still_gets_its_credits_per_invoice(db_client, monkeypatch):
    from app import billing, db
    call = db_client.portal.call
    uid, sub = _subscribe(call, "plus", "month")
    monkeypatch.setattr(billing, "month_key", lambda: "2026-10")
    assert call(billing.handle, _event("invoice.paid", _invoice(sub, "month"))) == "credits plus"
    assert call(db.user_get, uid)["credits"] == 150
    monkeypatch.setattr(billing, "month_key", lambda: "2026-11")
    call(billing.yearly_grants)
    assert call(lambda: billing.yearly_grant(uid)) == 0 and call(db.user_get, uid)["credits"] == 150    # a számla hozza
    assert call(billing.handle, _event("invoice.paid", _invoice(sub, "month"))) == "credits plus"
    assert call(db.user_get, uid)["credits"] == 300


@pytest.mark.parametrize("obj, want", [
    ({"lines": {"data": [{"price": {"recurring": {"interval": "year"}}}]}}, "year"),
    ({"lines": {"data": [{"plan": {"interval": "month"}}]}}, "month"),
    ({}, None),
])
def test_invoice_interval(obj, want):
    from app import billing
    assert billing._invoice_interval(obj) == want
