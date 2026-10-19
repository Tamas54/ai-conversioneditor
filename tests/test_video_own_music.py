"""Saját zene a saját felvételre (overlay): a felhasználó feltöltött zenéje, kezdőponttal (music.start) és szinttel
(music.level: background | even | front | only); veszteségmentesen a végső kódolásig, nem hurkolva. A beépített tár
alapból KI (MUSIC_LIBRARY) — Kommandant 10-19: „bízzuk a userre”, és ami a miénk, az legyen „korrekt”."""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path

import pytest


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def _band_db(f: Path, freq: int, start: float, dur: float) -> float:
    """Egy frekvenciasáv átlagos szintje (dB) a [start, start+dur] szakaszban — melyik hang szól ott."""
    band = ",".join([f"bandpass=f={freq}:width_type=h:w=60"] * 3)             # három fokozat: a szomszéd hang ne szivárogjon át
    err = subprocess.run(["ffmpeg", "-v", "info", "-ss", str(start), "-t", str(dur), "-i", str(f), "-af",
                          f"{band},volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    m = re.search(r"mean_volume: (-?[\d.]+) dB", err)
    return float(m.group(1)) if m else -120.0


def _song(path: Path) -> Path:
    """Két részes „dal”: 0–3 s 880 Hz, 3–6 s 1320 Hz — a kezdőpont így hallhatóan ellenőrizhető."""
    _ff("-f", "lavfi", "-i", "sine=f=880:d=3", "-f", "lavfi", "-i", "sine=f=1320:d=3", "-filter_complex",
        "[0:a][1:a]concat=n=2:v=0:a=1", "-c:a", "libmp3lame", "-b:a", "192k", str(path))
    return path


def test_music_choice_forms_and_errors():
    from app.tools.video import _make_music, music_choice
    assert music_choice("none") == {"mood": None, "file_id": None, "start": 0.0, "level": "background", "off": True}
    got = music_choice({"file_id": "ab.mp3", "start": "01:05", "level": "front"})
    assert got["file_id"] == "ab.mp3" and got["start"] == 65.0 and got["level"] == "front" and not got["off"]
    assert music_choice(None, "cd.flac")["file_id"] == "cd.flac"                 # a régi music_file_id ugyanaz
    with pytest.raises(ValueError, match="background \\| even \\| front \\| only"):
        music_choice({"file_id": "ab.mp3", "level": "loud"})
    with pytest.raises(ValueError, match="not volume"):
        music_choice({"file_id": "ab.mp3", "volume": 3})
    with pytest.raises(ValueError, match="isn't audio"):
        music_choice({"file_id": "clip.mp4"})
    with pytest.raises(ValueError, match="not a time"):
        music_choice({"file_id": "ab.mp3", "start": "soon"})
    assert _make_music({"file_id": "ab.mp3"}) == {"music": None, "music_file_id": "ab.mp3"}
    with pytest.raises(ValueError, match="own footage"):
        _make_music({"file_id": "ab.mp3", "start": 10})


def test_builtin_library_is_off_by_default(monkeypatch):
    from app.tools import music, video
    monkeypatch.delenv("MUSIC_LIBRARY", raising=False)
    assert not music.enabled() and music.pick("calm", "x") is None
    video.check_music("calm")                      # nem hiba: a check mondja ki, hogy nincs zene (és mit adjon helyette)
    orig, track, verified, findings = asyncio.run(video._music_under({"music": "calm"}, Path("/nonexistent"), 10, None, None, "x"))
    assert track is None and "music={\"file_id\"" in findings[0]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg kell")
def test_own_bed_starts_where_asked_and_does_not_loop(tmp_path):
    from app.tools import music
    song = _song(tmp_path / "song.mp3")
    left = asyncio.run(music.own_bed(song, 5.0, tmp_path / "bed.wav", start=3.0))
    assert left == pytest.approx(3.0, abs=0.1)                                   # 3 s maradt a dalból
    bed = tmp_path / "bed.wav"
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(bed)],
                               capture_output=True, text=True).stdout)
    assert abs(dur - 5.0) < 0.05                                                 # a videó hossza (csenddel kitöltve)
    assert _band_db(bed, 1320, 0.5, 1.5) > _band_db(bed, 880, 0.5, 1.5) + 20     # a második résszel kezd
    assert _band_db(bed, 1320, 3.6, 1.2) < -60                                   # a dal vége után csend, nincs hurok
    with pytest.raises(ValueError, match="past the end"):
        asyncio.run(music.own_bed(song, 5.0, tmp_path / "x.wav", start=9))


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_overlay_with_own_music_levels_and_edit_keeps_the_song(tmp_path, put_file):
    """Felvétel (330 Hz „beszéd”) + saját zene 1 s-tól: „only” → csak a zene szól; az edit csak a szintet állítja
    (a dal a projektben tárolt példányból jön, a feltöltött fájl nélkül is) → „background”: a felvétel hangja visszajön."""
    from app.storage import output_path
    from app.storage.metadata import read_meta
    from app.tools import crafts, video
    clip = tmp_path / "c.mp4"
    _ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10", "-f", "lavfi", "-i", "sine=f=330:sample_rate=48000", "-t", "4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clip))
    song_id = put_file(_song(tmp_path / "Harvester.mp3"))
    res = asyncio.run(video.overlay({"file_id": put_file(clip), "music": {"file_id": song_id, "start": 1, "level": "only"},
                                     "layers": [{"at": 0.5, "dur": 2, "type": "title", "text": "Teszt"}]}))
    out = output_path(res["video"]["file_id"])
    assert _band_db(out, 330, 0.5, 1.5) < _band_db(out, 880, 0.5, 1.5) - 25, "only: the footage's sound must be muted"
    assert _band_db(out, 880, 0.3, 1.2) > -45                                    # a dal 1 s-tól: előbb a 880 Hz-es rész
    assert _band_db(out, 1320, 2.3, 1.0) > -45                                   # …aztán a második
    verified = " ".join(res["check"]["verified"])
    assert "Your music “Harvester" in verified and "muted" in verified
    pid = res["file_id"]
    assert read_meta(pid)["spec"]["opts"]["music_src"] == "music-src.mp3"
    crafts.existing(song_id).unlink()                                            # a feltöltés lejárt — a projekt példánya marad
    ed = asyncio.run(video.edit(pid, {"music": {"level": "background"}}))
    out2 = output_path(ed["video"]["file_id"])
    assert _band_db(out2, 330, 0.5, 1.5) > -40, "background: the footage's own sound is back"
    assert "drops under the speech" in " ".join(ed["check"]["verified"])
