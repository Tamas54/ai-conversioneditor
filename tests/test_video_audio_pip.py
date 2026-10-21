"""Videó-minőség (Kommandant 10-21, a Wan-os „Reggeli különkiadás” után):
- hangerő-kiegyenlítés: az összefűzött részek egy szintre (a Wan-klip ~13 dB-lel hangosabb volt), a hang nélküli rész
  csendet kap (különben az egész néma lett), az AI-klip és a bevágás hangja a felvételhez;
- a csupa nagybetűs név szélesebb (a névsáv neve két sorba tört és a tisztségre csúszott);
- kép a képben (pip): keretes ablak a sarokban, a kért méretben — a „pop” animáció nem állíthatja vissza a videó
  natív méretére (a motor hibája volt: a sarokablak a kép 93%-a lett a 60% helyett)."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _tone(path: Path, secs: float, db: float, *, video: bool = True, color: str = "blue", size: str = "320x240") -> Path:
    """Szinusz-hang adott szinten (dB), opcionálisan egyszínű képpel."""
    a = ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={secs},volume={db}dB"]
    v = ["-f", "lavfi", "-i", f"color=c={color}:size={size}:rate=25:duration={secs}"] if video else []
    subprocess.run(["ffmpeg", "-v", "error", "-y", *v, *a, *(["-c:v", "libx264", "-pix_fmt", "yuv420p"] if video else []),
                    "-c:a", "aac" if path.suffix != ".wav" else "pcm_s16le", "-shortest", str(path)], check=True)
    return path


def _silent_clip(path: Path, secs: float, color: str = "green") -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:size=320x240:rate=25:duration={secs}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(path)], check=True)
    return path


def _lufs(path: Path, start: float = 0, dur: float = 0) -> float:
    from app.tools import loudness
    src = path
    if dur:
        src = path.with_name(f"{path.stem}_{start:g}.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(dur), "-i", str(path), "-vn", str(src)], check=True)
    return asyncio.run(loudness.measure(src))["i"]


def test_level_file_brings_a_quiet_and_a_loud_clip_to_the_target(tmp_path):
    from app.tools import loudness
    quiet, loud = _tone(tmp_path / "q.mp4", 4, -14), _tone(tmp_path / "l.mp4", 4, 14)     # a szinusz alapból −18 dBFS
    assert _lufs(quiet) < -30 and _lufs(loud) > -12
    for src in (quiet, loud):
        dst = tmp_path / f"{src.stem}_lvl.mp4"
        assert asyncio.run(loudness.level_file(src, dst))["changed"]
        assert abs(_lufs(dst) - loudness.TARGET_LUFS) < 1.0
    assert loudness.chain(None) == "anull" and loudness.chain({"i": -70, "tp": -60, "lra": 0, "thresh": -80, "offset": 0}) == "anull"
    silent = _silent_clip(tmp_path / "s.mp4", 2)
    assert asyncio.run(loudness.measure(silent)) is None
    assert not asyncio.run(loudness.level_file(silent, tmp_path / "s2.mp4"))["changed"]


def test_merge_levels_the_parts_and_a_silent_part_does_not_mute_the_rest(tmp_path):
    from app.ops.handlers.files import merge
    parts = [_tone(tmp_path / "a.mp4", 3, -12, color="blue"), _silent_clip(tmp_path / "b.mp4", 2),
             _tone(tmp_path / "c.mp4", 3, 14, color="red")]
    (tmp_path / "out").mkdir()
    (tmp_path / "raw").mkdir()
    out = asyncio.run(merge(parts, {"output_format": "mp4"}, tmp_path / "out"))[0]
    st = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(out)],
                                   capture_output=True, text=True, check=True).stdout)
    assert any(s["codec_type"] == "audio" for s in st["streams"])                   # a néma rész nem némít el mindent
    assert abs(float(st["format"]["duration"]) - 8) < 0.3
    a, c = _lufs(out, 0.5, 2), _lufs(out, 5.5, 2)
    assert abs(a - c) < 1.5, (a, c)                                                 # egy szinten (előtte 27 dB különbség)
    raw = asyncio.run(merge(parts, {"output_format": "mp4", "level_audio": False}, tmp_path / "raw"))[0]
    assert _lufs(raw, 5.5, 2) - _lufs(raw, 0.5, 2) > 20                             # kikapcsolva: az eredeti szintek


def test_a_cut_in_takes_the_footages_loudness(tmp_path):
    from app.tools import loudness, video
    base = _tone(tmp_path / "footage.mp4", 6, -4)             # ~ −26 LUFS
    cut = _tone(tmp_path / "gfx-01-L1.wav", 3, 14, video=False)
    asyncio.run(video._level_cuts([{"file": cut.name, "t0": 1, "dur": 3, "mode": "replace"}], tmp_path, base))
    assert abs(_lufs(cut) - _lufs(base)) < 1.5
    loud = _tone(tmp_path / "gfx-02-L2.wav", 3, 10, video=False)                  # felvétel hang nélkül: a közös célszint
    asyncio.run(video._level_cuts([{"file": loud.name, "t0": 1, "dur": 3, "mode": "mix"}], tmp_path, None))
    assert abs(_lufs(loud) - loudness.TARGET_LUFS) < 1.0


def test_an_all_caps_name_is_measured_wider():
    from app.tools import video
    name = "OMLETT HELYETT KROKODIL A KONYHÁBAN"
    assert video._word_w("KONYHÁBAN", 40) > video._word_w("konyhában", 40) * 1.15
    # a névsáv 1280×720-on: régen 1 sornak számolta (37 px), a valóságban kettőbe tört — most kisebb betű, egy sorban
    s = video._fit_size(name, 37, 640, 37 * 1.25)
    assert s < 37 and video._wrap(name, s, 640)[0] == 1


def test_pip_layer_is_a_framed_window_in_the_corner():
    from app.tools import video
    notes: list[str] = []
    ly = video.layers_from([{"at": 2, "dur": 5, "type": "pip", "image": "abc.mp4", "corner": "bottom-left", "size": 0.5,
                             "aspect": "16:9", "sound": "mix", "clip": [1, 6]}], "", notes)[0]
    assert ly["type"] == "chart" and ly["pip"] == {"corner": "bottom-left", "size": 0.5, "aspect": 16 / 9}
    frame, movie = ly["chart"]["elements"]
    assert frame["shape"] == "rect" and movie["shape"] == "image" and movie["fit"] == "cover" and movie["sound"] == "mix"
    video._pip_geometry(ly, 1080, 1920)
    assert movie["w"] == 0.5 and abs(movie["h"] - 0.5 * 1080 / (16 / 9) / 1920) < 1e-3
    assert movie["x"] == pytest.approx(0.04 + 0.25) and movie["y"] == pytest.approx(1 - 0.04 * 1080 / 1920 - movie["h"] / 2, abs=1e-3)
    assert frame["w"] > movie["w"] and frame["h"] > movie["h"]
    for bad, msg in (({"corner": "middle"}, "corner"), ({"size": "big"}, "size"), ({"image": None}, "image")):
        with pytest.raises(ValueError, match=msg):
            video.layers_from([{"at": 0, "dur": 3, "type": "pip", "image": "abc.mp4", **bad}], "", [])


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI (EFFECTCRAFT_CLI) and ffmpeg needed")
def test_pip_window_renders_at_the_asked_size(tmp_path, put_file):
    """Kék felvétel, piros videó a jobb felső sarokban 0,4 szélességben: az ablak közepe piros, a kért dobozon kívül
    (alatta és balra tőle) kék — a pop animáció után is."""
    from PIL import Image
    from app.storage import output_path
    from app.tools import video
    base = put_file(_tone(tmp_path / "base.mp4", 4, -20, color="blue", size="1280x720"))
    red = put_file(_tone(tmp_path / "red.mp4", 4, -20, color="red", size="1280x720"))
    res = asyncio.run(video.overlay({"file_id": base, "layers": [
        {"at": 0.2, "dur": 3.6, "type": "pip", "image": red, "corner": "top-right", "size": 0.4}]}))
    out = output_path(res["video"]["file_id"])
    png = tmp_path / "f.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "2.5", "-i", str(out), "-frames:v", "1", str(png)], check=True)
    im = Image.open(png).convert("RGB")
    W, H = im.size
    w, h = 0.4 * W, 0.4 * W * 9 / 16
    cx, cy = W - 0.04 * W - w / 2, 0.04 * W + h / 2

    def is_red(p):
        r, g, b = im.getpixel((int(p[0]), int(p[1])))
        return r > 150 and b < 100

    def is_blue(p):
        r, g, b = im.getpixel((int(p[0]), int(p[1])))
        return b > 150 and r < 100
    assert is_red((cx, cy)) and is_red((cx - w * 0.4, cy)) and is_red((cx, cy + h * 0.4))
    assert is_blue((cx - w * 0.6, cy)) and is_blue((cx, cy + h * 0.65)) and is_blue((W * 0.2, H * 0.6))
