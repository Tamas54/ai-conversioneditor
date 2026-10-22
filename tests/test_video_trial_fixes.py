"""A Tamás TV-próba (10-22, 2 perces iPhone-videó, 16 réteg) hibái:
- a szerkesztésben a grafika-réteg „elements”-e és a hálózati ábra „direction”-je némán elveszett;
- a three.js label() size-a betűméret px-ben (1,25 → elmosódott folt), a scale.setScalar összenyomja;
- a színkorrekció zárókártyás videón nem került a képre (a check mégis „Auto: gamma…”-t írt), az auto pedig a napfényes,
  meleg szobát erősen sötétítette.
"""
from __future__ import annotations

import asyncio
import colorsys
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = "24 / 226 MB"


def _layers():
    from app.tools import video
    notes: list[str] = []
    layers = video.layers_from([
        {"id": "L11", "at": 1, "dur": 5, "type": "graphic", "position": [0.74, 0.47, 0.44, 0.62], "backdrop": 0,
         "elements": [{"shape": "text", "text": "régi", "x": 0.5, "y": 0.5, "size": 60}]},
        {"id": "L9", "at": 7, "dur": 5, "chart": {"type": "network", "title": "Út", "nodes": [{"id": "a", "text": "Alfa"},
         {"id": "b", "text": "Béta"}], "edges": [{"from": "a", "to": "b"}]}},
        {"id": "L12", "at": 13, "dur": 4, "type": "pip", "image": "https://example.com/x.mp4", "corner": "top-right", "size": 0.38},
    ], SRC, notes)
    return layers, notes


def test_graphic_fields_at_the_top_of_an_edit_are_applied():
    from app.tools import video
    layers, _ = _layers()
    notes: list[str] = []
    out = video.apply_layer_changes(layers, {"layers": [
        {"id": "L11", "backdrop": 0.3, "elements": [{"shape": "text", "text": "új", "x": 0.5, "y": 0.5, "size": 60}]},
        {"id": "L9", "chart": {"nodes": [{"id": "a", "text": "Kezdet"}]}},
        {"id": "L12", "corner": "bottom-left"}]}, SRC, notes)
    g = out[0]["chart"]
    assert [e.get("text") for e in g["elements"]] == ["új"] and g["backdrop"] == 0.3
    assert [n["text"] for n in out[1]["chart"]["nodes"]] == ["Kezdet", "Béta"]          # a többi csúcs marad (id szerint)
    assert out[2]["pip"]["corner"] == "bottom-left" and out[2]["pip"]["size"] == 0.38
    assert not [n for n in notes if n.startswith("⚠")]


def test_a_field_that_is_not_applied_is_named():
    from app.tools import video
    layers, notes = _layers()
    assert not [n for n in notes if n.startswith("⚠")]                                  # a létrehozás mezői mind célba értek
    notes = []
    video.apply_layer_changes(layers, {"layers": [{"id": "L11", "colour": "red"}, {"id": "L9", "chart": {"direction": "down"}}]},
                              SRC, notes)
    warn = [n for n in notes if n.startswith("⚠")]
    assert any("Layer L11: colour — not a field of a graphic layer, so it was not applied" in n for n in warn), warn
    assert any("Layer L9" in n and "a network is laid out from its links" in n for n in warn), warn
    notes = []
    video.layers_from([{"at": 1, "dur": 3, "type": "fullscreen", "image": "https://example.com/y.jpg", "fit": "contain"}], SRC, notes)
    assert any("fit — not a field of a fullscreen layer" in n for n in notes)
    notes = []
    video.layers_from([{"at": 1, "type": "lower_third", "name": "Tamás", "role": "Tamás TV", "side": "left"}], SRC, notes)
    assert not [n for n in notes if n.startswith("⚠")]


def test_threejs_label_mistakes_are_reported():
    from app.tools import threejs
    bad = 'const t = label("TAMÁS TV", {size: 1.25, height: 0.2}); scene.add(t);\nt.scale.setScalar(0.5);'
    msgs = threejs.label_mistakes(bad)
    assert any("size is the FONT size in pixels" in m for m in msgs) and any("squashes the label" in m for m in msgs)
    good = 'const t = label("TAMÁS TV", {size: 140, height: 1.2}); const s0 = t.scale.clone(); t.scale.set(s0.x * 2, s0.y * 2, 1);'
    assert threejs.label_mistakes(good) == []
    from app.tools import video_schema
    ref = video_schema.reference("chart")["reference"]
    assert "size = the FONT size in pixels" in ref and "t.scale.clone()" in ref


def test_auto_leaves_a_bright_warm_room_alone_and_still_lifts_a_dark_one():
    from app.tools import video_fx
    room = video_fx.auto_params({"YMIN": 16.08, "YLOW": 120.65, "YAVG": 161.05, "YHIGH": 199.3, "YMAX": 234.47, "SATAVG": 39.43})
    assert "gamma" not in room and "saturation" not in room                             # a mért iPhone-felvétel (10-22)
    dark = video_fx.auto_params({"YAVG": 60, "YMIN": 16, "YMAX": 200, "YLOW": 30, "YHIGH": 150, "SATAVG": 35})
    assert 1 < dark["gamma"] <= 1.6
    burnt = video_fx.auto_params({"YAVG": 200, "YMIN": 40, "YMAX": 235, "YLOW": 170, "YHIGH": 232, "SATAVG": 35})
    assert 0.85 <= burnt["gamma"] < 1


def _hls(png: Path, box=(0.4, 0.4, 0.6, 0.6)) -> tuple[float, float, float]:
    from PIL import Image
    im = Image.open(png).convert("RGB")
    W, H = im.size
    px = list(im.crop((int(box[0] * W), int(box[1] * H), int(box[2] * W), int(box[3] * H))).getdata())
    r, g, b = (sum(c[i] for c in px) / len(px) / 255 for i in range(3))
    return colorsys.rgb_to_hls(r, g, b)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg needed")
def test_auto_keeps_warm_skin_warm(tmp_path):
    """Meleg, narancsos háttér + bőrtónusú folt: az auto után a folt árnyalata és világossága alig mozdul."""
    from app.tools import video_fx
    clip = tmp_path / "warm.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0xF0A060:size=320x240:rate=10:duration=2",
                    "-vf", "drawbox=x=112:y=84:w=96:h=72:color=0xD88A78:t=fill", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(clip)], check=True)
    st = asyncio.run(video_fx.measure(clip))
    auto = video_fx.auto_params(st)
    work = tmp_path / "w"
    work.mkdir()
    pngs = {}
    for name, vf in (("before", "null"), ("after", ",".join(video_fx.chain({"auto": True}, work, auto, temporal=False)) or "null")):
        pngs[name] = tmp_path / f"{name}.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(clip), "-frames:v", "1", "-vf", vf, str(pngs[name])],
                       check=True)
    (h0, l0, s0), (h1, l1, s1) = _hls(pngs["before"]), _hls(pngs["after"])
    assert abs(h1 - h0) * 360 < 3 and abs(l1 - l0) < 0.05 and s1 > s0 - 0.06, (auto, (h0, l0, s0), (h1, l1, s1))


def test_end_card_footage_name_follows_its_source():
    """A meghosszabbított felvétel neve a forrásét is viszi: a szerkesztés nem veheti át a szülő azonos nevű assetjét."""
    from app.tools import video

    async def fake_extend(src, dur):
        return f"ext-{Path(src).name}"
    orig = video._extend
    video._extend = fake_extend
    try:
        crafts_existing = video.crafts.existing
        video.crafts.existing = lambda fid: Path("/x") / fid
        a = asyncio.run(video._with_end_card({"file_id": "aaaa1111.mp4", "asset": "footage.mp4", "duration": 3.0},
                                             {"title": "Vége", "dur": 2.0}))
        b = asyncio.run(video._with_end_card({"file_id": "aaaa1111.mp4", "asset": "footage-fx-bbbb.mp4",
                                              "media_id": "bbbb2222.mp4", "duration": 3.0}, {"title": "Vége", "dur": 2.0}))
    finally:
        video._extend = orig
        video.crafts.existing = crafts_existing
    assert a["asset"] != b["asset"] and a["asset"].startswith("footage-end-2s-") and b["media_id"] == "ext-bbbb2222.mp4"


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


@pytest.mark.engines
def test_adjust_on_a_video_with_an_end_card_reaches_the_project(tmp_path, put_file):
    """Az új projekt felvétel-assetje a korrigált (eddig a szülő azonos nevű, korrigálatlan assetje lett)."""
    if not _engine_ok():
        pytest.skip("no render engine")
    import numpy as np
    from PIL import Image
    from app.storage import read_meta
    from app.tools import video
    clip = tmp_path / "c.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=48000", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(clip)], check=True)
    res = asyncio.run(video.overlay({"file_id": put_file(clip), "brand": "none", "render": False,
                                     "end_card": {"title": "Vége", "dur": 2}}))
    pid = res.get("file_id") or res["project"]["file_id"]
    ed = asyncio.run(video.edit(pid, {"adjust": {"lut": "black_white"}}, render=False))
    eid = ed.get("file_id") or ed["project"]["file_id"]
    asset = read_meta(eid)["spec"]["base"]["asset"]
    assert asset != read_meta(pid)["spec"]["base"]["asset"]
    from app.tools import crafts
    png = tmp_path / "f.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(crafts.existing(video.assets_of(eid)[asset])), "-frames:v", "1",
                    str(png)], check=True)
    assert float(np.asarray(Image.open(png).convert("HSV"))[..., 1].mean()) < 10      # a projektben a fekete-fehér felvétel
