"""„Ask for a change” egy kitölthető PDF-en: mezőértékek a kérésből — csomagokban, aláírás nélkül, a régi válasz törölhető.

UI-próba (10-18): a kérés a szerkesztő-ügynökhöz ment (Wordön át újraszedte: 266 mező eltűnt, 25 oldal → 42);
utána egyben kérte a 266 mezőt (4096 tokenes válasz → csonka), és a régi válaszok bent maradtak.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.tools import forms


def test_fill_from_request_batches_and_guards(monkeypatch, tmp_path):
    known = [{"name": f"mezo_{i}", "type": "text", "page": 1 + i // 50, "tooltip": f"Rovat {i}", "value": "régi"}
             for i in range(200)] + [{"name": "alairas", "type": "text", "page": 5, "tooltip": "Aláírás", "value": ""}]
    prompts: list[str] = []

    async def fake_fields(fid):
        return known

    async def fake_model(model, messages, thinking=False, json_mode=True):
        prompt = messages[0]["content"]
        prompts.append(prompt)
        names = [ln.split(" | ")[0] for ln in prompt.splitlines() if ln.startswith("mezo_") or ln.startswith("alairas")]
        vals = {n: ("Teszt Elek" if n == "mezo_0" else "") for n in names}
        vals["alairas"] = "Kitalált Aláíró"
        return json.dumps({"values": vals, "summary": "Kitöltve tesztadatokkal."})

    got: dict = {}

    async def fake_fill(src, vals, flatten, dst):
        got.update(vals)

    monkeypatch.setattr(forms, "fields", fake_fields)
    monkeypatch.setattr("app.llm.siliconflow._call_model", fake_model)
    monkeypatch.setattr(forms, "_fill_file", fake_fill)
    monkeypatch.setattr("app.tools.crafts.existing", lambda fid: Path("/dev/null"))
    res = asyncio.run(forms.fill_from_request("x.pdf", "töltsd ki tesztadatokkal", tmp_path / "o.pdf"))
    assert len(prompts) == 3                                     # 201 mező → 90-es csomagok
    assert "Teszt Elek" in prompts[1]                            # a második csomag látja az elsőt (összhang)
    assert got["mezo_0"] == "Teszt Elek" and got["mezo_150"] == "" and "alairas" not in got
    assert res["summary"] == "Kitöltve tesztadatokkal."


def test_whole_form_refill_clears_skipped_old_answers(monkeypatch, tmp_path):
    """UI-próba 10-18: a modell három mezőt kihagyott, azokban a régi gazda adata maradt („Csenger”, „10625 m2”)."""
    known = [{"name": f"m{i}", "type": "text", "page": 1, "tooltip": "", "value": "Csenger" if i < 3 else "régi"} for i in range(20)]

    async def fake_fields(fid):
        return known

    async def fake_model(model, messages, thinking=False, json_mode=True):
        return json.dumps({"values": {f"m{i}": ("Teszt" if i == 3 else "-----") for i in range(3, 20)}})

    got: dict = {}

    async def fake_fill(src, vals, flatten, dst):
        got.update(vals)

    monkeypatch.setattr(forms, "fields", fake_fields)
    monkeypatch.setattr("app.llm.siliconflow._call_model", fake_model)
    monkeypatch.setattr(forms, "_fill_file", fake_fill)
    monkeypatch.setattr("app.tools.crafts.existing", lambda fid: Path("/dev/null"))
    asyncio.run(forms.fill_from_request("x.pdf", "töltsd ki tesztadatokkal", tmp_path / "o.pdf"))
    assert got["m0"] == got["m1"] == got["m2"] == "-----" and got["m3"] == "Teszt"


def test_whole_form_refill_drops_echoed_old_answers(monkeypatch, tmp_path):
    """UI-próba 10-18: a modell három régi választ szó szerint visszaadott („Csenger”) — az sem maradhat,
    hacsak a felhasználó maga nem írta a kérésébe („tulajdonos”)."""
    known = [{"name": f"m{i}", "type": "text", "page": 1, "tooltip": "", "value": v}
             for i, v in enumerate(["Csenger", "tulajdonos"] + ["-----"] * 18)]

    async def fake_fields(fid):
        return known

    async def fake_model(model, messages, thinking=False, json_mode=True):
        return json.dumps({"values": {"m0": "Csenger", "m1": "tulajdonos", **{f"m{i}": "-----" for i in range(2, 20)}}})

    got: dict = {}

    async def fake_fill(src, vals, flatten, dst):
        got.update(vals)

    monkeypatch.setattr(forms, "fields", fake_fields)
    monkeypatch.setattr("app.llm.siliconflow._call_model", fake_model)
    monkeypatch.setattr(forms, "_fill_file", fake_fill)
    monkeypatch.setattr("app.tools.crafts.existing", lambda fid: Path("/dev/null"))
    asyncio.run(forms.fill_from_request("x.pdf", "töltsd ki tesztadatokkal: lakás, tulajdonos, 1/1", tmp_path / "o.pdf"))
    assert got["m0"] == "-----" and got["m1"] == "tulajdonos"


def test_coerce_does_not_mix_up_similar_names():
    """UI-próba 10-18: „telepules_nev_1_2” és „telepules_nev_12” normalizálva egyezett → az érték rossz mezőbe ment,
    a valódi mezőben a régi adat maradt."""
    known = [{"name": "telepules_nev_1_2", "type": "text"}, {"name": "telepules_nev_12", "type": "text"},
             {"name": "Vezetéknév", "type": "text"}]
    out, skipped = forms._coerce({"telepules_nev_1_2": "-----", "telepules_nev_12": "Budapest", "vezeteknev": "Teszt"}, known)
    assert out == {"telepules_nev_1_2": "-----", "telepules_nev_12": "Budapest", "Vezetéknév": "Teszt"} and not skipped
