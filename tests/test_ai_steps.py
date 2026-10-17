"""MCP-n a szerver SAJÁT modellhívása kredites (Kommandant 10-17: „ez is kredites legyen”): havi AI-lépés keret, felette
20 lépésenként 1 kredit, előre; webes használatban és a már fizető műveletben (szkennelt oldal) nem számol; elfogyott
keretnél érthető üzenet, mit csinálhat a kliens maga; az MCP-eredmény `studio_ai` mezője megmondja, mennyi ment el."""
from __future__ import annotations

import pytest


@pytest.fixture()
def fake_db(monkeypatch):
    """Egy free fiók (60 lépés/hó) a db helyett: használat-számláló és kreditegyenleg."""
    from app import billing, db, tenancy
    state = {"usage": 0, "credits": 1}

    async def usage_get(uid, month, kind):
        return state["usage"] if kind == "ai_steps" else 0

    async def usage_add(uid, month, kind, n, limit):
        if kind == "ai_steps":
            state["usage"] += n
        return state["usage"]

    async def credits_spend(uid, amount, reason, ref=None):
        if state["credits"] < amount:
            raise db.InsufficientCredits("no")
        state["credits"] -= amount
        return state["credits"]

    async def plan_of(uid):
        return "free"
    monkeypatch.setattr(db, "enabled", lambda: True)
    monkeypatch.setattr(db, "usage_get", usage_get)
    monkeypatch.setattr(db, "usage_add", usage_add)
    monkeypatch.setattr(db, "credits_spend", credits_spend)
    monkeypatch.setattr(billing, "plan_of", plan_of)
    tok = tenancy.set_user("abc123def")
    yield state
    tenancy.reset(tok)


async def test_web_use_and_prepaid_work_do_not_count(fake_db):
    from app import billing
    assert await billing.ai_step() == 0 and fake_db["usage"] == 0          # nem MCP: a csomagkeret a fair use
    from app import tenancy
    tok = tenancy.client_makes_images.set(True)
    try:
        with billing.prepaid():
            assert await billing.ai_step() == 0 and fake_db["usage"] == 0  # pl. szkennelt oldal: már fizetett
    finally:
        tenancy.client_makes_images.reset(tok)


async def test_allowance_then_credits_then_a_clear_way_out(fake_db):
    from app import billing, tenancy
    tok = tenancy.client_makes_images.set(True)
    try:
        with billing.count_steps() as used:
            for _ in range(60):
                assert await billing.ai_step() == 0                       # a havi 60 lépés ingyen
            assert await billing.ai_step() == 1 and fake_db["credits"] == 0  # a 61. egy 20-as blokkot nyit: 1 kredit
            for _ in range(19):
                assert await billing.ai_step() == 0                       # a blokk többi lépése már kifizetve
            with pytest.raises(billing.AIStepsExhausted, match="do that part yourself"):
                await billing.ai_step()                                   # a 81.: nincs kredit — érthető kiút
        assert used[0] == 81 and used[1] == 1
    finally:
        tenancy.client_makes_images.reset(tok)


async def test_mcp_result_says_what_was_used(fake_db):
    from app import billing
    from app.mcp_server import linked

    @linked
    async def files(**k):                                                # egy MCP-eszköz, ami a szerver modelljét hívja
        await billing.ai_step()
        await billing.ai_step()
        return {"ok": True}
    res = await files()
    assert res["studio_ai"]["steps"] == 2 and res["studio_ai"]["credits"] == 0


async def test_scene_plan_over_mcp_does_not_ask_the_server_model_for_image_words(monkeypatch):
    """Kész jelenetterv képkereső szavak nélkül: MCP-n NEM fut a szerver modellje — a kliens kap egy tanácsot."""
    from app import tenancy
    from app.tools import video
    called = []

    async def queries(scenes):
        called.append(scenes)
        return ["x"] * len(scenes)

    async def assemble(story, specs, opts, **kw):
        return {"findings": kw.get("pre_findings"), "specs": specs}

    async def choose(jobs):
        return {}
    monkeypatch.setattr(video, "_queries_for", queries)
    monkeypatch.setattr(video, "_assemble", assemble)
    monkeypatch.setattr(video, "_choose_stock", choose)
    tok = tenancy.client_makes_images.set(True)
    try:
        out = await video.make({"mode": "text", "format": "landscape", "narration": "none",
                                "scenes": [{"headline": "Szeged", "caption": "A város"}]})
    finally:
        tenancy.client_makes_images.reset(tok)
    assert not called and any("image_query" in f for f in out["findings"])
