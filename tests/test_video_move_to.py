"""3.6 (OMLETT, 10-19): a move_to odasiklás a megjelenés UTÁN indul, move_dur alatt (alap: dur), lágyan — a 10-19-i
rendernél dur 2.2 mellett a jelölő 4,3 s-nál még az út elején járt (x ≈ 0,23 a 0,88 helyett)."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


def _red_x(png: Path) -> float:
    """A piros jelölő közepe a képkocka szélességének arányában."""
    import numpy as np
    from PIL import Image
    a = np.asarray(Image.open(png).convert("RGB")).astype(int)
    m = (a[:, :, 0] > 150) & (a[:, :, 1] < 110) & (a[:, :, 2] < 110)
    assert m.sum() > 20, "the marker is not in the frame"
    return float(np.nonzero(m)[1].mean() / a.shape[1])


def test_move_dur_is_kept_and_reaches_the_builder():
    from app.tools.video import _graphic_elements, _graphic_from_plan
    ch, _ = _graphic_from_plan({"type": "graphic", "elements": [
        {"shape": "circle", "x": 0.2, "y": 0.5, "r": 0.05, "fill": "red", "at": 1.5, "dur": 2.2, "move_to": [0.88, 0.5]},
        {"shape": "circle", "x": 0.2, "y": 0.5, "r": 0.05, "at": 1.5, "dur": 0.4, "move_to": [0.5, 0.5], "move_dur": 3}]}, "")
    a, b = _graphic_elements(ch, 1280, 720, [1, 0, 0])
    assert a["move_to"] == [round(0.88 * 1280, 1), 360.0] and "move_dur" not in a
    assert b["move_dur"] == 3.0


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_marker_is_at_its_target_after_appear_plus_dur(tmp_path, put_file):
    from app.tools import video
    clip = tmp_path / "black.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:size=640x360:rate=10", "-t", "6",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True)
    layer = {"id": "M", "at": 0, "dur": 6, "type": "graphic", "elements": [
        {"shape": "circle", "x": 0.2, "y": 0.5, "r": 0.06, "fill": "red", "at": 1.5, "dur": 2.2, "move_to": [0.88, 0.5]}]}
    res = asyncio.run(video.overlay({"file_id": put_file(clip), "layers": [layer], "render": False}))
    work = Path(tempfile.mkdtemp(dir=tmp_path))
    proj = video.stage(res["file_id"], work)

    def x_at(t: float) -> float:
        out = work / f"f{t}.png"
        asyncio.run(video._run("render-frame", "--project", proj.name, "--comp", "Video", "--time", str(t), "--out", out.name,
                               cwd=work))
        return _red_x(out)
    assert abs(x_at(1.95) - 0.2) < 0.03                 # megjelent (0,5 s), még a helyén: a csúszás most indul
    assert 0.3 < x_at(3.1) < 0.8                         # útközben
    assert abs(x_at(4.3) - 0.88) < 0.03                  # 1,5 + 0,5 + 2,2 = 4,2 s-ra odaért (main: x ≈ 0,23)
