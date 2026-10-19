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


# ---------------------------------------------------------------- 3.3 HDR-bemenet (HLG / PQ → SDR BT.709)

def _hlg_clip(path: Path, w: int = 320, h: int = 180) -> tuple[Path, Path]:
    """Szintetikus iPhone-szerű HLG-klip: SDR testsrc2 → lineáris → BT.2020 + HLG (a referenciafehér 203 nit = 75%),
    10 bites HEVC (libx265 nélkül 10 bites H.264), HLG-jelöléssel. → (SDR-eredeti, HLG)"""
    sdr = path.with_name("sdr-" + path.name)
    _ff("-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=10:duration=1", "-c:v", "libx264", "-crf", "10", "-pix_fmt",
        "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", str(sdr))
    vf = ("zscale=tin=bt709:min=bt709:pin=bt709:rin=tv:t=linear:npl=100,format=gbrpf32le,"
          "zscale=p=bt2020:t=arib-std-b67:m=bt2020nc:r=tv:npl=203,format=yuv420p10le")
    encs = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    codec = (["-c:v", "libx265", "-x265-params", "log-level=error:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
              "-tag:v", "hvc1"] if "libx265" in encs else ["-c:v", "libx264", "-profile:v", "high10"])
    _ff("-i", str(sdr), "-vf", vf, *codec, "-color_primaries", "bt2020", "-color_trc", "arib-std-b67",
        "-colorspace", "bt2020nc", str(path))
    return sdr, path


def _stats(p: Path) -> tuple[float, float]:
    """Átlagos fényerő (YAVG) és telítettség (SATAVG), 8 bites skálán."""
    import re
    out = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(p), "-vf", "format=yuv420p,signalstats,metadata=print:file=-",
                          "-f", "null", "-"], capture_output=True, text=True).stdout
    y = [float(x) for x in re.findall(r"signalstats.YAVG=([\d.]+)", out)]
    s = [float(x) for x in re.findall(r"signalstats.SATAVG=([\d.]+)", out)]
    return sum(y) / len(y), sum(s) / len(s)


def _zscale() -> bool:
    return "zscale" in subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout


@pytest.mark.skipif(not _zscale(), reason="ffmpeg without zimg (zscale)")
@pytest.mark.asyncio
async def test_hlg_footage_is_tone_mapped_to_bt709_not_washed_out(tmp_path, put_file):
    """Elfogadás (3.3): 10 bites HLG-klip → resize / merge / az overlay alapsávja BT.709, és nem fakó: a fényerő és a
    telítettség az SDR-eredeti közelében (leképezés nélkül a telítettség az eredeti ~40%-a volt)."""
    from app.tools import mediaprobe, video
    sdr, hlg = _hlg_clip(tmp_path / "hlg.mp4")
    m = await mediaprobe.info(hlg)
    assert m["hdr"] == "hlg" and m["bit_depth"] == 10
    y0, s0 = _stats(sdr)
    fid = put_file(hlg)
    r = await _run("resize-video", [fid], resolution="320x180")
    outs = [_out(r["file_id"])]
    r = await _run("merge-files", [fid, put_file(sdr)])
    outs.append(_out(r["file_id"]))
    outs.append(_out(await video._upright(hlg)))                          # az overlay alapsávja (renderelő bemenete)
    for out in outs:
        v = next(s for s in _probe(out)["streams"] if s["codec_type"] == "video")
        assert v.get("color_transfer") == "bt709" and v["pix_fmt"] == "yuv420p", (out.name, v.get("color_transfer"))
    for out in outs[:1] + outs[2:]:
        y, s = _stats(out)
        assert 0.8 * y0 <= y <= 1.15 * y0, f"luma {y:.0f} vs SDR {y0:.0f}"
        assert s >= 0.72 * s0, f"washed out: saturation {s:.0f} vs SDR {s0:.0f}"


# ---------------------------------------------------------------- 3.4 meghosszabbítás, kép → videó, allowlist

def test_ffmpeg_allowlist_takes_tpad_loop_apad_and_lists_what_is_allowed():
    from app.convert import advanced
    ok = ["-vf", "tpad=stop_mode=clone:stop_duration=3", "-af", "apad=pad_dur=3"]
    assert advanced.check(ok) == ok
    assert advanced.check(["-vf", "loop=loop=90:size=1:start=45"]) and advanced.check(["-vf", "loop=2:10:0"])
    with pytest.raises(advanced.AdvancedError) as e:
        advanced.check(["-vf", "zoompan=z=2"])
    assert "Allowed video filters:" in str(e.value) and "tpad" in str(e.value) and "scale" in str(e.value)
    with pytest.raises(advanced.AdvancedError) as e:
        advanced.check(["-filter_complex", "x"])
    assert "-crf" in str(e.value) and "apad" in str(e.value)
    for bad in (["-af", "apad"], ["-vf", "loop=loop=-1:size=1"], ["-vf", "loop=loop=1000:size=100"],
                ["-vf", "tpad=stop_duration=4000"], ["-vf", "movie=/etc/passwd"], ["-af", "apad=pad_dur=1;x"]):
        with pytest.raises(advanced.AdvancedError):
            advanced.check(bad)                            # végtelen / óriási kimenet, fájl, gráf: nem megy át


def _same(a, b) -> bool:
    from PIL import ImageChops, ImageStat
    return max(ImageStat.Stat(ImageChops.difference(a, b)).mean) < 2.5


@pytest.mark.asyncio
async def test_extend_video_holds_the_last_frame_or_a_moment(tmp_path, put_file):
    v = put_file(_clip(tmp_path / "klip.mp4", 320, 180, seconds=2))
    r = await _run("extend-video", [v], duration=1.5)
    out = _out(r["file_id"])
    pr = _probe(out)
    assert 3.3 <= float(pr["format"]["duration"]) <= 3.8
    a = next(s for s in pr["streams"] if s["codec_type"] == "audio")
    assert float(a["duration"]) >= 3.2                                   # a hang is kitart (csend)
    assert _same(_frame(out, 2.3, tmp_path / "a.png"), _frame(out, 3.3, tmp_path / "b.png"))
    assert not _same(_frame(out, 0.5, tmp_path / "c.png"), _frame(out, 1.5, tmp_path / "d.png"))
    r = await _run("extend-video", [v], duration=1, at="1")
    out = _out(r["file_id"])
    assert 2.8 <= float(_probe(out)["format"]["duration"]) <= 3.3
    assert _same(_frame(out, 1.1, tmp_path / "e.png"), _frame(out, 1.8, tmp_path / "f.png"))
    assert not _same(_frame(out, 2.1, tmp_path / "g.png"), _frame(out, 2.8, tmp_path / "h.png"))   # utána megy tovább
    from app.convert.options import OptionError
    with pytest.raises(OptionError):
        await _run("extend-video", [v], duration=1, at="9")


@pytest.mark.asyncio
async def test_image_to_video_still_and_ken_burns(tmp_path, put_file, samples):
    png = put_file(samples.png(size=(400, 300)))
    r = await _run("image-to-video", [png], duration=2)
    out = _out(r["file_id"])
    pr = _probe(out)
    assert r["file_id"].endswith(".mp4") and _vsize(out) == (400, 300)
    assert 1.8 <= float(pr["format"]["duration"]) <= 2.3
    assert {s["codec_type"] for s in pr["streams"]} == {"video", "audio"}     # néma sáv: a merge megtartja a hangot
    r = await _run("image-to-video", [png], duration=2, motion="zoom_in", canvas="360x640", fit="blur", fps=15)
    out = _out(r["file_id"])
    assert _vsize(out) == (360, 640)
    assert not _same(_frame(out, 0.0, tmp_path / "a.png"), _frame(out, 1.9, tmp_path / "b.png"))
    assert _mean(_frame(out, 1.0, tmp_path / "c.png"), (0, 0, 360, 120)) > 25          # blur-kitöltés, nem fekete


def test_new_video_tools_are_in_the_catalog_rest_and_mcp(client, samples, put_file):
    """Egy katalógus-bejegyzés = web, REST, MCP (file_tools leírása) és az ügynök — mint a többi videós eszköz."""
    import time
    from app import mcp_ext
    from app.catalog import tools as T
    doc = mcp_ext._tools_doc()
    for key in ("extend-video", "image-to-video"):
        assert T.available(key, static=True) and key in doc
    fid = put_file(samples.png(size=(200, 100)))
    job = client.post("/api/tools/run", json={"tool": "image-to-video", "file_ids": [fid], "options": {"duration": 1}}).json()
    for _ in range(400):
        if job.get("status") in ("done", "failed"):
            break
        time.sleep(0.05)
        job = client.get(f"/api/jobs/{job['id']}").json()
    assert job["status"] == "done", job
    assert job["result"]["file_id"].endswith(".mp4")
