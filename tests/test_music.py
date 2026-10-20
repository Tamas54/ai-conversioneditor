"""Háttérzene: a CC0 zenetár (lista + ellenőrzött fájlok), hangulat szerinti, determinisztikus választás, a videó
hosszára igazított alap, és halkítás a beszéd alatt (a ChatGPT-értékelés 10-17: „háttérzene, beszéd alatt
automatikusan lehalkulva”)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _library_on(monkeypatch):
    monkeypatch.setenv("MUSIC_LIBRARY", "1")          # a tár alapból KI (a „korrekt” elv) — ezek a tesztek a bekapcsolt tárról szólnak


def test_library_is_cc0_and_pinned():
    lib = json.loads((Path(__file__).resolve().parents[1] / "app/tools/music_library.json").read_text("utf-8"))
    assert lib["license"]["spdx"] == "CC0-1.0"
    assert re.fullmatch(r"[0-9a-f]{40}", lib["source"]["commit"]) and lib["source"]["commit"] in lib["source"]["base_url"]
    assert len(lib["tracks"]) == 24 and all(re.fullmatch(r"[0-9a-f]{64}", t["sha256"]) for t in lib["tracks"])
    assert {t["mood"] for t in lib["tracks"]} == {"upbeat", "calm", "cinematic", "lofi", "corporate", "playful",
                                                   "romantic", "dramatic"}


def _installed():
    from app.tools import music
    if not music.tracks():
        pytest.skip("music library not installed (scripts/fetch_music.py)")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")


def _mean_db(f: Path, start: float, dur: float) -> float:
    err = subprocess.run(["ffmpeg", "-v", "info", "-ss", str(start), "-t", str(dur), "-i", str(f), "-af", "volumedetect",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    return float(re.search(r"mean_volume: (-?[\d.]+) dB", err).group(1))


def test_pick_is_deterministic_and_errors_say_what_to_do():
    from app.tools import music
    _installed()
    assert music.pick("calm", "Kedves Beáta") == music.pick("calm", "Kedves Beáta")
    assert music.pick("still-water")["id"] == "still-water"
    with pytest.raises(ValueError, match="choose a mood"):
        music.pick("polka-party")


async def test_bed_fits_the_length_and_ducks_under_speech(tmp_path):
    from app.tools import music
    _installed()
    bed = await music.bed(music.pick("calm", "x"), 12, tmp_path / "bed.wav")
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(bed)],
                               capture_output=True, text=True).stdout)
    assert abs(dur - 12) < 0.1
    speech = tmp_path / "speech.wav"                       # „beszéd” 1–4 s és 7–10 s között, máshol csend
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=220:d=12", "-af",
                    "volume='if(between(t,1,4)+between(t,7,10),1,0)':eval=frame", str(speech)], check=True)
    ducked = await music.duck(speech, bed, tmp_path / "ducked.wav")
    under, free = _mean_db(ducked, 2, 1.5), _mean_db(ducked, 5, 1.5)
    assert free - under >= 6, (under, free)               # a beszéd alatt legalább 6 dB-lel halkabb


async def test_video_music_goes_under_the_speech(tmp_path):
    """A videó útja: a tár zenéje a hosszra szabva, a beszéd mért szintje alá, halkítva — a check kimondja."""
    _installed()
    from app.tools import video
    speech = tmp_path / "speech.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=220:d=10", "-af",
                    "volume='if(between(t,1,4),1,0)':eval=frame", str(speech)], check=True)
    orig, track, verified, findings = await video._music_under({"music": "calm"}, tmp_path, 10, speech, None, "Beáta")
    assert orig == "music.wav" and track == "music-ducked.wav" and not findings
    assert "drops under the speech" in verified[0] and "CC0" in verified[0]
    orig, track, verified, _ = await video._music_under({"music": "calm"}, tmp_path, 10, None, None, "Beáta")
    assert track == orig == "music.wav" and "no speech" in verified[0]
    with pytest.raises(ValueError, match="choose a mood"):
        video.check_music("polka-party")
    video.check_music("none")
