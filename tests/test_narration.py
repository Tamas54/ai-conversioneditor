"""AI-narráció a videókhoz: nyelvfelismerés, hangválasztás, a jelenetek a narrációhoz igazodnak, a sáv összeáll."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import wave
from pathlib import Path

import pytest


def test_language_detection_and_names():
    from app.tools import narration as n
    assert n.detect("Az Integritás Hatóság 2022-ben jött létre, hogy őrködjön az uniós pénzek felett.") == "hu"
    assert n.detect("The authority was created in 2022 to guard the fair use of EU funds.") == "en"
    assert n.detect("Die Behörde wurde 2022 gegründet und ist nicht weisungsgebunden.") == "de"
    assert n.detect("La autoridad se creó en 2022 para vigilar el uso de los fondos de la UE.") == "es"
    assert n.detect("Привіт, як справи? Це українська мова.") == "uk"
    assert n.detect("Привет, как дела? Это русский язык.") == "ru"
    assert n.lang_code("Hungarian") == n.lang_code("magyar") == n.lang_code("hu-HU") == "hu"
    assert n.lang_code("auto") is None and n.lang_code(None) is None
    assert n.clean("Nézd meg: https://example.com 🎉 **most** ő ű") == "Nézd meg: most ő ű"


def test_voice_pick_curated_gender_and_fallback(monkeypatch):
    from app.tools import narration as n
    cat = {k: {"language": {"family": k[:2]}, "quality": "medium", "num_speakers": 1}
           for k in ("hu_HU-anna-medium", "hu_HU-imre-medium", "en_US-lessac-medium", "en_US-ryan-medium",
                     "fi_FI-harri-medium")}
    cat["fi_FI-harri-low"] = {"language": {"family": "fi"}, "quality": "low", "num_speakers": 1}
    monkeypatch.setattr(n, "_catalog", lambda: cat)
    assert n.pick("hu") == "hu_HU-anna-medium"
    assert n.pick("hu", "male") == "hu_HU-imre-medium"
    assert n.pick("en", "female") == "en_US-lessac-medium"
    assert n.pick("hu", "en_US-ryan-medium") == "en_US-ryan-medium"        # konkrét hang mindig nyer
    assert n.pick("fi") == "fi_FI-harri-medium"                           # nem kurált nyelv: a legjobb minőség
    with pytest.raises(ValueError):
        n.pick("xx")


def test_scenes_follow_the_narration_and_parts_land_on_their_scene(tmp_path):
    from app.tools import video
    story = {"title": "Integritás Hatóság", "kicker": "", "scenes": [
        {"id": "s1", "headline": "Egy", "caption": "Rövid."},
        {"id": "s2", "headline": "Kettő", "caption": "Hosszabb szöveg.", "duration": 3.0}],
        "end": {"title": "", "subtitle": ""}}
    wavs = [tmp_path / f"{i}.wav" for i in range(3)]
    clips = [(wavs[0], 1.2), (wavs[1], 9.0), (wavs[2], 6.5)]
    fitted = video.fit_to_narration(story, clips)
    assert fitted["scenes"][0]["duration"] == 10.4 and fitted["scenes"][1]["duration"] == 7.9
    assert "duration" not in story["scenes"][0]                          # az eredeti forgatókönyv nem változik
    D = video.compose(fitted, fmt="landscape", total=60.0, images=[None, None, None], accent=None, credits="",
                      voice=None, voice_s=None, music=None)
    tl = {e["id"]: e for e in D["timeline"]}
    assert tl["s1"]["end"] - tl["s1"]["start"] >= 10.4 - 0.01 and tl["s2"]["end"] - tl["s2"]["start"] >= 7.9 - 0.01
    segs = video.narration_segments(D, clips)
    assert segs[0] == (0.35, wavs[0])                                    # a cím a címkártyán
    assert [round(t, 2) for t, _ in segs[1:]] == [round(tl["s1"]["start"] + 0.45, 2), round(tl["s2"]["start"] + 0.45, 2)]
    assert segs[1][0] + 9.0 < tl["s1"]["end"] and segs[2][0] + 6.5 < tl["s2"]["end"]   # a beszéd a saját jelenetében marad
    long_title = [(wavs[0], 5.0)] + clips[1:]
    assert all(p != wavs[0] for _, p in video.narration_segments(D, long_title))      # nem fér a címkártyára: kimarad


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg needed")
def test_narration_track_is_one_stereo_wav_of_the_video_length(tmp_path):
    from app.tools import narration
    parts = []
    for i, f in enumerate((440, 660)):
        p = tmp_path / f"p{i}.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=f={f}:sample_rate=22050",
                        "-t", "1", "-ac", "1", str(p)], check=True)
        parts.append(p)
    out = asyncio.run(narration.track([(0.5, parts[0]), (3.0, parts[1])], 6.0, tmp_path / "n.wav"))
    with wave.open(str(out)) as w:
        assert w.getnchannels() == 2 and w.getframerate() == 48000
        assert abs(w.getnframes() / 48000 - 6.0) < 0.05


_MODEL = Path(os.getenv("PIPER_VOICES_DIR") or "/nonexistent") / "hu_HU-anna-medium.onnx"


@pytest.mark.skipif(not _MODEL.exists(), reason="a Piper-hangmodell nincs letöltve (PIPER_VOICES_DIR)")
def test_real_hungarian_voice(tmp_path):
    from app.tools import narration
    clips = asyncio.run(narration.speak_all(["Árvíztűrő tükörfúrógép.", "", "Az Integritás Hatóság 2022-ben jött létre."],
                                            "hu_HU-anna-medium", tmp_path))
    assert clips[0][0] and 1.0 < clips[0][1] < 4.0
    assert clips[1] == (None, 0.0)
    assert clips[2][1] > clips[0][1]
