"""The live preview's scene (app/tools/live_scene.py, plan20261026_live_preview.md lane L1): the footage ranges on the
copies that exist (a pending cut plays at once from the parent's copy), the colour tables (ffmpeg vf_eq, exactly), the
footage transform read from the engine project, the route. The browser side (live-player.js) is measured in Chrome:
.claude/qa/lp-footage/ (the engine's own valueAtTime vs the JS evaluator: 937 frames, ≤ 0.0007 px)."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from app.tools import live_scene as ls

FPS = 29.993
MAIN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.mov"
CLIP = "cccccccccccccccccccccccccccccccc.mov"
EQ = {"brightness": 0.05, "contrast": 1.08, "saturation": 1.35}


def base(keep=None, adjust=None, **kw):
    b = {"file_id": MAIN, "keep": keep, "adjust": adjust or {}, "fps": FPS, "duration": 1820.974,
         "plain": {"duration": 1815.974}, "end_card": {"title": "x", "dur": 5.0}}
    b.update(kw)
    return b


def copy(cid, keep=None, adjust=None, own=False):
    return {"id": cid, "keep": keep, "adjust": adjust or {}, "auto": None, "w": 960, "h": 540, "own": own}


# ---------- the colour tables ----------

def _ffmpeg_eq(vf: str) -> tuple[list[int], list[int]]:
    """ffmpeg's own eq on every code: a 256×8 yuv444p frame whose column i is Y = U = V = i."""
    row = bytes(range(256))
    frame = (row * 8) * 3
    out = subprocess.run(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "yuv444p", "-s", "256x8", "-i", "-",
                          "-vf", vf, "-f", "rawvideo", "-pix_fmt", "yuv444p", "-"], input=frame, capture_output=True,
                         check=True).stdout
    return list(out[:256]), list(out[256 * 8:256 * 8 + 256])


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg")
@pytest.mark.parametrize("rec", [EQ, {"brightness": -0.2, "contrast": 0.7}, {"saturation": 0.4}, {"brightness": -0.33},
                                 {"gamma": 1.3, "contrast": 1.1, "brightness": 0.02}, {"gamma": 0.8, "saturation": 1.2}])
def test_eq_tables_are_ffmpegs_eq(rec):
    """The tables ARE vf_eq (process_c and the create_lut path): every 8-bit code, luma and chroma, bit for bit."""
    from app.tools import video_fx
    vf = next(f for f in video_fx.chain(rec, None) if f.startswith("eq="))       # the chain's own eq string
    y, c = _ffmpeg_eq(vf)
    t = ls.eq_luts(rec)
    assert t["y"] == y
    assert t["c"] == c


def test_inverse_table_undoes_the_baked_eq():
    t = ls.eq_luts(EQ)
    inv = ls.invert_lut(t["y"])
    for x in range(16, 236):                         # where the baked eq didn't clip, its codes come back within half a code
        if 0 < t["y"][x] < 255:
            assert abs(inv[t["y"][x]] - x) <= 0.5
    invc = ls.invert_lut(t["c"])
    for x in range(16, 241):
        if 0 < t["c"][x] < 255:
            assert abs(invc[t["c"][x]] - x) <= 0.5
    assert ls._clips(EQ)                             # (this one clips the top of the luma: its undo isn't exact there)
    assert not ls._clips({"contrast": 0.9})


def test_colour_modes():
    neutral = ls.color_params(EQ, {})
    assert neutral["mode"] == "apply" and neutral["exact"] and neutral["pre"] is None and neutral["eq"]["y"][128] == 140
    same = ls.color_params(EQ, dict(EQ))
    assert same["mode"] == "none" and same["exact"] and same["eq"] is None
    diff = ls.color_params({"brightness": 0.1}, EQ)
    assert diff["mode"] == "diff" and diff["pre"] is not None and diff["eq"] is not None
    undo = ls.color_params({}, EQ)                   # colour taken back while the copy still has it: undone in the shader
    assert undo["mode"] == "diff" and undo["eq"] is None and undo["pre"] is not None
    lut = ls.color_params({"lut": "cinema", "contrast": 1.1}, {})
    assert lut["mode"] == "apply" and not lut["exact"] and "lut" in lut["approx"]
    stab = ls.color_params({"stabilize": True}, {})
    assert stab["mode"] == "apply" and "stabilize" in stab["approx"] and stab["eq"] is None


def test_auto_recipe_counts_with_its_measurement():
    r = ls.recipe_of({"auto": True, "contrast": 1.2}, {"gamma": 1.1, "levels": [0.05, 0.95]})
    assert r == {"gamma": 1.1, "levels": [0.05, 0.95], "contrast": 1.2}
    assert ls.recipe_of({"contrast": 1.2}, {"gamma": 1.1}) == {"contrast": 1.2}       # no auto: the old measurement is unused


# ---------- the footage ranges ----------

def test_one_range_on_the_copy_of_this_timeline_neutral_preferred():
    b = base(adjust=EQ)
    cs = [copy("baked.mp4", None, EQ, own=True), copy("neutral.mp4", None, {})]
    fp = ls.footage_plan(b, cs)
    assert fp["ranges"] == [{"t0": 0.0, "t1": 1815.974, "copy": 0, "src": 0.0}] and fp["copies"][0]["id"] == "neutral.mp4"
    assert ls.footage_plan(b, cs, prefer="own")["copies"][0]["id"] == "baked.mp4"
    assert ls.footage_plan(b, [copy("other.mp4", None, {"contrast": 1.5})])["copies"][0]["id"] == "other.mp4"   # any: diff


def test_pending_cut_plays_from_the_parents_copy_at_once():
    """The new cut (05:00–05:20 out): no copy of the new timeline yet → two ranges in the parent's uncut copy."""
    keep = [[0.0, 300.0033], [320.008, 1815.974]]
    fp = ls.footage_plan(base(keep=keep, proxy_pending=True), [copy("parent.mp4", None)])
    r = fp["ranges"]
    assert [(x["t0"], x["t1"], x["src"], x["copy"]) for x in r] == [(0.0, 300.0033, 0.0, 0), (300.0033, 1795.9693, 320.008, 0)]
    assert r[1]["join"] == "cut" and "xfade" not in r[1] and fp["missing"] == [] and fp["end"] == 1795.9693


def test_pending_cut_on_an_already_cut_parent():
    old = [[0.0, 89.9877], [92.9884, 219.9847], [223.9856, 1815.974]]
    new = [[0.0, 89.9877], [92.9884, 150.0], [160.0, 219.9847], [223.9856, 1815.974]]
    fp = ls.footage_plan(base(keep=new), [copy("parent.mp4", old)])
    r = fp["ranges"]
    # the parent's copy: [0, 89.99) [89.99, 216.98) [216.98, …) — the new cut is the only new joint
    assert len(r) == 2 and r[0]["t0"] == 0.0 and r[0]["src"] == 0.0
    assert r[0]["t1"] == pytest.approx(89.9877 + (150.0 - 92.9884), abs=1e-5)
    assert r[1]["t0"] == pytest.approx(r[0]["t1"], abs=1e-6)
    assert r[1]["src"] == pytest.approx(89.9877 + (160.0 - 92.9884), abs=1e-5)       # the parent copy's time of 160.0
    assert r[1]["t1"] == pytest.approx(fp["end"], abs=1e-6)


def test_the_most_covering_copy_wins_and_cut_back_material_comes_from_another():
    """Undoing a cut: the parent's copy lacks 300–320; the uncut neutral copy has all → one copy, two ranges."""
    keep = [[0.0, 1815.974]]
    fp = ls.footage_plan(base(keep=[[0.0, 600.0], [610.0, 1815.974]]),
                         [copy("parent.mp4", [[0.0, 300.0], [320.0, 1815.974]]), copy("whole.mp4", None)])
    assert {c["id"] for c in fp["copies"]} == {"whole.mp4"} and len(fp["ranges"]) == 2 and not fp["missing"]
    fp2 = ls.footage_plan(base(keep=keep), [copy("parent.mp4", [[0.0, 300.0], [320.0, 1815.974]])])
    assert [r["copy"] for r in fp2["ranges"]] == [0, None, 0] and fp2["missing"] == [[300.0, 320.0, None]]


def test_inserted_clip_without_a_copy_is_missing_and_one_with_its_match_is_used():
    keep = [[0.0, 100.0], {"a": 2.0, "b": 12.0, "src": CLIP, "join": "dissolve", "match": {"gamma": 1.1}},
            {"a": 100.0, "b": 1815.974, "join": "dissolve"}]
    fp = ls.footage_plan(base(keep=keep), [copy("parent.mp4", None)])
    assert [r["copy"] for r in fp["ranges"]] == [0, None, 0]
    assert fp["missing"] == [[100.0, 110.0, CLIP]] and fp["ranges"][1]["clip"] == CLIP
    assert fp["ranges"][2]["join"] == "dissolve" and fp["ranges"][2]["xfade"] == pytest.approx(12 / FPS, abs=1e-6)
    other = ls.footage_plan(base(keep=keep), [copy("parent.mp4", None),
                                              copy("withclip.mp4", [[0.0, 50.0], {"a": 0.0, "b": 20.0, "src": CLIP, "match": {"gamma": 1.3}}])])
    assert other["missing"]                                         # another colour match: not the same picture
    good = ls.footage_plan(base(keep=keep), [copy("parent.mp4", None),
                                             copy("withclip.mp4", [[0.0, 50.0], {"a": 0.0, "b": 20.0, "src": CLIP, "match": {"gamma": 1.1}}])])
    assert not good["missing"] and good["ranges"][1]["src"] == pytest.approx(52.0)


def test_the_copy_with_the_dissolve_in_it_needs_no_crossfade():
    keep = [[0.0, 100.0], {"a": 200.0, "b": 1815.974, "join": "dissolve"}]
    fp = ls.footage_plan(base(keep=keep), [copy("own.mp4", keep, own=True)])
    assert len(fp["ranges"]) == 1 and "join" not in fp["ranges"][0]


# ---------- the transform (the engine project) ----------

T = ls.TICKS


def _key(t, v, interp="Bezier", infl=0.6):
    e = [{"speed": 0.0, "influence": infl}] * 3
    return {"time": round(t * T), "value": {"t": "Vec3", "v": v + [0.0]}, "in_interp": interp, "out_interp": interp,
            "in_ease": e if interp == "Bezier" else [], "out_ease": e if interp == "Bezier" else [],
            "spatial_in": [0.0, 0.0, 0.0], "spatial_out": [0.0, 0.0, 0.0]}


def _project(scale_keys, pos_keys=None):
    tf = {"node": "Group", "match": "transform", "children": [
        {"node": "Prop", "match": "anchor", "value": {"t": "Vec3", "v": [480.0, 270.0, 0.0]}},
        {"node": "Prop", "match": "position", "value": {"t": "Vec3", "v": [960.0, 540.0, 0.0]}, **({"keys": pos_keys} if pos_keys else {})},
        {"node": "Prop", "match": "scale", "value": {"t": "Vec3", "v": [200.0, 200.0, 0.0]}, "keys": scale_keys},
        {"node": "Prop", "match": "rotation", "value": {"t": "Scalar", "v": 0.0}},
        {"node": "Prop", "match": "opacity", "value": {"t": "Scalar", "v": 100.0}}]}
    remap = {"node": "Prop", "match": "timeRemap", "keys": [{"time": 0, "value": {"t": "Scalar", "v": 0.0}},
                                                            {"time": round(10 * T), "value": {"t": "Scalar", "v": 10.0}}]}
    lay = {"name": "Footage", "source": {"type": "Footage", "item": 2}, "props": {"children": [remap, tf]}}
    return {"items": {"1": {"name": "Video", "kind": {"type": "Comp", "width": 1920, "height": 1080,
                                                      "frame_rate": {"num": 29993, "den": 1000}, "background": [0.1, 0.2, 0.3],
                                                      "layers": [{"name": "Caption 1", "source": {"type": "Text"}}, lay]}},
                      "2": {"name": "footage-x.mp4", "kind": {"type": "Footage", "width": 960, "height": 540, "path": "footage-x.mp4"}}}}


def test_footage_transform_from_the_project():
    tr = ls.footage_transform(_project([_key(0, [200.0, 200.0]), _key(4, [200.0, 200.0]), _key(4.6, [240.0, 240.0]),
                                        _key(8, [240.0, 240.0]), _key(8.6, [200.0, 200.0])]))
    assert tr["comp"] == [1920, 1080] and tr["src"] == [960, 540] and tr["bg"] == [0.1, 0.2, 0.3]
    assert tr["rest"]["scale"] == [200.0, 200.0] and tr["rest"]["anchor"] == [480.0, 270.0]
    assert tr["moving"] == [[4.0, 8.6]] and tr["remap"] == [[0.0, 0.0], [10.0, 10.0]]
    assert ls.value_at(tr, "scale", 2.0) == pytest.approx([200, 200]) and ls.value_at(tr, "scale", 6.0) == pytest.approx([240, 240])
    mid = ls.value_at(tr, "scale", 4.3)[0]                            # the ease: symmetric, half-way in the middle
    assert mid == pytest.approx(220.0, abs=1e-6)
    early = ls.value_at(tr, "scale", 4.06)[0] - 200                   # slow start (60 % influence): far below linear
    assert 0 < early < 0.25 * 40 * 0.1


def test_hold_and_linear_keys():
    tr = ls.footage_transform(_project([_key(0, [100.0, 100.0], "Hold"), _key(2, [150.0, 150.0], "Hold")],
                                       [_key(0, [0.0, 0.0], "Linear"), _key(2, [100.0, 50.0], "Linear")]))
    assert ls.value_at(tr, "scale", 1.99) == [100.0, 100.0] and ls.value_at(tr, "scale", 2.0) == [150.0, 150.0]
    assert ls.value_at(tr, "position", 0.5) == pytest.approx([25.0, 12.5])


def test_no_footage_layer_is_an_error():
    p = _project([_key(0, [200.0, 200.0])])
    p["items"]["1"]["kind"]["layers"] = p["items"]["1"]["kind"]["layers"][:1]
    with pytest.raises(ValueError):
        ls.footage_transform(p)


def test_revision_ignores_the_signed_tokens():
    a = {"v": 1, "footage": {"copies": [{"url": "/files/x.mp4?t=1.abc"}]}}
    b = {"v": 1, "footage": {"copies": [{"url": "/files/x.mp4?t=2.def&u=w"}]}}
    assert ls.revision(a) == ls.revision(b)
    assert ls.revision(a) != ls.revision({"v": 1, "footage": {"copies": [{"url": "/files/y.mp4?t=1.abc"}]}})


# ---------- the scene and the route ----------

@pytest.fixture()
def project(tmp_path):
    """A version on a cut timeline, pending, whose parent's (uncut, neutral) copy exists; the project JSON with the
    footage layer."""
    from app.storage import input_path, output_path, write_meta
    pid, parent, cp = "d" * 32 + ".ecproj", "e" * 32 + ".ecproj", "f" * 32 + ".mp4"
    input_path(MAIN).write_bytes(b"orig")
    output_path(cp).write_bytes(b"copy")
    key = json.dumps({"v": 2, "side": 960, "budget": 400, "adjust": {}, "keep": None}, sort_keys=True)
    write_meta(MAIN, extra={"proxies": {key: {"media_id": cp, "asset": "footage-ffff.mp4", "proxy": {"w": 960, "h": 540}}}})
    output_path(pid).write_text(json.dumps(_project([_key(0, [200.0, 200.0])])), "utf-8")
    output_path(parent).write_text("{}", "utf-8")
    b = base(keep=[[0.0, 300.0], [320.0, 1815.974]], adjust=EQ, proxy_pending=True, media_id=cp, asset="footage-ffff.mp4",
             proxy={"w": 960, "h": 540}, width=1920, height=1080, duration=1800.974)
    write_meta(parent, extra={"spec": {"kind": "overlay", "base": base(media_id=cp)}})
    write_meta(pid, label="v.ecproj", source_file_id=parent,
               extra={"spec": {"kind": "overlay", "base": b, "opts": {}}, "video": {"width": 1920, "height": 1080, "duration": 1800.974}})
    yield pid
    for p in (output_path(pid), output_path(parent), output_path(cp), input_path(MAIN)):
        p.unlink(missing_ok=True)


def test_build_scene_for_a_pending_cut(project):
    import asyncio
    sc = asyncio.run(ls.build(project))
    f = sc["footage"]
    assert sc["W"] == 960 and sc["H"] == 540 and sc["frames"] == round(1800.974 * FPS) and f["pending"]
    assert [(r["t0"], r["src"]) for r in f["ranges"]] == [(0.0, 0.0), (300.0, 320.0)]
    assert all(r["url"].startswith("/files/" + "f" * 32 + ".mp4?t=") for r in f["ranges"])
    c = f["copies"][0]
    assert c["color"]["mode"] == "apply" and c["color"]["exact"] and c["baked"] == {}
    assert f["hold_from"] == f["end"] == 1795.974 and f["adjust"] == EQ
    assert f["transform"]["rest"]["scale"] == [200.0, 200.0]
    assert isinstance(sc["layers"], list) and sc["rev"]


def test_live_json_route_is_signed(client, project):
    from app.tools import preview_serve as ps
    assert client.get(f"/api/video/{project}/live.json?t=1.bad").status_code == 403
    r = client.get(f"/api/video/{project}/live.json{ps.signed(project)}")
    assert r.status_code == 200 and r.json()["pid"] == project and r.headers["etag"].strip('"') == r.json()["rev"]
    page = client.get(f"/live/{project}")
    assert page.status_code == 200 and "/static/live-player.js" in page.text and "live.json?t=" in page.text
    assert client.get("/live/" + "0" * 32 + ".ecproj").status_code == 404
