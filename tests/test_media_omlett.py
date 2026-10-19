"""OPERATION OMLETT, média-sáv: álló klipek a merge-ben (1.1), kódolási profil (1.2), HDR-bemenet (3.3), meghosszabbítás
és kép→videó (3.4), videóeffektek (3.2), `video info` nyers videón és több fájl metaadata (5).

Minden minta szintetikus (ffmpeg testsrc / sine, elforgatás-jelöléssel); valódi felvétel nincs."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg needed")


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def _clip(path: Path, w: int, h: int, seconds: float = 1, rotate: int | None = None, fps: int = 10,
          src: str = "testsrc2") -> Path:
    """Szintetikus klip (rózsazaj-hanggal: a hangkódoló így a teljes bitrátát használja); rotate: a telefonos elforgatás-jelölés (kijelzőmátrix + rotate címke, átcsomagolva)."""
    raw = path.with_name("raw-" + path.name) if rotate else path
    _ff("-f", "lavfi", "-i", f"{src}=size={w}x{h}:rate={fps}:duration={seconds}", "-f", "lavfi", "-i",
        f"anoisesrc=duration={seconds}:color=pink:amplitude=0.2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt",
        "yuv420p", "-c:a", "aac", "-ac", "2", "-b:a", "256k", "-shortest", str(raw))
    if rotate:
        _ff("-i", str(raw), "-c", "copy", "-metadata:s:v:0", f"rotate={rotate}", str(path))
    return path


def _probe(p: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _vsize(p: Path) -> tuple[int, int]:
    v = next(s for s in _probe(p)["streams"] if s["codec_type"] == "video")
    return int(v["width"]), int(v["height"])


def _frame(p: Path, t: float, dst: Path):
    from PIL import Image
    _ff("-ss", f"{t:.2f}", "-i", str(p), "-frames:v", "1", str(dst))
    return Image.open(dst).convert("RGB")


def _mean(im, box=None) -> float:
    from PIL import ImageStat
    return sum(ImageStat.Stat(im.crop(box) if box else im).mean) / 3


def _out(fid: str) -> Path:
    from app.storage import output_path
    return output_path(fid)


async def _run(key: str, fids: list[str], **opts):
    from app.ops import run_tool
    return await run_tool(key, fids, opts, charge=False)


# ---------------------------------------------------------------- 1.1 álló klipek a merge-ben

def test_rotation_is_read_from_the_tag_and_from_the_display_matrix():
    from app.tools import mediaprobe
    legacy = {"codec_type": "video", "width": 1920, "height": 1080, "tags": {"rotate": "90"}}
    matrix = {"codec_type": "video", "width": 1920, "height": 1080, "side_data_list": [{"side_data_type": "Display Matrix",
                                                                                       "rotation": -90}]}
    flat = {"codec_type": "video", "width": 1920, "height": 1080}
    for st, rot in ((legacy, 90), (matrix, 90), (flat, 0)):
        m = mediaprobe.summarize({"streams": [st], "format": {"duration": "2.0"}})
        assert m["rotation"] == rot
        assert (m["display_width"], m["display_height"]) == ((1080, 1920) if rot else (1920, 1080))
    hlg = mediaprobe.summarize({"streams": [{**flat, "color_transfer": "arib-std-b67", "pix_fmt": "yuv420p10le"}]})
    assert hlg["hdr"] == "hlg" and hlg["bit_depth"] == 10


@pytest.mark.asyncio
async def test_merge_keeps_portrait_phone_clips_portrait_with_blur_fill(tmp_path, put_file):
    """Elfogadás (1.1): 2 álló (kódolva 1920×1080 + rotation 90) + 1 fekvő klip → 1080×1920, a fekvő klip mögött
    a saját elmosott képe (nem fekete sáv). A régi kód a kódolt méretből 1920×1080-as vásznat csinált."""
    a = put_file(_clip(tmp_path / "allo1.mp4", 1920, 1080, rotate=90))
    b = put_file(_clip(tmp_path / "allo2.mp4", 1920, 1080, rotate=90))
    c = put_file(_clip(tmp_path / "fekvo.mp4", 1920, 1080))
    r = await _run("merge-files", [a, b, c])
    out = _out(r["file_id"])
    assert _vsize(out) == (1080, 1920)
    assert 2.7 <= float(_probe(out)["format"]["duration"]) <= 3.4
    fr = _frame(out, 2.5, tmp_path / "f.png")                 # a fekvő klip: középen a kép, fölötte-alatta kitöltés
    top = _mean(fr, (0, 0, 1080, 500))
    assert top > 25, f"the band above the landscape clip is black ({top:.0f}) — no blur fill"
    # contain kérésre fekete sáv, ugyanazon a vásznon
    r = await _run("merge-files", [a, c], fit="contain")
    out = _out(r["file_id"])
    assert _vsize(out) == (1080, 1920)
    assert _mean(_frame(out, 1.5, tmp_path / "g.png"), (0, 0, 1080, 500)) < 8


@pytest.mark.asyncio
async def test_merge_canvas_option_and_wrong_values(tmp_path, put_file):
    from app.convert.options import OptionError
    a = put_file(_clip(tmp_path / "a.mp4", 320, 180, rotate=90))
    b = put_file(_clip(tmp_path / "b.mp4", 320, 180))
    r = await _run("merge-files", [b, a])
    assert _vsize(_out(r["file_id"])) == (320, 180)                       # auto: az első klip (fekvő) szerint
    r = await _run("merge-files", [b, a], canvas="360x640", fit="cover")
    assert _vsize(_out(r["file_id"])) == (360, 640)
    with pytest.raises(OptionError):
        await _run("merge-files", [b, a], canvas="huge")
    with pytest.raises(OptionError):
        await _run("merge-files", [b, a], fit="squeeze")


@pytest.mark.asyncio
async def test_resize_video_blur_fills_the_bars(tmp_path, put_file):
    v = put_file(_clip(tmp_path / "wide.mp4", 320, 180))
    r = await _run("resize-video", [v], resolution="360x640", aspect_mode="blur")
    out = _out(r["file_id"])
    assert _vsize(out) == (360, 640)
    assert _mean(_frame(out, 0.5, tmp_path / "f.png"), (0, 0, 360, 150)) > 25
    r = await _run("resize-video", [v], resolution="360x640", aspect_mode="pad")
    assert _mean(_frame(_out(r["file_id"]), 0.5, tmp_path / "g.png"), (0, 0, 360, 150)) < 8


# ---------------------------------------------------------------- 1.2 egységes kódolási profil

def _x264_settings(p: Path) -> str:
    """Az x264 a beállításait (crf=…, preset…) SEI-szövegként a fájl elejére írja."""
    import re
    head = p.read_bytes()[:400_000]
    m = re.search(rb"x264 - core .{0,1500}?options: ([^\x00]{20,1500})", head, re.S)
    return m.group(1).decode("latin-1") if m else ""


def _ssim(a: Path, b: Path) -> float:
    import re
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(a), "-i", str(b), "-lavfi", "[0:v][1:v]ssim", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    return float(re.search(r"All:([\d.]+)", err).group(1))


def test_encoding_profile_reads_the_environment(monkeypatch):
    from app.tools import encoding
    for k in ("VIDEO_CRF_INTERMEDIATE", "VIDEO_CRF_FINAL", "VIDEO_X264_PRESET", "VIDEO_AUDIO_BITRATE", "VIDEO_FINAL_ENCODER"):
        monkeypatch.delenv(k, raising=False)
    assert encoding.x264()[encoding.x264().index("-crf") + 1] == "17"
    assert encoding.x264("final")[encoding.x264("final").index("-crf") + 1] == "19"
    assert encoding.aac() == ["-c:a", "aac", "-b:a", "192k"] and encoding.final_encoder() == "x264"
    monkeypatch.setenv("VIDEO_CRF_INTERMEDIATE", "16")
    monkeypatch.setenv("VIDEO_X264_PRESET", "rm -rf")                     # rossz érték → az alap marad
    monkeypatch.setenv("VIDEO_FINAL_ENCODER", "engine")
    assert "16" in encoding.x264() and "medium" in encoding.x264() and encoding.final_encoder() == "engine"


@pytest.mark.asyncio
async def test_media_ops_encode_with_the_profile(tmp_path, put_file, monkeypatch):
    """resize/merge: x264 CRF 17 (a régi CRF 20 helyett), AAC 192k, moov elöl; a kép a forráshoz közel marad."""
    for k in ("VIDEO_CRF_INTERMEDIATE", "VIDEO_X264_PRESET", "VIDEO_AUDIO_BITRATE"):
        monkeypatch.delenv(k, raising=False)
    from app.tools import ios_safe
    src = _clip(tmp_path / "zaj.mp4", 320, 240, seconds=1, fps=15)
    v = put_file(src)
    r = await _run("resize-video", [v], resolution="320x240")
    out = _out(r["file_id"])
    assert "crf=17.0" in _x264_settings(out)
    a = next(s for s in _probe(out)["streams"] if s["codec_type"] == "audio")
    assert a["codec_name"] == "aac" and int(a["bit_rate"]) > 150_000
    assert ios_safe.moov_first(out) is True
    assert _ssim(out, src) > 0.97
    r = await _run("merge-files", [v, r["file_id"]])
    assert "crf=17.0" in _x264_settings(_out(r["file_id"]))


def _engine_ok() -> bool:
    from app.tools import video
    return video.available()


@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI (EFFECTCRAFT_CLI) needed")
@pytest.mark.asyncio
async def test_final_render_is_x264_crf_from_a_prores_master(tmp_path, put_file, monkeypatch):
    """A végső MP4 nem a renderelő rögzített 8 Mbps-os H.264-e: ProRes LT köztes → x264 CRF 19, AAC 192k, BT.709."""
    for k in ("VIDEO_CRF_FINAL", "VIDEO_FINAL_ENCODER", "VIDEO_AUDIO_BITRATE"):
        monkeypatch.delenv(k, raising=False)
    from app.tools import video
    fid = put_file(_clip(tmp_path / "felvetel.mp4", 320, 240, seconds=2, fps=15))
    res = await video.overlay({"file_id": fid, "brand": "none", "title": "Profil",
                               "layers": [{"at": 0.5, "dur": 1, "type": "source", "text": "Forrás: teszt"}]})
    out = _out(res["video"]["file_id"])
    assert "crf=19.0" in _x264_settings(out)
    st = {s["codec_type"]: s for s in _probe(out)["streams"]}
    assert st["audio"]["codec_name"] == "aac" and int(st["audio"]["bit_rate"]) > 150_000
    assert st["video"].get("color_primaries") == "bt709" and st["video"]["pix_fmt"] == "yuv420p"
    assert not list(tmp_path.glob("**/render-master.mov"))
