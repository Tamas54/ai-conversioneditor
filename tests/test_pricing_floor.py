"""Veszteség soha (Kommandant 10-21): minden külső AI-ár (kie / fal) a legolcsóbban eladott krediten is legalább a
szolgáltatói költség MARKUP-szorosa — az induló szabályok, az AI-képek listaára, egy padló alá állított régi sor és az
admin mentése is. Az AI-videó csak fizetett kreditből foglalhat; az AI-kép a könyvben a tényleges kie-költséggel."""
from __future__ import annotations

import asyncio
import secrets

import httpx
import pytest


def _cost_eur(provider: str, units: float) -> float:
    from app import credits
    return units * credits.USD_PER_UNIT[provider] * credits.USD_EUR


def test_floor_is_the_worst_credit_and_follows_the_plans(monkeypatch):
    from app import billing, credits
    # Pro éves: €190 bruttó, 27% ÁFA, a legdrágább kártya 3,25% + €0,25, 12 × 400 kredit
    assert credits.credit_floor_eur() == pytest.approx((190 / 1.27 - (190 * 0.0325 + 0.25)) / 4800)
    assert credits.floor_multiplier("kie") == pytest.approx(0.005 * 0.95 * 2 / credits.credit_floor_eur())
    assert credits.floor_multiplier("fal") == pytest.approx(200 * credits.floor_multiplier("kie"))
    assert credits.floor_multiplier(None) == 0 and credits.floor_multiplier("siliconflow") == 0
    cheaper = {**billing.PLANS, "pro": {**billing.PLANS["pro"], "year": 150}}
    before = credits.floor_multiplier("kie")
    monkeypatch.setattr(billing, "PLANS", cheaper)          # olcsóbb csomag → drágább AI-ár, magától
    assert credits.floor_multiplier("kie") > before


@pytest.mark.parametrize("units", [1, 2, 5, 7.5, 13, 30, 1438])
def test_every_external_price_covers_twice_its_cost_on_the_cheapest_credit(units):
    from app import credits
    from app.providers import kie
    floor = credits.credit_floor_eur()
    for r in credits.default_rules():
        assert credits.price(r, units) * floor >= credits.MARKUP * _cost_eur(r["provider"], r["provider_cost"] * units) - 1e-9, r
    for _, _, _, cost in kie.IMAGE_PRICES:
        assert credits.list_price("kie", cost) * floor >= credits.MARKUP * _cost_eur("kie", cost) - 1e-9


def test_the_prices_people_see():
    """A mostani csomagárakkal (Pro éves = a padló): 5 s Wan 3.0 720p 26, 1080p 51; kép 2 / 3, Kontext 2."""
    from app.tools import ai_image, ai_video
    wan, fast = ai_video.clip_prices(5)
    assert wan["engine"] == "wan-3" and wan["default"] and wan["credits"] == {"480p": 13, "720p": 26, "1080p": 51}
    assert fast["engine"] == "wan-3-fast" and fast["credits"] == {"480p": 20, "720p": 41, "1080p": 81}
    assert ai_image.gen_price("1024x1024") == 2 and ai_image.gen_price("1792x1024") == 3 == ai_image.picture_price()
    assert ai_image.edit_price() == 2


def test_a_losing_row_is_lifted_retired_models_are_gone_and_admin_cant_save_a_loss(db_client):
    from app import credits, db
    call = db_client.portal.call
    call(db._rows, "DELETE FROM pricing_rules RETURNING id")
    call(db._rows, """INSERT INTO pricing_rules (operation, model, tier, unit, provider, provider_cost, multiplier, min_charge)
                      VALUES ('ai_clip', 'wan/3-0-video', '720p', 'second', 'kie', 16, 0.15, 1),
                             ('ai_clip', 'bytedance/seedance-2-5', '720p', 'second', 'kie', 63, 0.15, 1),
                             ('enhance', 'topaz/video-upscale', '4x', 'second', 'kie', 14, 9, 1) RETURNING id""")
    call(credits.seed_rules)
    rows = {(r["model"], r["tier"]): r for r in call(db._rows, "SELECT * FROM pricing_rules")}
    fl = credits.floor_multiplier("kie")
    assert float(rows[("wan/3-0-video", "720p")]["multiplier"]) == pytest.approx(fl)        # felemelve a padlóra
    assert float(rows[("topaz/video-upscale", "4x")]["multiplier"]) == 9                       # az admin emelése marad
    assert rows[("bytedance/seedance-2-5", "720p")]["deleted_at"] is not None                  # a Seedance kikerült
    assert call(credits.rule_for, "ai_clip", "bytedance/seedance-2-5", "720p") is None
    with pytest.raises(ValueError, match="at least"):
        call(credits.rule_save, {"operation": "ai_clip", "model": "wan/3-0-video", "tier": "1080p", "unit": "second",
                                 "provider": "kie", "provider_cost": 32, "multiplier": 0.15})
    # egy kézzel (SQL-ből) a padló alá írt sor sem árazhat veszteségbe: a price() a padlóra emel
    forged = {"provider": "kie", "provider_cost": 16, "multiplier": 0.15, "min_charge": 1}
    assert credits.price(forged, 5) == credits.list_price("kie", 16, 5) == 26


def _user(call, n: int) -> str:
    from app import db
    return call(db.user_create, f"{secrets.token_hex(6)}@example.com", "jelszó123456", None, n)["id"]


def test_ai_video_runs_on_paid_credits_only(db_client):
    from app import credits, db
    call = db_client.portal.call
    uid = _user(call, 30)                                 # 30 kezdő kredit: van egyenleg, de nincs mögötte bevétel
    with pytest.raises(credits.PaidCreditsOnly) as e:
        call(lambda: credits.hold(uid, 26, "op-a", operation="ai_clip", provider="kie"))
    assert e.value.needed == 26 and e.value.available == 0 and "paid credits" in str(e.value)
    assert call(db.user_get, uid)["credits"] == 30        # semmi nem mozdult
    # az AI-kép ingyenes kreditből is mehet
    assert call(lambda: credits.hold(uid, 3, "op-img", operation="ai_image", provider="kie"))["held"] == 3
    call(lambda: credits.settle(uid, "op-img", 3, 3, operation="ai_image", provider="kie", provider_cost=7))
    # kreditcsomag (Stripe) → 100 fizetett kredit; ebből foglalhat, többet nem
    call(lambda: db.credits_add(uid, 100, "bought pack100", ref="cs_x", kind="purchase", idempotency_key=f"stripe:cs_{uid}"))
    assert call(lambda: credits.hold(uid, 75, "op-b", operation="ai_clip", provider="kie"))["held"] == 75
    with pytest.raises(credits.PaidCreditsOnly) as e:
        call(lambda: credits.hold(uid, 26, "op-c", operation="enhance", provider="fal"))
    assert e.value.available == 25
    call(lambda: credits.settle(uid, "op-b", 75, 50, operation="ai_clip", provider="kie", provider_cost=150))   # 25 vissza
    assert call(lambda: credits.hold(uid, 50, "op-d", operation="enhance", provider="fal"))["held"] == 50
    # az előfizetés havi kreditje (invoice.paid, stripe:-kulcs) is fizetett
    call(lambda: db.credits_add(uid, 400, "pro monthly credits", ref="in_x", kind="grant", idempotency_key=f"stripe:in_{uid}"))
    assert call(lambda: credits.hold(uid, 400, "op-e", operation="ai_clip", provider="kie"))["held"] == 400


class _FakeKie:
    """A kie feladat-API-ja hálózat nélkül: create → status (siker / hiba) → a kép letöltése."""

    def __init__(self, ok=True, cost=7.0):
        self.ok, self.cost, self.created = ok, cost, []

    async def create(self, model, inp, **kw):
        from app.providers.kie import map_input
        self.created.append((model, map_input(model, inp)))
        return f"t{len(self.created)}"

    async def status(self, tid):
        from app.providers.base import FAIL, SUCCESS, TaskState
        if self.ok:
            return TaskState(task_id=tid, state=SUCCESS, urls=["https://tempfile.example/x.png"], cost=self.cost)
        return TaskState(task_id=tid, state=FAIL, error="nsfw", cost=0.0)

    async def upload(self, path):
        return "https://kie.example/up.png"


def _fake_kie(monkeypatch, fake):
    from app.tools import ai_image
    monkeypatch.setattr(ai_image, "_kie", lambda: fake)
    monkeypatch.setattr(ai_image, "POLL_S", 0)
    real = httpx.AsyncClient
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    monkeypatch.setattr(ai_image.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=png)), **kw))


def test_ai_image_on_kie_charges_the_list_price_and_books_the_real_cost(db_client, monkeypatch):
    from app import db, tenancy
    from app.tools import vision_ops
    call = db_client.portal.call
    uid = _user(call, 30)
    fake = _FakeKie(cost=7.0)
    _fake_kie(monkeypatch, fake)

    async def gen(size):
        tok = tenancy.set_user(uid)
        try:
            return await vision_ops.generate_image("a lighthouse at dusk", size=size)
        finally:
            tenancy.reset(tok)
    out = call(gen, "1792x1024")
    assert out["model"] == "FLUX.2-pro" and call(db.user_get, uid)["credits"] == 27          # 2K: 3 kredit
    model, inp = fake.created[-1]
    assert model == "flux-2/pro-text-to-image" and inp == {"prompt": "a lighthouse at dusk", "aspect_ratio": "16:9",
                                                           "resolution": "2K", "nsfw_checker": True}
    cap = call(db._rows, "SELECT * FROM credit_ledger WHERE user_id = %s AND kind = 'capture'", (uid,))
    assert [(r["delta"], r["provider"], float(r["provider_cost"]), r["operation"]) for r in cap] == [(-3, "kie", 7.0, "ai_image")]
    call(gen, "1024x1024")
    assert fake.created[-1][1]["resolution"] == "1K" and fake.created[-1][1]["aspect_ratio"] == "1:1"
    assert call(db.user_get, uid)["credits"] == 25
    fake.ok = False                                        # hiba: semmi nem vonódik le, a könyvben a 0 költség
    with pytest.raises(RuntimeError, match="failed"):
        call(gen, "1024x1024")
    assert call(db.user_get, uid)["credits"] == 25


def test_kontext_edit_goes_to_kie_with_the_original_ratio(monkeypatch, tmp_path):
    from PIL import Image
    from app.tools import ai_image
    fake = _FakeKie()
    _fake_kie(monkeypatch, fake)
    src = tmp_path / "old.jpg"
    Image.new("RGB", (900, 600), "grey").save(src)
    bill = ai_image.Bill()
    out = asyncio.run(ai_image.kontext(src, "Restore this photo", bill))
    assert out.startswith(b"\x89PNG") and bill.cost == 7.0 and bill.tasks == ["t1"]
    assert fake.created == [("flux1-kontext", {"prompt": "Restore this photo", "input_image": "https://kie.example/up.png",
                                               "model": "flux-kontext-pro", "output_format": "png"})]   # nincs aspect_ratio
