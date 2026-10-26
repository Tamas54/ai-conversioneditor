"""Stage layer (N5, app/tools/stage_layer.py): the schema, the geometry (the projected corners, the Corner Pin mapping),
the builder output and its depth order (behind the speaker / in front of him), the speaker items of the final render,
the live preview's matte part and known cycles, and the engine project end to end."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest

DECK = "0123456789abcdef0123456789abcdef.pptx"
PNG = "fedcba9876543210fedcba9876543210.png"


def _stage(**kw):
    return {"type": "stage", "at": 180, "until": 210, "deck": DECK,
            "panels": [{"slide": 2}, {"slide": 4}, {"slide": 7}, {"slide": 9}], **kw}


def _one(raw, notes=None):
    from app.tools import video
    [ly] = video.layers_from([raw], "", notes if notes is not None else [])
    return ly


# ---------- the schema ----------

def test_stage_is_validated_with_panels_background_and_pills():
    notes: list[str] = []
    ly = _one(_stage(background="particles", pills=[{"text": "Claude  Desktop", "near": 1}, "összehasonlítás"]), notes)
    assert ly["type"] == "stage" and ly["at"] == 180 and ly["dur"] == 30 and ly["preset"] == "auto"
    assert [p["slide"] for p in ly["panels"]] == [2, 4, 7, 9] and [p["slot"] for p in ly["panels"]] == [1, 2, 3, 4]
    assert all(p["kind"] == "slide" and p["file_id"] == DECK and p["enter"] == "fly" and p["frame"] == "plain" for p in ly["panels"])
    assert [p["at"] for p in ly["panels"]] == [180.35, 180.53, 180.71, 180.89] and {p["until"] for p in ly["panels"]} == {210}
    assert "depth" not in ly["panels"][0]                         # auto: decided at build time (the preset, the face)
    assert ly["background"] == {"kind": "particles"}
    assert ly["pills"][0] == {"text": "Claude Desktop", "style": "accent", "near": 1, "at": 181.0, "until": 210.0}
    assert ly["pills"][1]["text"] == "összehasonlítás" and ly["pills"][1]["at"] == 181.5
    from app.tools import video
    assert video._layer_text(ly) == "stage (4 panels, particles background, 2 pills)"
    assert not [n for n in notes if n.startswith("⚠")]


def test_stage_preset_depth_box_and_pictures():
    ly = _one(_stage(preset="grid4", panels=[{"slide": 1}, {"file_id": PNG, "depth": "front", "tilt": [0, 20, 0]},
                                             {"file_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90.mp4", "box": [0.5, 0.2, 0.2]}],
                     background={"kind": "glow", "color": "#3B82F6", "intensity": 0.4}))
    p = ly["panels"]
    assert p[0]["depth"] == "front" and p[0]["depth_auto"] is True                  # grid4's first place: beside the head
    assert p[1] == {**p[1], "kind": "image", "depth": "front", "frame": "window", "tilt": [0.0, 20.0, 0.0]} and "depth_auto" not in p[1]
    assert p[2]["kind"] == "video" and p[2]["box"] == [0.5, 0.2, 0.2] and "slot" not in p[2]
    assert ly["background"] == {"kind": "glow", "color": "#3b82f6", "intensity": 0.4}


@pytest.mark.parametrize("bad, msg", [
    ({"preset": "circle"}, "preset 'circle'"),
    ({"panels": [{"slide": 1}] * 7}, "1–6 panels"),
    ({"deck": None, "panels": [{"slide": 2}]}, "which picture"),
    ({"panels": [{"file_id": "x.docx"}]}, "a picture (png"),
    ({"panels": [{"slide": 0}]}, "start at 1"),
    ({"panels": [{"slide": 1, "at": 209.5}]}, "at least 1 s"),
    ({"pills": [{"text": "x", "near": 5}]}, "near 5"),
    ({"background": "plasma"}, "background.kind"),
    ({"background": {"kind": "video"}}, "give file_id"),
    ({"preset": "pair", "panels": [{"slide": 1}, {"slide": 2}, {"slide": 3}]}, "has 2 places"),
    ({"panels": [], "pills": [], "background": "none"}, "give panels, pills or a background"),
])
def test_stage_errors_say_what_is_wrong(bad, msg):
    with pytest.raises(ValueError) as e:
        _one(_stage(**bad))
    assert msg in str(e.value), str(e.value)


def test_stage_edit_moves_its_panels_and_pills_and_a_cut_remaps_them():
    from app.tools import stage_layer, video
    ly = _one(_stage(pills=[{"text": "A", "at": 190, "until": 200}]))
    [moved] = video.apply_layer_changes([ly], {"layers": [{"id": ly["id"], "at": 240, "until": 270}]}, "", [])
    assert [p["at"] for p in moved["panels"]] == [240.35, 240.53, 240.71, 240.89] and moved["pills"][0]["at"] == 250
    assert moved["pills"][0]["until"] == 260
    cut = stage_layer.recut(ly, (180, 205), lambda t: None if 190 <= t < 195 else (t if t < 190 else t - 5))
    assert cut["pills"][0] == {**cut["pills"][0], "at": 180, "until": 195}           # 190 was cut out → the stage's start
    assert {p["until"] for p in cut["panels"]} == {205}


# ---------- geometry ----------

def test_corners_project_the_tilted_rectangle():
    from app.tools import stage_layer as sl
    flat = sl.corners(400, 200, (0, 0, 0), f=3000)
    assert flat == [[-200, -100], [200, -100], [-200, 100], [200, 100]]
    ul, ur, ll, lr = sl.corners(400, 200, (0, 20, 0), f=3000)               # ry + = the right edge back: shorter
    assert (lr[1] - ur[1]) < (ll[1] - ul[1]) and ur[0] < 200
    ul, ur, ll, lr = sl.corners(400, 200, (15, 0, 0), f=3000)               # rx + = the top edge back: narrower
    assert (ur[0] - ul[0]) < (lr[0] - ll[0])
    ul, ur, ll, lr = sl.corners(400, 200, (0, 0, 10), f=3000)               # rz + = clockwise on screen
    assert ur[1] > ul[1]


def test_corner_pin_maps_the_padded_canvas_so_the_card_lands_on_its_projection():
    """Corner Pin maps the layer's whole source (the padded canvas) onto the four points (10-26: with the card's own
    corners as the points every panel came out 72 % of its size). The homography canvas → pins must put the card's
    corners where the tilted card projects — for every preset place and every entrance key."""
    import cv2
    import numpy as np
    from app.tools import stage_layer as sl
    for preset, slots in sl.PRESETS.items():
        ly = _one(_stage(preset=preset, panels=[{"slide": k + 1} for k in range(len(slots))]))
        for k in range(len(slots)):
            g = sl.panel_geometry(ly, k, 1920, 1080, 16 / 9)
            cw, ch, pad = g["canvas"][0], g["canvas"][1], g["pad"]
            src = np.float32([[0, 0], [cw, 0], [0, ch], [cw, ch]])
            for pins in [g["pins"]] + [kf[1:5] for kf in g["keys"]]:
                M = cv2.getPerspectiveTransform(src, np.float32(pins))
                card = np.float32([[[pad, pad], [pad + g["w"], pad], [pad, pad + g["h"]], [pad + g["w"], pad + g["h"]]]])
                got = cv2.perspectiveTransform(card, M)[0]
                assert ((got >= -1) & (got <= [cw + 1, ch + 1])).all(), (preset, k, got)   # inside the layer's bounds
            M = cv2.getPerspectiveTransform(src, np.float32(g["pins"]))
            got = cv2.perspectiveTransform(np.float32([[[pad, pad], [pad + g["w"], pad + g["h"]]]]), M)[0]
            want = np.float32(sl.corners(g["w"], g["h"], sl.place(ly, k, 1920, 1080)["tilt"], f=sl.FOCAL * 1920)) + [cw / 2, ch / 2]
            assert np.abs(got - want[[0, 3]]).max() < 0.6, (preset, k, got, want)


def test_auto_preset_follows_the_speaker_and_faces_push_panels_behind():
    from app.tools import stage_layer as sl
    assert sl.auto_preset(4, None) == "grid4" and sl.auto_preset(4, {"cx": 0.5}) == "grid4"
    assert sl.auto_preset(4, {"cx": 0.32}) == "right_grid4" and sl.auto_preset(2, {"cx": 0.7}) == "left_stack"
    assert sl.auto_preset(6, {"cx": 0.2}) == "right_grid6" and sl.auto_preset(1, {"cx": 0.75}) == "single_left"
    ly = _one(_stage(preset="grid4"))
    head = [0.3, 0.0, 0.62, 0.7]
    assert sl.covers(sl.panel_box(ly, 0, 1920, 1080, 16 / 9), head) > 0.04        # top-left place on a left-ish head
    assert sl.covers(sl.panel_box(ly, 1, 1920, 1080, 16 / 9), head) == 0.0


# ---------- the builder output and the depth order ----------

def _built(raw, total=600.0):
    from app.tools import stage_layer as sl
    ly = _one(raw)
    ly["_art"] = {"cards": {k: {"file": f"stage-c{k}.png", "aspect": 16 / 9} for k in range(len(ly["panels"]))},
                  "aspects": {k: 16 / 9 for k in range(len(ly["panels"]))},
                  "bg": {"sprites": [{"file": "stagebg-base.png", "pos": [960, 540], "scale": 200.0, "opacity": 100}]}}
    ly["_preset"] = sl.resolved_preset({**ly, "preset": "grid4" if ly["preset"] == "auto" else ly["preset"]})
    for k, p in enumerate(ly["panels"]):
        p.setdefault("depth", sl.PRESETS[ly["_preset"]][p["slot"] - 1][4] if p.get("slot") else "front")
    return ly, sl.items(ly, 1920, 1080, total, [0.2, 0.5, 0.9], fps=30.0)


def test_builder_groups_and_draws_behind_parts_first():
    from app.tools import stage_layer as sl
    ly, items = _built(_stage(pills=[{"text": "Claude Desktop", "near": 1}], background="glow"))
    assert [x["id"] for x in items] == ["L1.b", "L1.f", "L1.t1"]
    b, f, t = items
    assert b["stage"]["depth"] == "behind" and [x["role"] for x in b["stage"]["parts"]] == ["bg", "panel", "panel"]
    assert f["stage"]["depth"] == "front" and [x["role"] for x in f["stage"]["parts"]] == ["panel", "panel"]
    assert (b["w"], b["h"], b["pos"], b["at"], b["dur"]) == (1920, 1080, [960, 540], 180, 30)
    assert b["anim"] == {"in": "fade", "out": "fade"} and f["anim"]["in"] == "none"
    assert [x["t0"] for x in b["stage"]["parts"][1:]] == [0.71, 0.89] and b["stage"]["parts"][1]["slide"][0] < 0
    assert t["stage"]["role"] == "pill" and t["stage"]["text"] == "Claude Desktop" and t["anim"]["out"] == "pop"
    assert all(sl.ITEM_RE.match(x["name"]) for x in items), [x["name"] for x in items]
    assert sl.behind_ids(ly) == {"L1.b"}


def test_a_panel_with_its_own_times_is_its_own_layer_in_depth_order():
    from app.tools import stage_layer as sl
    ly, items = _built(_stage(panels=[{"slide": 1, "depth": "behind", "at": 190, "until": 200}, {"slide": 2, "depth": "front"},
                                      {"slide": 3, "depth": "behind"}, {"slide": 4, "depth": "front", "until": 205}],
                              background="none", preset="grid4"))
    assert [x["id"] for x in items] == ["L1.b", "L1.p1", "L1.p2", "L1.p4"]
    assert [x["stage"]["depth"] for x in items] == ["behind", "behind", "front", "front"]   # behind ones under the front ones
    p1 = items[1]
    assert (p1["at"], p1["dur"], p1["anim"]) == (190, 10, {"in": "slide", "out": "slide"}) and p1["stage"]["role"] == "panel"
    assert sl.behind_ids(ly) == {"L1.b", "L1.p1"} and sl.behind_span(ly) == [180.35 + 0.18 * 2, 210]


def test_speaker_goes_right_after_the_last_behind_part(tmp_path, monkeypatch):
    from app.tools import deck_matte as dm, stage_layer as sl
    ly, items = _built(_stage(pills=[{"text": "A"}]))
    D = {"fps": 25.0, "total": 600.0, "layers": [{"id": "L5", "name": "Layer L5 – logo", "at": 0, "dur": 600, "pos": [9, 9],
                                                 "w": 9, "h": 9, "elements": []}] + items}
    clip = tmp_path / "m.mp4"
    clip.write_bytes(b"x")
    calls = []

    async def fake(src, media, fa, n, fps):
        calls.append((media, fa, n))
        return clip, {"cached": False}
    monkeypatch.setattr(dm, "matte", fake)
    monkeypatch.setattr(dm, "available", lambda: None)
    names, notes = asyncio.run(sl.add_speakers(D, tmp_path, "footage.mp4", "abc.mp4", [ly]))
    assert [x["id"] for x in D["layers"]] == ["L5", "L1.b", "L1.speaker", "L1.f", "L1.t1"] and not notes
    sp = D["layers"][2]
    assert sp["name"] == dm.SPEAKER + "L1" and sp["speaker"]["at"] == 180 and sp["speaker"]["dur"] == 30
    assert calls == [("abc.mp4", 4500, 750)] and names == [sp["speaker"]["file"]]
    monkeypatch.setattr(dm, "available", lambda: "MediaPipe isn't installed on this server")
    D2 = {"fps": 25.0, "total": 600.0, "layers": [dict(x) for x in items]}
    names, notes = asyncio.run(sl.add_speakers(D2, tmp_path, "footage.mp4", "abc.mp4", [ly]))
    assert names == [] and "over you" in notes[0] and not any(x.get("speaker") for x in D2["layers"])


def test_live_matte_part_flags_the_behind_clips_and_keeps_a_decks_spans():
    from app.tools import stage_layer as sl
    ly = _one(_stage(background="glow"))
    for p in ly["panels"]:
        p["depth"] = "front"
    spec = {"story": {"layers": [ly]}}
    scene = [{"id": "L1.b"}, {"id": "L1.f"}, {"id": "L1.t1"}, {"id": "L7"}]
    m = sl.matte_part(spec, scene, 1797.0, None)
    assert m["spans"] == [[180.0, 210.0]] and m["model"] and [e.get("behind", False) for e in scene] == [True, False, False, False]
    deck = {"spans": [[200.0, 260.0]], "model": "x", "semantic": "y", "levels": [0.3, 0.7], "ema": 0.5, "motion": None}
    m2 = sl.matte_part(spec, [{"id": "L1.b"}], 1797.0, deck)
    assert m2["spans"] == [[180.0, 260.0]] and m2["model"] == "x"
    assert sl.matte_part({"story": {"layers": []}}, [], 100.0, deck) is deck


def test_live_cycles_are_known_from_the_entrance_and_the_period():
    from app.tools import stage_layer as sl
    pf = round(sl.PERIOD_S * 30)
    assert sl.live_cycle("Layer L29.b – stage behind (glow + 2 panels)", 30.0, 44.6) == (47, pf)
    assert sl.live_cycle("Layer L29.f – stage front (2 panels, still)", 30.0, 30) == (32, 1)
    assert sl.live_cycle("Layer L29.t1 – stage pill “A”", 30.0, 10.8) == (13, 1)
    assert sl.live_cycle("Layer L3 – chapter", 30.0, 10) is None


def test_background_sprites_loop_and_cards_are_rounded(tmp_path):
    from PIL import Image
    from app.tools import stage_layer as sl
    made = {}

    def emit(tag, key, make):
        im = make()
        made[tag] = im
        return f"{tag}.png"
    for kind in ("glow", "particles", "halftone", "gradient"):
        made.clear()
        bg = sl.background_sprites({"kind": kind}, [0.9, 0.3, 0.2], 640, 360, 30.0, emit)
        assert bg["sprites"][0]["file"] == "base.png" and made["base"].size == (320, 180)
        for s in bg["sprites"]:
            assert set(s) <= {"file", "pos", "scale", "opacity", "orbit", "rot", "spin", "twinkle", "breathe", "slide", "over"}
        if kind == "halftone":
            pitch = next(s for s in bg["sprites"] if s.get("slide"))["slide"]
            assert pitch[0] == pitch[1] < 0 and made["halftone"].size == (640 - pitch[0], 360 - pitch[1])
    src = tmp_path / "s.png"
    Image.new("RGB", (1920, 1080), (200, 30, 40)).save(src)
    card = sl.card_image(src, 400, 225, 50, "plain")
    assert card.size == (500, 325) and card.getpixel((0, 0))[3] == 0 and card.getpixel((50, 50))[3] < 60   # padded, rounded
    assert card.getpixel((250, 160))[:3] == (200, 30, 40) and card.getpixel((250, 160))[3] == 255


# ---------- end to end: the engine project and the live clips ----------

def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_stage_in_the_engine_project_draws_tilted_panels_and_loops(tmp_path, put_file):
    import hashlib
    import numpy as np
    from PIL import Image
    from app.storage import output_path
    from app.tools import live_layers, stage_layer as sl, video
    clip = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x404040:size=640x360:rate=25", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=48000", "-t", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(clip)], check=True)
    fid = put_file(clip)
    red, green = tmp_path / "r.png", tmp_path / "g.png"
    Image.new("RGB", (800, 450), (230, 20, 20)).save(red)
    Image.new("RGB", (800, 450), (20, 200, 40)).save(green)
    pr, pg = put_file(red), put_file(green)
    layer = {"type": "stage", "at": 2, "until": 28, "preset": "pair", "background": "glow",
             "panels": [{"file_id": pr, "depth": "behind", "frame": "none"}, {"file_id": pg, "depth": "front", "frame": "none"}],
             "pills": [{"text": "összehasonlítás"}]}
    res = asyncio.run(video.overlay({"file_id": fid, "layers": [layer], "render": False}))
    assert [x["text"] for x in res["layers"]] == ["stage (2 panels, glow background, 1 pill)"]
    pid = res["file_id"]
    d = json.loads(output_path(pid).read_text("utf-8"))
    sc = live_layers.scene_from(d, proxy=None, assets=video.assets_of(pid))
    by = {e["id"]: e for e in sc["layers"]}
    assert {"L1.b", "L1.p2", "L1.t1"} <= set(by) and all(not by[k]["unsupported"] for k in ("L1.b", "L1.p2", "L1.t1"))
    assert by["L1.b"]["z"] < by["L1.p2"]["z"] < by["L1.t1"]["z"]                     # behind group under the front panel
    assert by["L1.b"]["cycle"][1] == round(sl.PERIOD_S * 25) and by["L1.t1"]["cycle"][1] == 1
    work = tmp_path / "w"
    work.mkdir()
    proj = video.stage(pid, work)

    def frame(t):
        out = work / f"f{t}.png"
        subprocess.run([video.effect_cli(), "render-frame", "--project", proj.name,
                        "--comp", "Video", "--time", f"{t:.6f}", "--out", out.name], cwd=work, check=True, capture_output=True)
        return out
    W, H = 640, 360
    a = np.asarray(Image.open(frame(10.0)).convert("RGB")).astype(int)
    g = sl.PRESETS["pair"]
    for (cx, cy, _w, _t, _d), col in zip(g, ((230, 20, 20), (20, 200, 40))):
        px = a[int(cy * H), int(cx * W)]
        assert np.abs(px - col).max() < 40, (cx, cy, px)                              # each panel where its place is
    f0 = round(10.0 * 25)
    h1 = hashlib.md5(Image.open(frame(f0 / 25)).tobytes()).hexdigest()
    h2 = hashlib.md5(Image.open(frame((f0 + round(sl.PERIOD_S * 25)) / 25)).tobytes()).hexdigest()
    h3 = hashlib.md5(Image.open(frame((f0 + 7) / 25)).tobytes()).hexdigest()
    assert h1 == h2 != h3                                                               # one period later: the same frame
