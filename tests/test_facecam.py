"""Facecam layout (app/tools/facecam.py): the speaker window on a content layer — the schema, the builder data (the
footage keys and the mask), the engine project's mask read for the live preview, the final's classification."""
import copy
import math

import pytest

from app.tools import facecam, fast_render as fr, live_layers, live_scene as ls, video

W, H = 1920, 1080


# ---------- the schema ----------

def test_speaker_defaults_and_fields():
    sp = facecam.opts({}, "Layer L5")
    assert sp["shape"] == "circle" and sp["corner"] == "bl" and sp["ring"] == "accent" and sp["shadow"] is True
    sp = facecam.opts({"shape": "rounded", "corner": "top-right", "size": "large", "ring": "#ff0000", "shadow": False,
                       "focus": [0.3, 0.4], "label": "  Coming   Up... "}, "L")
    assert sp["shape"] == "rounded" and sp["corner"] == "tr" and sp["size"] == 0.5 and sp["ring"] == "#ff0000"
    assert sp["shadow"] is False and sp["focus"] == [0.3, 0.4] and sp["label"] == "Coming Up..."
    assert facecam.opts({"corner": [0.8, 0.2], "ring": False}, "L")["corner"] == [0.8, 0.2]
    assert facecam.opts({"ring": False}, "L")["ring"] is None
    assert facecam.opts("rect", "L")["shape"] == "rect"
    assert facecam.opts(False, "L") is None and facecam.opts("off", "L") is None
    assert facecam.opts({"size": 5}, "L")["size"] == 0.7                     # clamped


@pytest.mark.parametrize("bad", [{"shape": "star"}, {"corner": "middle"}, {"corner": [2]}, {"ring": 5}, {"wobble": 1},
                                 {"aspect": "x:y"}, 7])
def test_speaker_errors_name_the_fix(bad):
    with pytest.raises(ValueError) as e:
        facecam.opts(bad, "Layer L5")
    assert "Layer L5 (speaker)" in str(e.value)


def test_edit_keeps_what_is_not_given():
    old = facecam.opts({"shape": "rounded", "corner": "br", "label": "Next"}, "L")
    new = facecam.opts({"corner": "tl"}, "L", old)
    assert new["shape"] == "rounded" and new["corner"] == "tl" and new["label"] == "Next"
    assert "label" not in facecam.opts({"label": ""}, "L", old)


def test_layer_from_fullscreen_with_speaker():
    notes = []
    ly = video.layers_from([{"type": "fullscreen", "at": 10, "until": 30, "image": {"stock_video": "network"},
                             "speaker": {"corner": "br"}, "label": "Coming Up..."}], "", notes)[0]
    assert ly["type"] == "fullscreen" and ly["speaker"]["corner"] == "br" and ly["speaker"]["label"] == "Coming Up..."
    el = ly["chart"]["elements"][0]
    assert el["sound"] == "off"                                   # the voice runs on (a fullscreen video alone: replace)
    assert not [n for n in notes if "not a field" in n]
    plain = video.layers_from([{"type": "fullscreen", "at": 10, "dur": 5, "image": {"stock_video": "network"}}], "", [])[0]
    assert plain["chart"]["elements"][0]["sound"] == "replace" and "speaker" not in plain


def test_facecam_alias_and_edit():
    ly = video.layers_from([{"type": "facecam", "at": 5, "dur": 12, "image": "clip123.mp4"}], "", [])[0]
    assert ly["type"] == "fullscreen" and ly["speaker"]["shape"] == "circle"
    out = video.apply_layer_changes([ly], {"layers": [{"id": ly["id"], "speaker": {"shape": "rect"}}]}, "", [])[0]
    assert out["speaker"]["shape"] == "rect" and out["image"] == "clip123.mp4"
    off = video.apply_layer_changes([ly], {"layers": [{"id": ly["id"], "speaker": False}]}, "", [])[0]
    assert "speaker" not in off


def test_text_video_refuses_a_speaker_window():
    with pytest.raises(ValueError, match="your own footage"):
        facecam.no_speaker([{"id": "L1", "speaker": {"shape": "circle"}}])


# ---------- the geometry and the builder data ----------

def test_window_corners_and_captions():
    sp = facecam.opts({}, "L")
    w = facecam.window(sp, W, H)
    assert w["w"] == w["h"] == pytest.approx(0.36 * H) and w["r"] == pytest.approx(w["h"] / 2)
    assert w["cx"] == pytest.approx(0.04 * H + w["w"] / 2) and w["cy"] == pytest.approx(H - 0.04 * H - w["h"] / 2)
    up = facecam.window(sp, W, H, safe=880)                        # a bottom corner stays above the captions
    assert up["cy"] + up["h"] / 2 == pytest.approx(880)
    tr = facecam.window(facecam.opts({"corner": "tr", "shape": "rect"}, "L"), W, H, safe=880)
    assert tr["w"] == pytest.approx(tr["h"] * 16 / 9) and tr["cy"] - tr["h"] / 2 == pytest.approx(0.04 * H) and tr["r"] == 0


def test_plan_maps_the_face_into_the_window():
    sp = facecam.opts({"focus": [0.31, 0.42]}, "L")
    p = facecam.plan(sp, 100.0, 20.0, W, H, 883)
    win, reg = p["window"], p["win"]
    # the region's centre (the face, comp px at rest) lands on the window's centre: P + s·(f − C) = window centre
    for i, (f, c) in enumerate(((reg["cx"], W / 2), (reg["cy"], H / 2))):
        assert p["to"][i] + p["scale"] * (f - c) == pytest.approx(win["cx"] if i == 0 else win["cy"], abs=1e-2)
    assert reg["hh"] * p["scale"] == pytest.approx(win["h"] / 2, rel=1e-6) and reg["r"] == pytest.approx(reg["hh"])
    assert p["full"]["hw"] > W / 2 and p["full"]["hh"] > H / 2 and p["full"]["r"] == 0
    assert p["move"] == 0.7
    assert facecam.plan(sp, 0, 1.2, W, H)["move"] == pytest.approx(0.4)        # a short layer: a third each way


def test_region_stays_inside_the_footage():
    sp = facecam.opts({"focus": [0.0, 1.0]}, "L")
    reg = facecam.region(sp, facecam.window(sp, W, H), W, H)
    assert reg["cx"] - reg["hw"] >= -1e-6 and reg["cy"] + reg["hh"] <= H + 1e-6


def test_path_is_linear_in_its_parameters():
    a = {"cx": 960, "cy": 540, "hw": 980, "hh": 560, "r": 0}
    b = {"cx": 600, "cy": 450, "hw": 370, "hh": 370, "r": 370}
    pa, pb = facecam.path(a), facecam.path(b)
    u = 0.37
    mid = {k: a[k] + (b[k] - a[k]) * u for k in a}
    pm = facecam.path(mid)
    for key in ("vertices", "in_tangents", "out_tangents"):
        for va, vb, vm in zip(pa[key], pb[key], pm[key]):
            assert [x + (y - x) * u for x, y in zip(va, vb)] == pytest.approx(vm)
    assert facecam.params_of(pm) == pytest.approx([mid["cx"], mid["cy"], mid["hw"], mid["hh"], mid["r"]], abs=1e-3)
    assert facecam.params_of({"vertices": [[0, 0]] * 4}) is None


def test_layer_comps_puts_the_content_under_and_keys_the_footage():
    ly = video.layers_from([{"type": "fullscreen", "at": 10, "until": 30, "image": "clip.mp4",
                             "speaker": {"focus": [0.3, 0.4], "label": "Coming Up..."}},
                            {"type": "lower_third", "at": 40, "dur": 5, "name": "Anna"}], "", [])
    items, rows, problems = video.layer_comps(ly, W, H, 120.0, "en", [0.2, 0.6, 0.95], 883)
    main = next(it for it in items if it.get("facecam"))
    assert main["under"] is True and main["anim"] == {"in": "none", "out": "none"}
    fc = main["facecam"]
    assert fc["at"] == 10 and fc["dur"] == 20 and fc["scale"] < 1 and fc["win"]["r"] > 0
    names = [it["name"] for it in items]
    assert any("facecam ring" in n for n in names) and any("facecam label" in n for n in names)
    shadow = next(it for it in items if "facecam shadow" in it["name"])
    ring = next(it for it in items if "facecam ring" in it["name"])
    assert shadow["under"] is True and "under" not in ring
    assert ring["at"] == pytest.approx(10 + fc["move"]) and ring["at"] + ring["dur"] == pytest.approx(30 - fc["move"])
    assert ring["elements"][0]["stroke"] == [0.2, 0.6, 0.95] and ring["elements"][0]["color"] is None
    assert [r["id"] for r in rows].count(ly[0]["id"]) == 1          # one timeline row for the facecam layer
    assert not [p for p in problems if ly[0]["id"] in p]


def test_builder_has_the_mask_and_the_under_hook():
    js = (video.Path(video.__file__).parent / "video_builder.jsx").read_text("utf-8")
    assert "function facecamBase(fb, fc)" in js and '"ADBE Mask Atom"' in js and 'M.name = "Facecam"' in js
    assert "if (ly.under && D.base && base.length) L.moveAfter(base[0]);" in js


# ---------- the engine project → the live scene ----------

T = ls.TICKS


def _pkey(t, p, interp="Bezier"):
    e = [{"speed": 0.0, "influence": 0.6}]
    return {"time": round(t * T), "value": {"t": "Path", "v": {**facecam.path(p), "closed": True}}, "in_interp": interp,
            "out_interp": interp, "in_ease": e, "out_ease": e}


def _project(mask_keys, extra_layers=()):
    full = {"cx": 480, "cy": 270, "hw": 490, "hh": 280, "r": 0}
    masks = {"node": "Group", "match": "masks", "children": [
        {"node": "Group", "match": "mask", "name": "Facecam", "kind": {"kind": "Mask", "mode": "Add", "inverted": False},
         "enabled": True, "children": [
             {"node": "Prop", "match": "path", "value": {"t": "Path", "v": {**facecam.path(full), "closed": True}}, "keys": mask_keys},
             {"node": "Prop", "match": "feather", "value": {"t": "Vec2", "v": [1.5, 1.5]}}]}]}
    tf = {"node": "Group", "match": "transform", "children": [
        {"node": "Prop", "match": "anchor", "value": {"t": "Vec3", "v": [480.0, 270.0, 0.0]}},
        {"node": "Prop", "match": "position", "value": {"t": "Vec3", "v": [960.0, 540.0, 0.0]}},
        {"node": "Prop", "match": "scale", "value": {"t": "Vec3", "v": [200.0, 200.0, 0.0]}}]}
    lay = {"name": "Footage", "source": {"type": "Footage", "item": 2}, "props": {"children": [masks, tf]}}
    return {"items": {"1": {"name": "Video", "kind": {"type": "Comp", "width": 1920, "height": 1080,
                                                      "frame_rate": {"num": 30, "den": 1}, "layers": [lay, *extra_layers]}},
                      "2": {"name": "f.mp4", "kind": {"type": "Footage", "width": 960, "height": 540, "path": "f.mp4"}}}}


def test_scene_reads_the_mask_with_the_engine_interpolation():
    full = {"cx": 480, "cy": 270, "hw": 490, "hh": 280, "r": 0}
    win = {"cx": 300, "cy": 230, "hw": 186, "hh": 186, "r": 186}
    tr = ls.footage_transform(_project([_pkey(10, full), _pkey(10.7, win), _pkey(29.3, win), _pkey(30, full)]))
    m = tr["mask"]
    assert m["feather"] == 1.5 and m["spans"] == [[10.0, 30.0]] and len(m["keys"]) == 4
    assert m["keys"][1]["v"] == pytest.approx([300, 230, 186, 186, 186])
    # half-way through the shrink: the eased progress is ½ (symmetric ease) — every parameter half-way
    v = ls.value_at({"rest": {}, "keys": [{**k, "prop": "m"} for k in m["keys"]]}, "m", 10.35)
    assert v == pytest.approx([(a + b) / 2 for a, b in zip([480, 270, 490, 280, 0], [300, 230, 186, 186, 186])], abs=1e-6)


def test_scene_without_a_mask_and_with_a_foreign_one():
    p = _project([])
    p["items"]["1"]["kind"]["layers"][0]["props"]["children"] = p["items"]["1"]["kind"]["layers"][0]["props"]["children"][1:]
    assert ls.footage_transform(p)["mask"] is None
    q = _project([])
    q["items"]["1"]["kind"]["layers"][0]["props"]["children"][0]["children"][0]["children"][0]["value"]["v"]["vertices"] = [[0, 0]] * 4
    assert ls.footage_transform(q)["mask"] is None                # someone's own mask: not ours, the preview ignores it


def test_live_layers_flags_the_layers_under_the_footage():
    p = _project([])
    comp = {"type": "Comp", "item": 9}
    over = {"name": "Layer L2 – facecam ring", "source": comp, "in_point": 0, "out_point": 2 * T, "start_time": 0, "switches": {}}
    under = {"name": "Layer L1 – picture", "source": comp, "in_point": 0, "out_point": 2 * T, "start_time": 0, "switches": {}}
    layers = p["items"]["1"]["kind"]["layers"]
    p["items"]["1"]["kind"]["layers"] = [over, layers[0], under]
    p["items"]["1"]["kind"]["duration"] = 10 * T
    p["items"]["9"] = {"name": "x", "kind": {"type": "Comp", "width": 10, "height": 10, "layers": []}}
    sc = live_layers.scene_from(p, proxy=(960, 540), assets={})
    by = {e["name"]: e for e in sc["layers"]}
    assert by["Layer L1 – picture"].get("under") is True and not by["Layer L2 – facecam ring"].get("under")
    assert by["Layer L1 – picture"]["z"] < by["Layer L2 – facecam ring"]["z"]


# ---------- the final: the fast path keeps working ----------

def _info(under_a=10.0, under_b=30.0):
    foot = {"i": 3, "name": "Footage", "a": 0, "b": 60, "on": True, "video": True, "nul": False, "adj": False, "fx": 0,
            "normal": True, "file": "footage.mp4", "t0": [[960, 540], [200, 200]], "t1": [[960, 540], [200, 200]],
            "kv": [["ADBE Position", 10, [960, 540]], ["ADBE Position", 10.7, [400, 700]], ["ADBE Position", 29.3, [400, 700]],
                   ["ADBE Position", 30, [960, 540]], ["ADBE Scale", 10, [200, 200]], ["ADBE Scale", 10.7, [100, 100]],
                   ["ADBE Scale", 29.3, [100, 100]], ["ADBE Scale", 30, [200, 200]]]}
    base = {"on": True, "video": True, "nul": False, "adj": False, "fx": 0, "normal": True}
    return {"W": 1920, "H": 1080, "fps": 30, "dur": 60, "comp": "Video", "layers": [
        {**base, "i": 1, "name": "Layer L5.ring – facecam ring", "a": 10.7, "b": 29.3},
        {**base, "i": 2, "name": "Layer L9 – chart", "a": 40, "b": 45},
        foot,
        {**base, "i": 4, "name": "Layer L5 – picture", "a": under_a, "b": under_b}]}


def test_classify_allows_the_content_under_a_moving_footage():
    spec = {"kind": "overlay", "base": {"file_id": "x.mov"}}
    k = fr.classify(_info(), spec)
    assert k["ok"], k["why"]
    assert any(a <= 10 and 30 <= b for a, b in fr.spans(k["opaque"], 60, 30))
    under = next(x for x in k["gfx"] if x["name"] == "Layer L5 – picture")
    assert under not in k["persistent"]
    # a layer under the footage where the footage stands still: the fast path can't put it under — the old way
    assert not fr.classify(_info(5.0, 30.0), spec)["ok"]


def test_fm_sigma_matches_the_measured_engine_edge():
    # measured (eng/fit.py, 10-26, scale 1.044): feather 0 → σ 0.49, 3 → 1.51, 10 → 4.96 (layer px); the JS model
    js = (video.Path(video.__file__).parent.parent / "static" / "live-player.js").read_text("utf-8")
    assert "const FM_K = 0.5, FM_AA = 0.51" in js
    sig = lambda f, s: math.hypot(0.5 * f, 0.51 / s)                 # noqa: E731 — = fmSigma in live-player.js
    for f, want in ((0.0, 0.491), (3.0, 1.506), (10.0, 4.962)):
        assert sig(f, 1.044) == pytest.approx(want, abs=0.12)
