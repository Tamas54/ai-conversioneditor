"""A szerkesztő munkamásolata, a végső kép az eredetiből (10-22, a gyors render 2. lépése — app/tools/footage.py).

Mérve, a 30 perces HDR iPhone-felvételen: a projekt előtti teljes méretű újrakódolás (felállítás + HDR 25,5 perc, a
zárókártya kimerevítése még 10,3 perc) helyett egy kis munkamásolat; a végső kép az eredetiből, egy menetben.
- a munkamásolat kicsi, és a projektben a kompozíció méretére nagyítva áll (a rétegek koordinátái a teljes méretéi);
- a végső kép az eredetiből készül, nem a kis másolatból (a mintás felvétel a felnagyított másolattól elmosódna);
- a zárókártya alatt a felvétel utolsó kockája áll, fájl nélkül (időtérkép);
- ahol a motor rajzolja a felvételt (ráközelítés), és a régi (teljes) úton: a teljes méretű felvétel kerül a helyére;
- lejárt eredetinél a kis másolatból készül, és a check ezt megmondja.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.tools import fast_render as fr
from app.tools import footage


def test_proxy_size_and_when_it_is_needed():
    assert footage.size(1920, 1080) == (960, 540) and footage.size(1080, 1920) == (540, 960)
    assert footage.size(3840, 2160) == (960, 540) and footage.size(640, 360) == (640, 360)
    assert footage.needed({"rotation": 90}) and footage.needed({"hdr": "hlg"}) and footage.needed({}, {"lut": "cinema"})
    assert not footage.needed({"rotation": 0, "hdr": None, "width": 640, "height": 360}) and not footage.needed({"rotation": 360})
    assert footage.needed({"display_width": 1920, "display_height": 1080})              # nagy: a böngésző egészben töltené be
    assert footage.needed({"width": 640, "height": 360}, nbytes=500 * 1024 * 1024)
    rate = footage._rate(3600.0)
    assert rate[rate.index("-maxrate") + 1] == "888k"                                  # 1 óra ≤ 400 MB


def test_slices_stay_inside_the_footage_when_the_end_card_holds_it():
    """A zárókártya alatti szakasznak nincs bemenete (a felvétel véget ért): a vágások a felvételen belül esnek."""
    cuts = fr.chunks(135.0, 30, n=8, media=120.0)
    assert cuts[-1] == (105.0, 135.0) and all(a < 120.0 for a, _ in cuts) and cuts[0][0] == 0.0
    assert fr.chunks(200.0, 25, n=4) == fr.chunks(200.0, 25, n=4, media=300.0)


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


def _clip(path: Path, secs: int = 3) -> Path:
    """Telefonos felvétel: 1280×720-ban tárolva, 90°-os elforgatás-jelöléssel (álló 720×1280), mintás kép, hang."""
    raw = path.with_name("raw-" + path.name)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=10", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=48000", "-t", str(secs), "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(raw)], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-c", "copy", "-metadata:s:v:0", "rotate=90", str(path)],
                   check=True)                                  # (kódolás közben az ffmpeg 4.4 nem írja ki a jelölést)
    return path


def _frame(path: Path, t: float, out: Path) -> Path:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1", str(out)], check=True)
    return out


def _psnr(a: Path, b: Path, t: float, size: tuple[int, int] = (720, 1280)) -> float:
    """Két videó egy kockája YUV-ban (RGB-be bontva a jelöletlen forrást BT.601-ként, a BT.709-nek jelölt kimenetet
    BT.709-ként számolná át az ffmpeg — ugyanaz a kép is ~24 dB-nek látszana); a kisebbik a teljes méretre nagyítva."""
    import re
    w, h = size
    raws = []
    for k, p in enumerate((a, b)):
        raw = p.with_name(f"{p.stem}-{t}-{k}.yuv")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t}", "-i", str(p), "-frames:v", "1", "-vf", f"scale={w}:{h}",
                        "-f", "rawvideo", "-pix_fmt", "yuv420p", str(raw)], check=True)
        raws += ["-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}", "-i", str(raw)]
    r = subprocess.run(["ffmpeg", "-v", "info", *raws, "-lavfi", "psnr", "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"average:(inf|[0-9.]+)", r.stderr)
    return 99.0 if m and m[1] == "inf" else float(m[1]) if m else 0.0


def _probe(p: Path) -> dict:
    return json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(p)],
                                     capture_output=True, text=True, check=True).stdout)


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_editor_gets_a_small_copy_and_the_video_comes_from_the_original(tmp_path, put_file):
    from app.storage import output_path, read_meta
    from app.tools import video
    clip = _clip(tmp_path / "phone.mp4")
    fid = put_file(clip)
    res = asyncio.run(video.overlay({"file_id": fid, "brand": "none",
                                     "layers": [{"at": 0.3, "dur": 1.0, "type": "lower_third", "name": "Anna Kovács", "role": "Mayor"}],
                                     "end_card": {"title": "Köszönjük", "dur": 2}}))
    base = read_meta(res["file_id"])["spec"]["base"]
    assert base["proxy"] == {"w": 540, "h": 960} and base["rotated"] in (90, 270) and base["asset"].startswith("footage-")
    pm = _probe(output_path(base["media_id"]))
    assert {(s["width"], s["height"]) for s in pm["streams"] if s["codec_type"] == "video"} == {(540, 960)}
    assert float(pm["format"]["duration"]) == pytest.approx(3.0, abs=0.15)          # a kártya nem hosszabbítja meg a fájlt
    assert any("small copy (540×960)" in v for v in res["check"]["verified"]), res["check"]["verified"]
    out = output_path(res["video"]["file_id"])
    assert res["video"].get("render_path"), res["check"]
    v = next(s for s in _probe(out)["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (720, 1280)
    assert float(_probe(out)["format"]["duration"]) == pytest.approx(5.0, abs=0.2)
    # a felvétel az eredetiből: a grafika nélküli kocka az eredeti felállított képe (mérve 52,9 dB; a felnagyított kis
    # másolathoz képest 35 dB)
    ref = tmp_path / "ref.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(clip), "-c:v", "libx264", "-crf", "1", str(ref)], check=True)
    p_orig, p_proxy = _psnr(out, ref, 2.5), _psnr(out, output_path(base["media_id"]), 2.5)
    assert p_orig > 45 and p_proxy < p_orig - 8, (p_orig, p_proxy)
    # a kártya alatt az utolsó kocka áll (két pillanat ugyanaz), és a hang kitart
    import numpy as np
    from PIL import Image
    a, b = (np.asarray(Image.open(_frame(out, t, tmp_path / f"h{t}.png")).convert("L")).astype(int) for t in (3.4, 4.6))
    assert np.abs(a - b).mean() < 3
    au = next(s for s in _probe(out)["streams"] if s["codec_type"] == "audio")
    assert float(au.get("duration") or 0) == pytest.approx(5.0, abs=0.25)
    # ugyanarról a felvételről egy újabb videó: a meglévő munkamásolatot kapja (nem készül újra)
    again = asyncio.run(video.overlay({"file_id": fid, "brand": "none", "render": False,
                                       "layers": [{"at": 0.5, "type": "source", "text": "Forrás: teszt"}]}))
    assert read_meta(again["file_id"])["spec"]["base"]["media_id"] == base["media_id"]


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_punch_in_and_full_render_use_the_full_size_footage(tmp_path, put_file, monkeypatch):
    """Ahol a motor rajzolja a felvételt (ráközelítés), a szakasz teljes méretű darabot kap; a régi úton az egész
    felvétel teljes méretben áll a projektben. Lejárt eredetinél: a kis másolatból, és a check szól."""
    from app.storage import output_path, read_meta
    from app.tools import crafts, video
    clip = _clip(tmp_path / "phone.mp4")
    fid = put_file(clip)
    body = {"file_id": fid, "brand": "none",
            "layers": [{"at": 0.2, "dur": 0.8, "type": "lower_third", "name": "Anna Kovács", "role": "Mayor"}],
            "camera": [{"at": 1.0, "zoom": 1.4, "dur": 0.3}, {"at": 2.4, "zoom": 1, "dur": 0.3}]}
    fast = asyncio.run(video.overlay(dict(body)))
    assert fast["video"].get("render_path"), fast["check"]
    monkeypatch.setenv("VIDEO_FAST_RENDER", "0")
    full = asyncio.run(video.overlay(dict(body)))
    assert not full["video"].get("render_path")
    fa, fb = output_path(fast["video"]["file_id"]), output_path(full["video"]["file_id"])
    t = 1.9                                                                      # ráközelítve, állva
    same = _psnr(fa, fb, t)
    assert same > 35, same
    # lejárt eredeti: a kis másolatból (elmosódottabb), és a check megmondja
    crafts.existing(fid).unlink()
    monkeypatch.setenv("VIDEO_FAST_RENDER", "1")
    gone = asyncio.run(video.render(fast["file_id"]))
    assert any("no longer stored" in f for f in gone["check"]["findings"]), gone["check"]
    assert _psnr(output_path(gone["file_id"]), fa, t) < same - 5
    assert read_meta(fast["file_id"])["spec"]["base"]["proxy"]
