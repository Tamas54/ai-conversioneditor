"""Szakasz-gyorsítótáras előnézet (app/tools/preview_cache.py): a rács, a kockák, az ujjlenyomat (vágás, eltolás,
vízjel-fázis, réteg, akcentus / stílus / korrekció, beszúrt klip + áttűnés, zárókártya), a futamok, a tárolás (PG és
PG nélkül, LRU), és — ha a motor megvan — a futam-render kockapontossága."""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.tools import footage, preview_cache as pc, video

FPS = 30.0


def _subs(dur: float) -> list[dict]:
    """4 s-os feliratok 5 s-onként (szóidőkkel), a vágáspontokon szünettel."""
    out = []
    for k in range(int(dur // 5)):
        a = 5 * k + 0.5
        words = [round(a + 0.5 * i, 3) for i in range(6)]
        out.append({"start": a, "end": a + 4.0, "text": f"mondat {k} egy kettő három négy öt", "words": words})
    return out


def _layers() -> list[dict]:
    return [
        {"id": "L1", "type": "chart", "at": 3.5, "dur": 590.0, "chart": {"type": "graphic", "backdrop": 0, "elements": [
            {"shape": "circle", "x": 0.035, "y": 0.06, "r": 0.009, "fill": "red", "anim": "pulse"},
            {"shape": "text", "text": "TAMÁS TV", "x": 0.095, "y": 0.06, "size": 34}]}},
        {"id": "L2", "type": "lower_third", "at": 4.0, "dur": 6.0, "name": "Tamás", "role": "tesztadás", "side": "left"},
        {"id": "L3", "type": "chapter", "at": 121.0, "dur": 3.0, "text": "2. rész"},
        {"id": "L4", "type": "chart", "at": 400.0, "dur": 10.0, "position": [0.74, 0.47, 0.44, 0.62],
         "chart": {"type": "column", "title": "Videóméretek", "items": [{"label": "a", "value": 235}, {"label": "b", "value": 300}]}},
        {"id": "L5", "type": "stat", "at": 500.0, "dur": 10.0, "value": "207 MB", "label": "iPhone", "position": [0.74, 0.47, 0.44, 0.62]},
    ]


def _spec(dur: float = 600.0, fps: float = FPS, *, layers=None, subs=None, camera=None, card=None, keep=None,
          accent: str = "#ff6a00", style: str = "highlight", adjust=None) -> dict:
    base = {"file_id": "src.mov", "asset": "footage-a.mp4", "media_id": "proxy.mp4", "width": 1920, "height": 1080, "fps": fps,
            "duration": dur, "proxy": {"w": 960, "h": 540}, "audio": True, "hdr": "hlg"}
    if keep:
        base["keep"] = keep
        base["duration"] = footage.length(keep, dur)
    if adjust:
        base["adjust"] = adjust
    if card:
        plain = base["duration"]
        base.update(end_card=card, plain={"duration": plain, "asset": "footage-a.mp4", "media_id": "proxy.mp4"},
                    duration=round(plain + card["dur"], 3))
    subs = _subs(dur) if subs is None else subs
    return {"kind": "overlay", "base": base, "story": {"title": "T", "layers": copy.deepcopy(_layers() if layers is None else layers),
                                                       **({"subtitles": subs} if subs else {})},
            "opts": {"accent": accent, "subtitles": subs or None, "caption_style": style if subs else None,
                     "camera": video._footage_camera(camera) if camera else None}, "layer_source": "Ez egy magyar szöveg és a teszt."}


CAM = [{"at": 450, "zoom": 1.2, "to": [0.3, 0.33], "dur": 0.6}, {"at": 456, "zoom": 1, "dur": 0.6}]
CARD = {"title": "Tamás TV", "subtitle": "próba", "kicker": "", "dur": 5.0}


def _cut(spec: dict, f: float, t: float, src_dur: float = 600.0) -> dict:
    """Mint video._edit_overlay vágása: a keep az eredeti idejében, a rétegek / feliratok / kamera video._recut-tal."""
    s = copy.deepcopy(spec)
    b = video._with_end_card(dict(s["base"]), None)
    old, fps = b.get("keep"), float(b["fps"])
    new = footage.cut(old, src_dur, [(f, t)], fps)
    lys, subs, cam = video._recut(s["story"]["layers"], s["opts"]["subtitles"] or [], s["opts"].get("camera"), old, new, [])
    b["keep"], b["duration"] = new, footage.length(new, src_dur)
    b = video._with_end_card(b, spec["base"].get("end_card"))
    s["base"], s["story"]["layers"] = b, lys
    s["story"]["subtitles"], s["opts"]["subtitles"], s["opts"]["camera"] = subs, subs or None, cam
    return s


def _plan(spec: dict, cycles=None) -> dict:
    return pc.plan(spec, cycles, engine="test-engine")


def _cyc(spec: dict, intro: int = 15, period: int = 1) -> dict:
    return {p["key"]: {"intro": intro, "period": period} for p in _plan(spec)["persistent"]}


def _tiles(p: dict) -> None:
    segs = p["segments"]
    assert [s["k"] for s in segs] == list(range(len(segs)))
    assert sum(s["frames"] for s in segs) == round(p["total"] * p["fps"]) == p["frames"]
    for a, b in zip(segs, segs[1:]):
        assert a["f0"] + a["frames"] == b["f0"]                                    # nincs hézag, nincs átfedés
        assert abs(a["out_start"] + a["dur"] - b["out_start"]) < 1e-5
    assert segs[0]["f0"] == 0 and all(s["frames"] > 0 for s in segs)
    assert all(s["dur"] <= pc.SEG_S + pc.MIN_S + 1e-6 for s in segs)
    for s in segs:
        assert s["f0"] == pc.frame(s["out_start"], p["fps"])
        assert len(s["hash"]) == 64


# ---------- a rács és a kockák ----------

@pytest.mark.parametrize("fps", [30.0, 29.97, 25.0, 60.0])
def test_the_grid_tiles_the_output_frame_exactly(fps):
    for spec in (_spec(fps=fps), _spec(fps=fps, card=CARD), _cut(_spec(fps=fps, card=CARD), 300.47, 320.21),
                 _cut(_cut(_spec(fps=fps), 100.1, 101.9), 400.0, 433.33)):
        _tiles(_plan(spec))


def test_the_parts_fall_on_the_frames_of_pr42():
    """PR #42: az i. rész a round(O_i·fps) … round(O_{i+1}·fps) − 1 kockákon — a rész első szakasza ott indul."""
    spec = _cut(_cut(_spec(), 100.1, 101.9), 300.47, 320.21)
    keep = spec["base"]["keep"]
    tl = footage.Timeline(keep, {None: Path("x")}, {None: {}}, {None: lambda t: ""}, (960, 540), FPS)
    p = _plan(spec)
    offs = footage._offsets(keep)
    for i, (o, x) in enumerate(offs):
        segs = [s for s in p["segments"] if s["part"] == i]
        assert segs[0]["f0"] == tl.frame(o)
        nxt = tl.frame(o + x["b"] - x["a"])
        assert segs[-1]["f0"] + segs[-1]["frames"] == nxt
        assert segs[0]["src_a"] == pytest.approx(x["a"]) and segs[-1]["src_b"] == pytest.approx(x["b"])


def test_the_grid_lives_on_the_source_time_and_short_rests_merge():
    p = _plan(_cut(_spec(), 300.5, 320.3))
    srcs = [(s["src_a"], s["src_b"]) for s in p["segments"] if not s["hold"]]
    assert (298.0, 300.0) in srcs and (300.0, 300.5) in srcs                      # 0,5 s nem „rövidebb”: marad
    assert (320.3, 322.0) in srcs and (322.0, 324.0) in srcs                      # a rács a FORRÁS 2 s-os pontjain
    p2 = _plan(_cut(_spec(), 300.2, 321.8))
    srcs2 = [(s["src_a"], s["src_b"]) for s in p2["segments"] if not s["hold"]]
    assert (298.0, 300.2) in srcs2 and (321.8, 324.0) in srcs2                     # a 0,2 s-os maradék / fej beolvad


# ---------- vágás és eltolás ----------

def test_a_grid_aligned_cut_at_5_min_reuses_everything_else():
    v1 = _spec(camera=CAM, card=CARD)
    cyc = _cyc(v1)                                                               # álló vízjel (kör = 1 kocka)
    v2 = _cut(v1, 300.0, 320.0)
    p1, p2 = _plan(v1, cyc), _plan(v2, cyc)
    d = pc.diff(p1, p2)
    assert d["dirty"] == [] and d["reused"] == d["total"] == len(p2["segments"])


def test_a_cut_at_5_min_dirties_only_the_segments_touching_the_cut_point():
    v1 = _spec(camera=CAM, card=CARD)
    cyc = _cyc(v1)
    v2 = _cut(v1, 300.5, 320.3)
    p1, p2 = _plan(v1, cyc), _plan(v2, cyc)
    d = pc.diff(p1, p2)
    touched = [s["k"] for s in p2["segments"] if s["part"] is not None and
               ((s["part"] == 0 and s["src_b"] == pytest.approx(300.5)) or (s["part"] == 1 and s["src_a"] == pytest.approx(320.3)))]
    assert d["dirty"] == touched and len(touched) == 2
    # a vágás után minden ugyanaz a forrás-szakasz ugyanazzal a hash-sel (a kimeneti idő 19,8 s-mal korábban van)
    key = lambda s, p: ("hold", round(s["out_start"] - p["plain"], 3)) if s["hold"] else (s["src_a"], s["src_b"])   # noqa: E731
    old = {key(s, p1): s["hash"] for s in p1["segments"]}
    for s in p2["segments"]:
        if s["k"] in touched:
            continue
        assert old[key(s, p2)] == s["hash"], s


def test_moving_everything_by_a_cut_keeps_the_later_segments_with_captions_layers_and_camera():
    """Egy kockára vágott, 4 tizedes hosszú vágás (19,9667 s): a rétegek / feliratok ideje 3 tizedesre kerekítve tolódik —
    a kockaszámos ujjlenyomat ettől nem változik (a másodperces helyi idő minden későbbi feliratos szakaszt érvénytelenítene)."""
    v1 = _spec(camera=CAM)
    cyc = _cyc(v1)
    v2 = _cut(v1, 300.0, 319.9667)
    assert v2["base"]["keep"][1][0] == pytest.approx(319.9667)
    p1, p2 = _plan(v1, cyc), _plan(v2, cyc)
    d = pc.diff(p1, p2)
    after = [s for s in p2["segments"] if s["part"] == 1]
    dirty_after = [s for s in after if s["k"] in d["dirty"]]
    assert len(dirty_after) <= 3, [(s["src_a"], s["src_b"]) for s in dirty_after]  # a vágás melletti fej (+ ha egy kocka átesik)
    assert all(s["src_a"] < 330 for s in dirty_after)
    l4 = next(ly for ly in v2["story"]["layers"] if ly["id"] == "L4")
    assert l4["at"] == pytest.approx(380.033, abs=1e-3)                           # (a réteg valóban odébb került)


def test_a_cut_dirties_the_rest_when_the_watermark_cycle_phase_moves():
    v1 = _spec()
    key = _plan(v1)["persistent"][0]["key"]
    pulse = {key: {"intro": 15, "period": 90}}                                    # 3 s-os pulzálás
    p1 = _plan(v1, pulse)
    same = pc.diff(p1, _plan(_cut(v1, 300.0, 330.0), pulse))                     # 900 kocka = 10 kör: ugyanaz a fázis
    assert same["dirty"] == []
    p2 = _plan(_cut(v1, 300.0, 310.0), pulse)                                     # 300 kocka: a fázis 30-cal odébb
    moved = pc.diff(p1, p2)
    wm = [s["k"] for s in p2["segments"] if s["part"] == 1 and s["src_b"] <= 593.5 - 10]   # a vízjel alatti szakaszok a vágás után
    assert set(wm) <= set(moved["dirty"]) and len(wm) >= 135
    assert all(p2["segments"][k]["part"] == 1 for k in moved["dirty"])           # előtte semmi
    p3 = _plan(_cut(v1, 300.0, 330.0))
    unknown = pc.diff(_plan(v1), p3)                                              # nincs kör: a helyi idő → a vágás után minden
    wm3 = [s["k"] for s in p3["segments"] if s["part"] == 1 and s["src_b"] <= 593.5 - 30]
    assert set(wm3) <= set(unknown["dirty"]) and len(wm3) >= 115
    assert all(p3["segments"][k]["part"] == 1 for k in unknown["dirty"])         # előtte a vízjel ugyanott tart


def test_the_watermark_phase_is_in_the_fingerprint():
    spec = _spec(subs=[], layers=[_layers()[0]])
    key = _plan(spec)["persistent"][0]["key"]
    cyc = {key: {"intro": 15, "period": 90}}
    c = pc.plan_ctx(spec, cyc, engine="test-engine")
    segs = pc._grid(c)
    la = c["layers"][0]["ps"] + 15
    for s in segs[5:40]:
        f = pc.fingerprint(spec, s, c)
        wm = f["layers"][0]
        assert wm["key"] == key and wm["phase"] == (s["f0"] - la) % 90 and "desc" not in wm
    head = pc.fingerprint(spec, segs[1], c)["layers"][0]                         # a bevezetésbe ér: a réteg eleje helyben
    assert head["fa"] == c["layers"][0]["ps"] - segs[1]["f0"]
    tail = [s for s in segs if s["f0"] + s["frames"] > c["layers"][0]["lb"]][0]  # a kivezetés: a vége helyben
    assert "fb" in pc.fingerprint(spec, tail, c)["layers"][0]
    # a kör kulcsa a réteg tartalmáé, nem az idejéé: odébb téve ugyanaz
    moved = _spec(subs=[], layers=[{**_layers()[0], "at": 13.5}])
    assert _plan(moved)["persistent"][0]["key"] == key


# ---------- réteg, akcentus, stílus, korrekció ----------

def test_a_layer_edit_dirties_at_most_its_length_over_seg_plus_two():
    v1 = _spec()
    cyc = _cyc(v1)
    p1 = _plan(v1, cyc)
    bound = math.ceil(10.0 / pc.SEG_S) + 2
    v2 = copy.deepcopy(v1)
    next(ly for ly in v2["story"]["layers"] if ly["id"] == "L4")["chart"]["title"] = "Új cím"
    d = pc.diff(p1, _plan(v2, cyc))
    assert 0 < len(d["dirty"]) <= bound
    assert all(398 <= p1["segments"][k]["out_start"] <= 410 for k in d["dirty"])
    v3 = copy.deepcopy(v1)                                                       # a stat 10 s-mal később: a régi + az új helye
    next(ly for ly in v3["story"]["layers"] if ly["id"] == "L5")["at"] = 510.0
    d3 = pc.diff(p1, _plan(v3, cyc))
    assert 0 < len(d3["dirty"]) <= 2 * bound
    assert all(498 <= p1["segments"][k]["out_start"] <= 520 for k in d3["dirty"])


def test_an_auto_placed_layer_follows_the_layers_it_dodges():
    lys = _layers() + [{"id": "L6", "type": "source", "at": 402.0, "dur": 20.0, "text": "Forrás: teszt"}]
    v1 = _spec(layers=lys)
    cyc = _cyc(v1)
    p1 = _plan(v1, cyc)
    v2 = copy.deepcopy(v1)
    next(ly for ly in v2["story"]["layers"] if ly["id"] == "L4")["position"] = "left"   # a chart máshová: a forrás is mozdulhat
    d = pc.diff(p1, _plan(v2, cyc))
    assert any(p1["segments"][k]["out_start"] >= 412 for k in d["dirty"])       # a forrás-réteg a chart után is


@pytest.mark.parametrize("change", ["accent", "caption_style", "adjust"])
def test_accent_caption_style_and_colour_correction_dirty_everything(change):
    v1 = _spec(card=CARD)
    cyc = _cyc(v1)
    v2 = {"accent": lambda: _spec(card=CARD, accent="#2a9df4"), "caption_style": lambda: _spec(card=CARD, style="box"),
          "adjust": lambda: _spec(card=CARD, adjust={"auto": True})}[change]()
    d = pc.diff(_plan(v1, cyc), _plan(v2, _cyc(v2)))
    assert d["reused"] == 0 and len(d["dirty"]) == d["total"]


# ---------- beszúrt klip, áttűnés ----------

def test_an_inserted_clip_with_a_dissolve():
    v1 = _spec(subs=[], layers=[_layers()[0]])
    cyc = _cyc(v1)
    keep = footage.insert(None, 600.0, 100.0, {"a": 5.0, "b": 15.0, "src": "clip.mp4", "join": "dissolve"}, FPS)
    v2 = _spec(subs=[], layers=[{**_layers()[0], "dur": 600.0}], keep=keep)      # (a vízjel a klippel együtt 10 s-mal tovább)
    p1, p2 = _plan(v1, cyc), _plan(v2, cyc)
    _tiles(p2)
    clip = [s for s in p2["segments"] if s["src"] == "clip.mp4"]
    assert [(s["src_a"], s["src_b"]) for s in clip] == [(5.0, 6.0), (6.0, 8.0), (8.0, 10.0), (10.0, 12.0), (12.0, 14.0), (14.0, 15.0)]
    c = pc.plan_ctx(v2, cyc, engine="test-engine")
    f_clip = pc.fingerprint(v2, clip[0], c)
    assert f_clip["part"]["src"] == "clip.mp4" and f_clip["dissolve"][0]["prev"]["src"] is None
    assert f_clip["dissolve"][0]["next"]["src"] == "clip.mp4" and f_clip["dissolve"][0]["j0"] == 0
    back = next(s for s in p2["segments"] if s["part"] == 2)                    # a fő felvétel folytatása: áttűnés a klipből
    f_back = pc.fingerprint(v2, back, c)
    assert f_back["dissolve"][0]["prev"]["src"] == "clip.mp4" and f_back["dissolve"][0]["next"]["src"] is None
    d = pc.diff(p1, p2)
    assert sorted(d["dirty"]) == sorted([s["k"] for s in clip] + [back["k"]])    # a klip + a visszatérés első szakasza
    before = next(s for s in p2["segments"] if s["part"] == 0 and s["src_b"] == 100.0)
    assert before["k"] not in d["dirty"]                                          # az áttűnés kockái a következő részéi
    # a klip színillesztése: a klip szakaszai + ahol a klip áttűnik a fő felvételbe; a hangereje (gain) is a klipé
    keep2 = copy.deepcopy(keep)
    keep2[1]["match"] = {"gamma": 1.1}
    d2 = pc.diff(p2, _plan(_spec(subs=[], layers=v2["story"]["layers"], keep=keep2), cyc))
    assert sorted(d2["dirty"]) == sorted([s["k"] for s in clip] + [back["k"]])
    keep3 = copy.deepcopy(keep)
    keep3[1]["gain"] = -3.0
    d3 = pc.diff(p2, _plan(_spec(subs=[], layers=v2["story"]["layers"], keep=keep3), cyc))
    assert set(d3["dirty"]) >= {s["k"] for s in clip}


# ---------- zárókártya ----------

def test_the_end_card_has_its_own_hold_segments():
    v1 = _spec(card=CARD)
    cyc = _cyc(v1)
    p1 = _plan(v1, cyc)
    hold = [s for s in p1["segments"] if s["hold"]]
    assert [(s["out_start"], s["dur"], s["frames"]) for s in hold] == [(600.0, 2.0, 60), (602.0, 2.0, 60), (604.0, 1.0, 30)]
    assert all(s["src_a"] is None and s["src"] is None and s["part"] is None for s in hold)
    c = pc.plan_ctx(v1, cyc, engine="test-engine")
    f = pc.fingerprint(v1, hold[0], c)
    assert f["src"]["hold_of"] == [None, 600 * 30 - 1]                           # a felvétel utolsó kockája áll
    assert any(ly["id"] == "end_card" for ly in f["layers"])
    v2 = _spec(card={**CARD, "title": "Köszönöm"})
    d = pc.diff(p1, _plan(v2, cyc))
    assert sorted(d["dirty"]) == [s["k"] for s in hold]
    v3 = _cut(v1, 100.0, 110.0)                                                  # korábbi vágás: a kártya ugyanaz
    p3 = _plan(v3, cyc)
    assert [s["hash"] for s in p3["segments"] if s["hold"]] == [s["hash"] for s in hold]


# ---------- futamok, diff ----------

def test_runs_are_contiguous_inside_one_part_and_capped(monkeypatch):
    spec = _cut(_spec(card=CARD), 300.5, 320.3)
    p = _plan(spec)
    segs = p["segments"]
    cut = next(s["k"] for s in segs if s["part"] == 1)
    hold = next(s["k"] for s in segs if s["hold"])
    rs = pc.runs(p, [cut - 2, cut - 1, cut, cut + 1, 0, 1, 2, hold - 1, hold, hold + 1, 9999])
    assert rs == [[0, 1, 2], [cut - 2, cut - 1], [cut, cut + 1], [hold - 1], [hold, hold + 1]]
    monkeypatch.setattr(pc, "RUN_MAX", 4)
    assert [len(r) for r in pc.runs(p, list(range(10)))] == [4, 4, 2]
    assert pc.diff(None, p) == {"dirty": list(range(len(segs))), "reused": 0, "total": len(segs)}


def test_the_plan_shape_and_the_audio_hash():
    spec = _spec(card=CARD, camera=CAM)
    p = _plan(spec)
    assert {"version", "source_id", "recipe", "fps", "W", "H", "pw", "ph", "total", "audio_hash", "segments"} <= set(p)
    assert (p["W"], p["H"], p["pw"], p["ph"], p["source_id"]) == (1920, 1080, 960, 540, "src.mov")
    s = p["segments"][0]
    assert {"k", "out_start", "dur", "frames", "src", "src_a", "part", "hold", "hash"} <= set(s)
    json.dumps(p)                                                                 # JSON-ba írható (a meta / a kliens)
    # a hang: a képi változás nem érinti, a vágás és a zene igen
    assert _plan(_spec(card=CARD, camera=CAM, accent="#000000"))["audio_hash"] == p["audio_hash"]
    assert _plan(_cut(spec, 100.0, 110.0))["audio_hash"] != p["audio_hash"]
    m = copy.deepcopy(spec)
    m["opts"]["music"] = "calm"
    assert _plan(m)["audio_hash"] != p["audio_hash"]
    pip = copy.deepcopy(spec)
    pip["story"]["layers"].append({"id": "L9", "type": "fullscreen", "at": 50, "dur": 5, "chart": {"type": "graphic", "elements": [
        {"shape": "image", "image": "clip.mp4", "sound": "mix", "clip": [1, 6]}]}})
    assert _plan(pip)["audio_hash"] != p["audio_hash"]
    with pytest.raises(ValueError):
        pc.plan({"kind": "make", "story": {}})


def test_the_fingerprint_has_no_output_time():
    """A kimeneti idő (out_start) nem kerül az ujjlenyomatba: ugyanaz a tartalom máshol ugyanaz a hash."""
    spec = _spec(subs=[], layers=[])
    c = pc.plan_ctx(spec, engine="test-engine")
    segs = pc._grid(c)
    f = pc.fingerprint(spec, segs[10], c)
    assert "out_start" not in f and "f0" not in f and f["src"] == [20.0, 22.0] and f["sf"] == 600


# ---------- tárolás ----------

def _fake(source_id: str, h: str, nbytes: int = 1000, age: float = 0.0) -> Path:
    p = pc.seg_path(source_id, h)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * nbytes)
    if age:
        t = time.time() - age
        os.utime(p, (t, t))
    return p


def test_paths_live_under_data_dir(data_dir):
    assert pc.seg_path("src.mov", "ab" * 32) == data_dir / "preview-cache" / "src.mov" / f"{'ab' * 32}.ts"
    assert pc.audio_dir("src.mov", "cd" * 32) == data_dir / "preview-cache" / "src.mov" / f"a-{'cd' * 32}"
    for bad in ("../x", "a/b", "", ".hidden"):
        with pytest.raises(ValueError):
            pc.seg_path(bad, "ab")


def test_storage_without_postgres_uses_the_files(monkeypatch):
    from app import db
    assert not db.enabled()
    shutil.rmtree(pc.cache_root(), ignore_errors=True)
    _fake("s1.mov", "a" * 64, age=300)
    _fake("s1.mov", "b" * 64, age=200)
    _fake("s2.mov", "c" * 64, age=100)

    async def main():
        assert await pc.ready(["a" * 64, "b" * 64, "d" * 64], "s1.mov") == {"a" * 64, "b" * 64}
        assert await pc.ready(["a" * 64, "c" * 64, "d" * 64]) == {"a" * 64, "c" * 64}         # forrás nélkül: a kötet
        await pc.touch(["a" * 64], "s1.mov")                                                  # a legrégebbi most használt
        monkeypatch.setenv("VIDEO_PREVIEW_CACHE_GB", str(2500 / 1024 ** 3))
        out = await pc.sweep()
        assert out["removed"] == 1 and out["bytes"] <= 2500
        assert await pc.ready(["a" * 64, "b" * 64, "c" * 64]) == {"a" * 64, "c" * 64}         # a b volt a legrégebben használt
    asyncio.run(main())


def test_storage_with_postgres_lru(pg_url, monkeypatch):
    from app import db
    from app.config import settings
    shutil.rmtree(pc.cache_root(), ignore_errors=True)
    settings.DATABASE_URL = pg_url

    async def main():
        await db.start()
        try:
            assert db.enabled()
            for i, h in enumerate(("e" * 64, "f" * 64, "0" * 64)):
                _fake("s3.mov", h, 1000)
                await pc.register(h, "s3.mov", 1000)
                await db._rows("UPDATE preview_segments SET last_used_at = now() - %s * interval '1 minute' WHERE hash = %s",
                               (10 - i, h))
            assert await pc.ready(["e" * 64, "f" * 64, "1" * 64]) == {"e" * 64, "f" * 64}
            await pc.touch(["e" * 64])                                            # az e most használt: a f a legrégebbi
            pc.seg_path("s3.mov", "0" * 64).unlink()                              # a fájl eltűnt: a sor is menjen
            assert await pc.ready(["0" * 64]) == set()
            assert not await db.preview_seg_get(["0" * 64])
            _fake("s3.mov", "1" * 64, 1000)                                       # a köteten van, nincs nyilvántartva
            assert await pc.ready(["1" * 64], "s3.mov") == {"1" * 64}
            assert await db.preview_seg_get(["1" * 64])
            monkeypatch.setenv("VIDEO_PREVIEW_CACHE_GB", str(2500 / 1024 ** 3))
            out = await pc.sweep()
            assert out["removed"] == 1 and out["bytes"] == 2000
            assert await pc.ready(["e" * 64, "f" * 64, "1" * 64]) == {"e" * 64, "1" * 64}
            assert not pc.seg_path("s3.mov", "f" * 64).exists()
            await db.preview_cycle_put("k" * 64, 15, 90)
            assert await db.preview_cycle_get("k" * 64) == {"intro": 15, "period": 90}
        finally:
            await db.stop()
            settings.DATABASE_URL = ""
    asyncio.run(main())


def test_no_sqlite_in_the_cache():
    src = Path(pc.__file__).read_text("utf-8")
    assert "sqlite" not in src.lower()


# ---------- a futam-render a motorral (kockapontosság, a szakaszfájlok, a hang) ----------

def _engine_ok() -> bool:
    return video.available() and bool(shutil.which("ffmpeg"))


def _reset_gates() -> None:
    from app.tools import video_preview
    video_preview._gate = None
    video._gate = None
    video._bgate = None
    pc._CTX.clear()


def _probe_ts(p: Path) -> tuple[int, int, str]:
    out = json.loads(subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v", "-show_entries",
                                     "stream=nb_read_frames,width:frame=key_frame", "-read_intervals", "%+#1", "-of", "json", str(p)],
                                    capture_output=True, text=True, check=True).stdout)
    n = json.loads(subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v", "-show_entries",
                                   "stream=nb_read_frames", "-of", "json", str(p)], capture_output=True, text=True, check=True).stdout)
    return int(n["streams"][0]["nb_read_frames"]), int(out["streams"][0]["width"]), str(out["frames"][0]["key_frame"])


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_render_runs_are_frame_exact_on_an_overlay(tmp_path, put_file, monkeypatch):
    from app.tools import fast_render
    _reset_gates()
    src = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=48000", "-t", "32", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(src)], check=True)
    fid = put_file(src)
    layers = [{"at": 1.0, "until": 31.5, "type": "graphic", "anim": "fade", "backdrop": 0, "elements": [
                  {"shape": "text", "text": "TV", "x": 0.095, "y": 0.06, "size": 34, "weight": "bold", "color": "white"}]},
              {"at": 4, "dur": 3, "type": "lower_third", "name": "Anna Kovács", "role": "Mayor", "side": "left"},
              {"at": 20, "dur": 3, "type": "source", "text": "Forrás: teszt", "position": "bottom"}]
    subs = [{"start": 0.5, "end": 3.5, "text": "Ez az első mondat"}, {"start": 9.0, "end": 12.0, "text": "és ez a második mondat"}]

    async def main():
        res = await video.overlay({"file_id": fid, "brand": "none", "layers": layers, "subtitles": subs, "caption_style": "highlight",
                                   "camera": [{"at": 14, "zoom": 1.2, "dur": 0.6}, {"at": 16, "zoom": 1, "dur": 0.6}],
                                   "end_card": {"title": "Vége", "dur": 2}, "render": False})
        ctx = await pc.context(res["file_id"])
        p = ctx["plan"]
        assert ctx["path"] == "fast" and (p["pw"], p["ph"]) == (960, 540) and p["total"] == 34.0
        assert ctx["persist"] and ctx["persist"][0]["P"] >= 1 and ctx["opaque"]       # a vízjel körből; a ráközelítés a motoré
        assert await pc.context(res["file_id"]) is ctx                                 # verziónként egyszer
        segs = p["segments"]
        ks = sorted({0, 1, 2, 3, 6, 7, 9, 10, 15, 16, len(segs) - 2, len(segs) - 1})
        for run in pc.runs(p, ks):
            hs = await pc.render_run(ctx, run)
            assert hs == [segs[k]["hash"] for k in run]
            for k, h in zip(run, hs):
                n, w, key = _probe_ts(pc.seg_path(p["source_id"], h))
                assert (n, w, key) == (segs[k]["frames"], 960, "1"), (k, n, w, key)
        assert await pc.ready([segs[k]["hash"] for k in ks], p["source_id"]) == {segs[k]["hash"] for k in ks}
        chk = await pc.frame_check(ctx, ks=[1, 2, 3, 7, 10, 16, len(segs) - 1])  # vízjel, alsó harmad, felirat, kamera, kártya
        assert chk["ok"], chk
        d = await pc.render_audio(ctx)
        m3u = (d / "index.m3u8").read_text()
        got = sum(float(x.split(":")[1].rstrip(",")) for x in m3u.splitlines() if x.startswith("#EXTINF"))
        assert got == pytest.approx(34.0, abs=0.1) and "#EXT-X-PLAYLIST-TYPE:VOD" in m3u
        assert await pc.render_audio(ctx) == d                                         # másodszor kész
        # ha a projekt nem a gyors útra való: a futam a motorból, más hash-sel (a projekt is az ujjlenyomatban)
        _reset_gates()
        monkeypatch.setattr(fast_render, "classify", lambda info, spec: {"ok": False, "why": "test"})
        ctx2 = await pc.context(res["file_id"])
        assert ctx2["path"] == "engine" and ctx2["plan"]["segments"][3]["hash"] != segs[3]["hash"]
        hs2 = await pc.render_run(ctx2, [3, 4])
        assert [_probe_ts(pc.seg_path(p["source_id"], h))[0] for h in hs2] == [segs[3]["frames"], segs[4]["frames"]]
    asyncio.run(main())
