"""Mondathoz kötött réteg a saját felvételen (a ChatGPT-értékelés 10-17: „ne nekem kelljen másodpercre megadnom”).
Az átirat-modell nélkül: egy valódi (large-v3-turbo) átirat szavaival — a modell félrehallását is bele."""
from __future__ import annotations

import pytest

TR = {"language": "hu", "model": "large-v3-turbo", "duration": 14.0,
      "sentences": [{"start": 0.0, "end": 3.14, "text": "A bíróság első fokon elutasította a keresetet."},
                    {"start": 3.96, "end": 8.38, "text": "Az ügyfér felletbez és a másodfokú bíróság 6 hónapon belül dönt."}],
      "words": [{"w": w, "start": a, "end": b} for w, a, b in [
          ("A", 0.0, 0.2), ("bíróság", 0.2, 0.8), ("első", 0.8, 1.2), ("fokon", 1.2, 1.6), ("elutasította", 1.6, 2.4),
          ("a", 2.4, 2.6), ("keresetet.", 2.6, 3.1), ("Az", 3.96, 4.3), ("ügyfér", 4.3, 4.7), ("felletbez", 4.7, 5.3),
          ("és", 5.3, 5.6), ("a", 5.6, 5.7), ("másodfokú", 5.7, 6.4), ("bíróság", 6.4, 6.9)]]}


def test_find_tolerates_accents_punctuation_and_mishearing():
    from app.tools import transcript
    hit = transcript.find(TR, "az ügyfél fellebbez")
    assert hit and hit["start"] == 3.96 and hit["sentence_end"] == 8.38 and hit["score"] >= 0.72
    assert transcript.find(TR, "A BÍRÓSÁG, első fokon")["start"] == 0.0
    assert transcript.find(TR, "a végrehajtó lefoglalja a házat") is None
    assert transcript.find(TR, "bíróság", after=3.0)["start"] == 6.4                 # a későbbi előfordulás


def test_srt_is_well_formed():
    from app.tools import transcript
    out = transcript.srt(TR)
    assert out.startswith("1\n00:00:00,000 --> 00:00:03,140\nA bíróság") and "\n2\n00:00:03,960 --> 00:00:08,380\n" in out


async def test_layers_tied_to_what_is_said(monkeypatch, tmp_path):
    from app.tools import transcript, video

    async def fake(path, language=None):
        return TR
    monkeypatch.setattr(transcript, "transcribe", fake)
    notes: list[str] = []
    raw = [{"at": {"say": "az ügyfél fellebbez"}, "type": "lower_third", "name": "Fellebbezés"},
           {"at": 4.5, "until": {"say": "másodfokú bíróság"}, "type": "source", "text": "x"},
           {"at": {"say": "a végrehajtó lefoglalja a házat"}, "type": "source", "text": "y"}]
    out = await video.said_to_times(raw, tmp_path / "f.mp4", notes, new_ids=True)
    assert out[0]["at"] == pytest.approx(3.81) and out[0]["dur"] == pytest.approx(5.22)   # a mondat végéig (+0,8 s)
    assert len(out) == 2 and out[1]["until"] == pytest.approx(7.2)                      # a nem elhangzó kimarad…
    assert any(n.startswith("⚠ L3: “a végrehajtó") and "closest" in n for n in notes)  # …és a check kimondja
    assert any("L1 starts when “az ügyfél fellebbez” is said" in n and "heard as" in n for n in notes)
    video.layers_from(video._unsaid(raw), "", [])                                     # a szerkezeti előellenőrzés átengedi
