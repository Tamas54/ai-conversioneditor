"""Hullámforma és audiogram (10-22, a vanta ötletlistájából): a hang hangereje képkockánként (gyors fel, lassabb le), a
sávok egy csúszkát követnek (kifejezéssel); a „waveform” réteg és a hangból készülő podcast-videó (action="audiogram")."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest


def _tone(path: Path, *, video: bool = True, secs: float = 4.0) -> Path:
    """1 s hangos, 1 s néma, ismétlődve (a hangerő-burkoló próbájához)."""
    a = ["-f", "lavfi", "-i", f"sine=frequency=300:sample_rate=48000:duration={secs},volume='if(lt(mod(t,2),1),1,0)':eval=frame"]
    v = ["-f", "lavfi", "-i", f"color=c=gray:size=640x360:rate=25:duration={secs}"] if video else []
    subprocess.run(["ffmpeg", "-v", "error", "-y", *v, *a, *(["-c:v", "libx264", "-pix_fmt", "yuv420p"] if video else []),
                    "-c:a", "aac" if video else "pcm_s16le", "-shortest", str(path)], check=True)
    return path


def test_levels_follow_the_sound(tmp_path):
    from app.tools import video
    lv = asyncio.run(video.wave_levels(_tone(tmp_path / "t.mp4"), 0, 4))
    step = video.WAVE_STEP
    loud, quiet = lv[int(0.5 / step)], lv[int(1.7 / step)]
    assert abs(len(lv) - 4 / step) <= 2 and loud > 55 and quiet < 15            # a szinusz −18 dBFS
    silent = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:size=64x64:rate=25:duration=2", "-an",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(silent)], check=True)
    assert asyncio.run(video.wave_levels(silent, 0, 2)) == []


def test_waveform_layer_is_checked_and_carries_its_levels():
    from app.tools import video
    ly = video.layers_from([{"at": 1, "dur": 3, "type": "waveform", "style": "rise", "bars": 100, "color": "#00ff00"}], "", [])[0]
    assert (ly["type"], ly["style"], ly["bars"], ly["color"]) == ("waveform", "rise", 64, "#00ff00")
    with pytest.raises(ValueError, match="bars | rise"):
        video.layers_from([{"at": 0, "type": "waveform", "style": "circle"}], "", [])
    ly["_levels"] = [50.0] * 200
    items, _, problems = video.layer_comps([ly], 1080, 1920, 10, "en", [1, 0, 0], 1700)
    wv = items[0]["wave"]
    assert wv["style"] == "rise" and wv["bars"] == 64 and len(wv["shape"]) == 64 and wv["color"] == pytest.approx([0, 1, 0], abs=0.01)
    assert len(wv["levels"]) <= int(3 / video.WAVE_STEP) + 2 and all(0.35 <= k <= 1 for k in wv["shape"])
    assert items[0]["w"] > 900                                                   # a képkocka szélességében
    assert video._wave_shape(10, "L1") == video._wave_shape(10, "L1") != video._wave_shape(10, "L2")


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


def _accent_px(png: Path, y0: float, y1: float) -> int:
    from PIL import Image
    im = Image.open(png).convert("RGB")
    W, H = im.size
    return sum(1 for x in range(0, W, 2) for y in range(int(H * y0), int(H * y1), 2)
               if (lambda p: p[1] > 170 and p[0] < 100 and p[2] < 100)(im.getpixel((x, y))))


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI (EFFECTCRAFT_CLI) and ffmpeg needed")
def test_bars_rise_with_the_sound(tmp_path, put_file):
    from app.storage import output_path
    from app.tools import video
    fid = put_file(_tone(tmp_path / "base.mp4"))
    res = asyncio.run(video.overlay({"file_id": fid, "accent": "#00ff00", "layers": [
        {"at": 0, "dur": 4, "type": "waveform", "position": "center"}]}))
    out = output_path(res["video"]["file_id"])
    counts = {}
    for t in (0.6, 1.8):
        png = tmp_path / f"f{t}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(out), "-frames:v", "1", str(png)], check=True)
        counts[t] = _accent_px(png, 0.3, 0.7)
    assert counts[0.6] > counts[1.8] * 1.5 and counts[0.6] > 40, counts          # hangos részen magasabb sávok (néma: pöttyök)


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI (EFFECTCRAFT_CLI) and ffmpeg needed")
def test_audiogram_makes_a_video_from_a_sound(tmp_path, put_file):
    import json
    from app.storage import output_path
    from app.tools import video
    fid = put_file(_tone(tmp_path / "talk.wav", video=False, secs=5))
    res = asyncio.run(video.audiogram({"file_id": fid, "format": "square", "title": "Podcast", "captions": False}))
    st = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                                    str(output_path(res["video"]["file_id"]))], capture_output=True, text=True).stdout)
    v = next(s for s in st["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1080, 1080) and any(s["codec_type"] == "audio" for s in st["streams"])
    assert abs(float(st["format"]["duration"]) - 5) < 0.3
    assert {x["type"] for x in res["layers"]} == {"waveform", "title"}
    with pytest.raises(ValueError, match="no sound"):
        silent = tmp_path / "mute.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:size=64x64:rate=25:duration=2", "-an",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(silent)], check=True)
        asyncio.run(video.audiogram({"file_id": put_file(silent)}))
