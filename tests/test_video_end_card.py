"""3.4 (OMLETT, overlay-oldal): zárókártya az overlay végére — a felvétel automatikusan meghosszabbodik (az utolsó kocka
kimerevítve: tpad, a hang csenddel: apad); eddig az overlay hossza mindig a felvételé volt."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


def _durations(p: Path) -> dict:
    out = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(p)],
                                    capture_output=True, text=True, check=True).stdout)
    return {s["codec_type"]: float(s.get("duration") or out["format"]["duration"]) for s in out["streams"]}


def test_end_card_forms_and_errors():
    from app.tools.video import end_card_of
    assert end_card_of({}) is None and end_card_of({"end_card": "none"}) is None and end_card_of({"end_card": {}}) is None
    assert end_card_of({"end_card": "Köszönjük"}) == {"title": "Köszönjük", "subtitle": "", "kicker": "", "dur": 4.0}
    assert end_card_of({"end_title": "Vége", "end_subtitle": "Tamás TV"})["subtitle"] == "Tamás TV"
    assert end_card_of({"end_card": {"title": "X", "dur": "00:06"}})["dur"] == 6.0
    with pytest.raises(ValueError, match='"title": "Thanks for watching"'):
        end_card_of({"end_card": {"subtitle": "only"}})
    with pytest.raises(ValueError, match="1.5–20"):
        end_card_of({"end_card": {"title": "X", "dur": 60}})


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_overlay_end_card_extends_the_footage_and_edits_follow(tmp_path, put_file):
    from app.storage import output_path
    from app.tools import video
    clip = tmp_path / "c.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=48000", "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(clip)], check=True)
    res = asyncio.run(video.overlay({"file_id": put_file(clip), "end_card": {"title": "Köszönjük", "subtitle": "Tamás TV", "dur": 2}}))
    assert abs(res["duration"]["actual"] - 5.0) < 0.2, res["duration"]
    assert res["end_card"]["at"] == pytest.approx(3.0, abs=0.05) and res["end_card"]["dur"] == 2
    d = _durations(output_path(res["video"]["file_id"]))
    assert abs(d["video"] - 5.0) < 0.2 and abs(d["audio"] - 5.0) < 0.25                  # a hang is kitart (csenddel)
    assert any(v.startswith("End card “Köszönjük”") for v in res["check"]["verified"])
    # a kimerevített vég: a kártya alatt az utolsó kocka áll (két pillanat a kártya alatt ugyanaz a kép)
    import numpy as np
    from PIL import Image
    frames = []
    for t in (3.6, 4.6):
        png = tmp_path / f"f{t}.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", str(output_path(res["video"]["file_id"])), "-frames:v", "1",
                        str(png)], check=True)
        frames.append(np.asarray(Image.open(png).convert("L")).astype(int))
    assert np.abs(frames[0] - frames[1]).mean() < 3
    # szerkesztés: hosszabb kártya → újra meghosszabbítva; a kártya ki → az eredeti hossz
    ed = asyncio.run(video.edit(res["file_id"], {"end_card": {"dur": 3}}, render=False))
    assert ed["end_card"]["title"] == "Köszönjük" and ed["end_card"]["until"] == pytest.approx(6.0, abs=0.1)
    ed2 = asyncio.run(video.edit(ed["file_id"], {"end_card": "none", "add_layers": [{"at": 1, "type": "source", "text": "Forrás: teszt"}]},
                                 render=False))
    assert "end_card" not in ed2 and video.info(ed2["file_id"])["video"]["duration"] == pytest.approx(3.0, abs=0.1)
