"""A forgatókönyv a feliratokban és szünet a narrációban (10-22, a MoneyPrinterTurbo ötletlistájából): ha a kimondott
szöveg ismert, a felirat SZAVAI abból jönnek, csak az IDEJÜK a hallásból; a [pause: 1.5s] jelnél a hang hallgat, a
képernyőn a jel nem látszik. A beszédfelismerő itt utánzat."""
from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import pytest

from test_ai_clip import _as, _job, _put_as, _user
from test_avatar import _face, prov  # noqa: F401  (fixture)

SCRIPT = ("Jó reggelt, itt a Tamás TV reggeli különkiadása. Ma a Sárkány és a Hiéna lesz a téma – meg a hét "
          "legfontosabb hírei. Köszönöm, hogy velünk tartott!")
HEARD = [("Jó", 0.0, 0.3), ("reggelt,", 0.3, 0.7), ("itt", 0.8, 0.9), ("a", 0.9, 1.0), ("Tomás", 1.0, 1.4), ("tévé", 1.4, 1.8),
         ("reggeli", 1.9, 2.3), ("különkiadása.", 2.3, 3.0), ("öö", 3.1, 3.3), ("Ma", 3.5, 3.7), ("a", 3.7, 3.8),
         ("sárkány", 3.8, 4.3), ("és", 4.3, 4.4), ("a", 4.4, 4.5), ("hiéna", 4.5, 5.0), ("lesz", 5.0, 5.2), ("a", 5.2, 5.3),
         ("téma.", 5.3, 5.8)]


def _tr(words=HEARD) -> dict:
    return {"language": "hu", "model": "large-v3-turbo", "duration": 6.0, "sentences": [],
            "words": [{"w": w, "start": a, "end": b} for w, a, b in words]}


def test_the_script_gives_the_words_and_the_listening_the_times():
    from app.tools import transcript
    r = transcript.align_script(_tr(), SCRIPT)
    assert [s["text"] for s in r["sentences"]] == ["Jó reggelt, itt a Tamás TV reggeli különkiadása.",
                                                   "Ma a Sárkány és a Hiéna lesz a téma"]
    st = r["script"]
    assert (st["heard_as_written"], st["corrected"], st["dropped"], st["not_said"]) == (15, 2, 1, 10)   # a vége nem hangzott el
    fixes = {c["heard"]: c for c in r["corrections"]}
    assert fixes["Tomás tévé"]["fixed"] == "Tamás TV" and fixes["Tomás tévé"]["at"] == 1.0 and fixes["öö"]["fixed"] == ""
    w = {x["w"]: x for x in r["words"]}
    assert w["Tamás"]["start"] == 1.0 and w["TV"]["end"] == 1.8 and w["Sárkány"]["start"] == 3.8      # a hallott idők
    cues = transcript.cues(r)
    assert all(len(c["words"]) == len(c["text"].split()) for c in cues)       # szavankénti felirathoz is jó


def test_unscripted_speech_stays_short_gaps_are_filled_and_a_wrong_script_is_refused():
    from app.tools import transcript
    ad_lib = HEARD[:8] + [("és", 3.1, 3.2), ("ezt", 3.2, 3.3), ("most", 3.3, 3.4), ("nagyon", 3.4, 3.5), ("komolyan", 3.5, 3.6),
                          ("mondom.", 3.6, 3.7)] + [(w, a + 0.5, b + 0.5) for w, a, b in HEARD[9:]]
    r = transcript.align_script(_tr(ad_lib), SCRIPT)
    assert r["script"]["kept_heard"] == 6 and "ezt most nagyon komolyan mondom." in " ".join(s["text"] for s in r["sentences"])
    gap = [x for x in HEARD if x[0] != "lesz"]                                  # egy szót nem hallott a gép
    r2 = transcript.align_script(_tr(gap), SCRIPT)
    g = next(x for x in r2["words"] if x["w"] == "lesz")
    assert r2["script"]["added"] == 1 and 5.0 <= g["start"] <= g["end"] <= 5.2
    tagged = transcript.align_script(_tr(), SCRIPT.replace("téma –", "téma [pause: 1s] 🎉 –"))
    assert tagged["script"]["words"] == 27 and "pause" not in " ".join(x["w"] for x in tagged["words"])
    assert transcript.align_script(_tr(), "Egészen más szöveg, amit itt senki sem mond el soha, semmilyen formában.") is None


async def test_transcribe_with_a_script_and_with_a_script_that_doesnt_fit(monkeypatch, tmp_path):
    import os
    from app.tools import transcript
    monkeypatch.setattr(transcript, "available", lambda: True)
    pcm = os.urandom(64000)

    async def pcm16k(src):
        return pcm
    monkeypatch.setattr(transcript, "_pcm16k", pcm16k)
    monkeypatch.setattr(transcript, "_run", lambda *a, **k: _tr())
    src = tmp_path / "hang.wav"
    src.write_bytes(b"x")
    out = await transcript.transcribe(src, "hu", script=SCRIPT)
    assert out["script"]["corrected"] == 2 and out["sentences"][0]["text"].startswith("Jó reggelt, itt a Tamás TV")
    off = await transcript.transcribe(src, "hu", script="Ez egy teljesen másik felvétel szövege, semmi köze ehhez.")
    assert "doesn't match" in off["correction_notes"][0] and off["words"][4]["w"] == "Tomás"


def test_auto_subtitles_follow_the_script_and_the_file_remembers_it(monkeypatch, tmp_path, put_file):
    from app.storage import write_meta
    from app.tools import transcript, video
    assert video.transcript_opts({"script": SCRIPT, "nope": 1}) == {"script": SCRIPT}
    got = []

    async def fake(path, language=None, **kw):
        got.append(kw)
        return transcript.align_script(_tr(), kw["script"]) if kw.get("script") else _tr()
    monkeypatch.setattr(transcript, "transcribe", fake)
    src = tmp_path / "v.mp4"
    src.write_bytes(b"x")
    fid = put_file(src)
    notes: list[str] = []
    subs = asyncio.run(video._auto_subtitles(fid, "hu", lambda *a, **k: None, {"script": SCRIPT}, notes))
    assert subs[0]["text"].startswith("Jó reggelt, itt a Tamás TV") and "Hiéna" in subs[-1]["text"]
    assert notes[0].startswith("The captions follow the script: 15 of its 27 words heard as written, 2 corrected")
    assert video.with_script(fid, {}) == {}
    write_meta(fid, extra={"script": SCRIPT})
    assert video.with_script(fid, {"prompt": "p"}) == {"prompt": "p", "script": SCRIPT}
    assert video.with_script(fid, {"script": "más"}) == {"script": "más"}               # a kliensé nyer


def test_a_talking_avatar_from_text_remembers_what_it_said(db_client, prov, tmp_path, monkeypatch):  # noqa: F811
    from app.storage import read_meta
    from app.tools import ai_video, narration, video
    call = db_client.portal.call

    def synth(text, key, dst, speed=1.0):
        Path(dst).write_bytes(b"")
        with wave.open(str(dst), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
            w.writeframes(b"\0" * 32000 * 3)
        return 3.0
    monkeypatch.setattr(narration, "synth", synth)
    monkeypatch.setattr(narration, "pick", lambda lang, want=None: "hu-x")
    uid, _ = _user(call, 100)
    face = _put_as(uid, _face(tmp_path / "face.png"), "png")
    text = "Jó reggelt, itt a Tamás TV. [pause: 1s] Ma a Sárkány és a Hiéna a téma."
    _, j = _job(call, prov, uid, lambda: ai_video.start_avatar({"image": face, "text": text, "consent": True}))
    assert j["status"] == "done", j.get("error")
    fid = j["result"]["file_id"]

    async def meta():
        return read_meta(fid), video.with_script(fid, {})
    m, topts = call(_as(uid, meta))
    assert m["script"] == text and topts == {"script": text}


def test_pause_marks_are_parsed_and_never_shown():
    from app.tools import narration, video
    assert narration.pauses("Egy. [pause: 1.5s] Kettő [pause 500ms] három [szünet: 2 mp] [PAUSE: 60s]") == [
        ("Egy. ", 1.5), (" Kettő ", 0.5), (" három ", 2.0), (" ", 10.0), ("", 0.0)]
    assert narration.strip_pauses("Egy. [pause: 1.5s] Kettő") == "Egy. Kettő" and narration.strip_pauses("[nem szünet]") == "[nem szünet]"
    story = {"title": "T", "scenes": [{"id": "s1", "headline": "H", "caption": "Első mondat. [pause: 2s] Második mondat."}]}
    D = video.compose(story, fmt="landscape", total=20.0, images=[None, None], accent=None, credits="", voice=None,
                      voice_s=None, music=None)
    assert " ".join(c["text"] for c in D["captions"]) == "Első mondat. Második mondat."


def test_the_voice_stops_at_a_pause(tmp_path, monkeypatch):
    pytest.importorskip("piper")
    from app.tools import narration
    said = []

    class Voice:
        def synthesize_wav(self, text, wf, syn_config=None):
            said.append(text)
            wf.setnchannels(1), wf.setsampwidth(2), wf.setframerate(16000)
            wf.writeframes(b"\1\0" * 8000)                     # 0,5 s
    monkeypatch.setattr(narration, "_voice", lambda key: Voice())
    secs = narration.synth("[pause: 1s] Egy. [pause: 1.5s] Kettő. [pause: 3s]", "x", tmp_path / "o.wav")
    assert said == ["Egy.", "Kettő."] and abs(secs - 3.5) < 0.01              # elöl 1 s, közte 1,5 s, a végén nincs
    with wave.open(str(tmp_path / "o.wav")) as w:
        frames = w.readframes(w.getnframes())
    assert frames[:32000] == b"\0" * 32000 and frames[32000:32002] == b"\1\0"
