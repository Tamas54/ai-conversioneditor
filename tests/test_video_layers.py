"""Időkódos rétegek (layers) a make- és az overlay-videón, a felhasználó felvétele alapsávként, átlátszó (alpha) kimenet,
őszinte időtartam (duration_mode). A logika motor nélkül; a végponttól végpontig tesztek EffectCraft-tal (engines)."""
from __future__ import annotations

import asyncio
import inspect
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = ("The city of Szeged plans a budget of 47.5 bn Ft for 2025, up from 44.8 bn Ft in 2024 and 41.2 bn Ft in 2023. "
       "Education gets 38% of it. “We will spend every forint where people can see it,” said Mayor Anna Kovács.")
COLUMN = {"type": "column", "title": "Budget", "unit": "bn Ft",
          "items": [{"label": "2023", "value": 41.2}, {"label": "2024", "value": 44.8}, {"label": "2025", "value": 47.5}]}


def _probe(p: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


# ---------- időkód, rétegek ----------

def test_timecodes():
    from app.tools.video import timecode
    assert timecode("01:24") == 84 and timecode("1:02:03.5") == 3723.5 and timecode("00:01:24,5") == 84.5
    assert timecode(84) == 84 and timecode("84.5s") == 84.5 and timecode("7") == 7
    for bad in ("1:75", "abc", -1, "", None, True, "1:2:3:4"):
        with pytest.raises(ValueError, match="timecode"):
            timecode(bad)


def test_layers_get_stable_ids_and_are_cleaned():
    from app.tools import video
    notes: list[str] = []
    ls = video.layers_from([{"at": "00:02", "type": "lower_third", "name": "Anna Kovács", "role": "Mayor"},
                            {"id": "intro", "at": 0, "until": "00:03.5", "type": "Lower-Third", "name": "X"},
                            {"at": 9, "type": "column", "title": "Budget", "unit": "bn Ft", "items": COLUMN["items"]}], SRC, notes)
    assert [x["id"] for x in ls] == ["L1", "intro", "L2"]
    assert ls[0]["dur"] == 5.0 and ls[1]["type"] == "lower_third" and ls[1]["dur"] == 3.5
    assert ls[2]["type"] == "chart" and ls[2]["chart"]["type"] == "column"            # rövidítés: a típus maga az ábra
    assert any("Layer L2" in n and "in the source" in n for n in notes)
    with pytest.raises(ValueError, match="lower_third .* split"):
        video.layers_from([{"at": 1, "type": "banner"}], SRC, [])
    with pytest.raises(ValueError, match="give at"):
        video.layers_from([{"type": "title", "text": "x"}], SRC, [])
    with pytest.raises(ValueError, match="used twice"):
        video.layers_from([{"id": "A", "at": 1, "type": "source", "text": "s"}, {"id": "A", "at": 2, "type": "source", "text": "t"}], "", [])
    with pytest.raises(ValueError, match="position"):
        video.layers_from([{"at": 1, "type": "source", "text": "s", "position": [0.5, 0.5]}], "", [])


def test_numbers_and_quotes_are_checked_against_the_source_or_said_unchecked():
    from app.tools import video
    notes: list[str] = []
    ls = video.layers_from([{"at": 1, "type": "chart", "chart": {**COLUMN, "items": COLUMN["items"] + [{"label": "2026", "value": 51}]}},
                            {"at": 2, "type": "stat", "value": "99%", "label": "made up"},
                            {"at": 3, "type": "quote", "text": "We will spend it all", "author": "Anna Kovács"},
                            {"at": 4, "type": "quote", "text": "We will spend every forint where people can see it", "author": "Anna Kovács"},
                            {"at": 5, "type": "stat", "value": "38%", "label": "for schools"}], SRC, notes)
    assert [x["id"] for x in ls] == ["L4", "L5"]                                   # a kitalált számú / nem szó szerinti kimaradt
    bad = [n for n in notes if n.startswith("⚠ ")]
    assert any("L1" in n for n in bad) and any("L2" in n and "99%" in n for n in bad) and any("L3" in n for n in bad)
    # forrás nélkül: marad, és kimondva NEM ellenőrzött
    notes = []
    ls = video.layers_from([{"at": 1, "type": "chart", "chart": COLUMN}, {"at": 2, "type": "stat", "value": "99%", "label": "x"}], "", notes)
    assert len(ls) == 2 and sum("not checked against a source" in n for n in notes) == 2
    assert not any("in the source text" in n for n in notes)


def test_targeted_layer_edit_by_id():
    from app.tools import video
    ls = video.layers_from([{"at": 1, "type": "lower_third", "name": "A"}, {"at": 4, "type": "chapter", "text": "One"},
                            {"at": 7, "type": "chart", "chart": COLUMN}, {"at": 9, "type": "source", "text": "S"}], SRC, [])
    notes: list[str] = []
    out = video.apply_layer_changes(ls, {"layers": [{"id": "L2", "text": "Two", "until": "00:06"}, {"id": "L4", "remove": True},
                                                    {"id": "L3", "chart": {"title": "City budget"}}],
                                         "add_layers": [{"at": 12, "type": "title", "text": "End"}]}, SRC, notes)
    by = {x["id"]: x for x in out}
    assert list(by) == ["L1", "L2", "L3", "L5"]
    assert by["L1"] == ls[0]                                                       # érintetlen
    assert by["L2"]["text"] == "Two" and by["L2"]["at"] == 4 and by["L2"]["dur"] == 2
    assert by["L3"]["chart"]["title"] == "City budget" and by["L3"]["chart"]["items"] == ls[2]["chart"]["items"]
    with pytest.raises(ValueError, match="no layer L9 .*L1, L2"):
        video.apply_layer_changes(ls, {"layers": [{"id": "L9", "text": "x"}]}, SRC, [])
    rows = video.layer_listing(out)
    assert rows[0]["timecode"] == "00:01–00:06" and rows[1]["text"] == "Two"
    l3 = next(x for x in video.apply_layer_changes(out, {"layers": [{"id": "L3", "type": "bar"}]}, SRC, []) if x["id"] == "L3")
    assert l3["chart"]["type"] == "bar" and len(l3["chart"]["items"]) == 3          # csak a típus cserélődött


@pytest.mark.parametrize("W,H", [(1280, 720), (1080, 1920), (1080, 1080)])
def test_every_layer_type_fits_the_frame(W, H):
    from app.tools import video
    ls = video.layers_from([
        {"at": 0, "type": "lower_third", "name": "Anna Kovács", "role": "Mayor of Szeged, since 2019"},
        {"at": 0, "type": "title", "text": "Szeged in numbers", "subtitle": "The budget"},
        {"at": 0, "type": "chapter", "text": "Part two: where the money goes", "kicker": "Chapter 2"},
        {"at": 0, "type": "source", "text": "Source: City of Szeged"},
        {"at": 0, "type": "stat", "value": "47.5 bn", "label": "Ft for 2025"},
        {"at": 0, "type": "quote", "text": "We will spend every forint where people can see it", "author": "Anna Kovács"},
        {"at": 0, "type": "chart", "chart": COLUMN},
        {"at": 0, "type": "fullscreen", "text": "Thank you"},
        {"at": 0, "type": "split", "chart": COLUMN}], SRC, [])
    out, rows, _ = video.layer_comps(ls, W, H, 10.0, "en", [1, 0.3, 0.2], H - 60)
    assert len(out) == len(ls) == len(rows)
    for it in out:
        x0, y0 = it["pos"][0] - it["w"] / 2, it["pos"][1] - it["h"] / 2
        assert -1 <= x0 and -1 <= y0 and x0 + it["w"] <= W + 1 and y0 + it["h"] <= H + 1, it["id"]
        assert it["elements"] and it["w"] % 2 == 0 and it["h"] % 2 == 0
        for e in it["elements"]:
            if e["k"] == "text":
                assert e["size"] >= 10 and e["box"][0] <= it["w"] + 1, (it["id"], e["name"])
    sp = next(x for x in out if x.get("split"))
    assert 0.3 < sp["split"]["scale"] < 0.6


def test_layers_past_the_end_and_overlaps_are_findings():
    from app.tools import video
    ls = video.layers_from([{"at": 12, "type": "title", "text": "Late"}, {"at": 8, "dur": 5, "type": "source", "text": "S"},
                            {"at": 1, "type": "lower_third", "name": "A", "side": "left"},
                            {"at": 2, "type": "lower_third", "name": "B", "side": "left"}], "", [])
    out, rows, probs = video.layer_comps(ls, 1280, 720, 10.0, "en", [1, 0, 0], 670)
    assert {r["id"] for r in rows} == {"L2", "L3", "L4"}
    assert any("L1 starts at 00:12" in p and "left out" in p for p in probs)
    assert any("L2 was asked to last until 00:13" in p for p in probs)
    assert any("L3" in p and "L4" in p and "same place" in p for p in probs)


def test_auto_placement_avoids_other_layers_and_the_scene_text():
    from app.tools import video
    story = {"title": "Budget", "scenes": [{"id": "s1", "headline": "A bigger budget", "caption": "Szeged plans to spend more."}],
             "end": {"title": "", "subtitle": ""}}
    D = video.compose(story, fmt="landscape", total=12.0, images=[None, None], accent=None, credits="", voice=None,
                      voice_s=None, music=None)
    s1 = next(r for r in D["timeline"] if r["id"] == "s1")
    ls = video.layers_from([{"at": s1["start"] + 1, "dur": 3, "type": "lower_third", "name": "City of Szeged"}], "", [])
    out, _, probs = video.layer_comps(ls, D["W"], D["H"], D["total"], "en", D["accent"], video._caption_top(D["W"], D["H"]),
                                      video._scene_zones(D))
    assert out[0]["pos"][0] > D["W"] / 2 and not probs                          # a bal oldali jelenetcím elől jobbra
    ls = video.layers_from([{"at": s1["start"] + 1, "dur": 3, "type": "lower_third", "name": "City of Szeged", "side": "left",
                             "role": "The budget plan for the year 2025, as the city council approved it"}], "", [])
    _, _, probs = video.layer_comps(ls, D["W"], D["H"], D["total"], "en", D["accent"], video._caption_top(D["W"], D["H"]),
                                    video._scene_zones(D))
    assert any("covers scene s1's headline" in p for p in probs)                 # kért hely: marad, de a check szól


def test_number_language_is_not_decided_by_an_accent():
    from app.tools.video import _lang_of
    assert _lang_of("Budget by year Mayor of Szeged Anna Kovács") == "en"
    assert _lang_of("Az Integritás Hatóság 2022-ben jött létre, hogy őrködjön") == "hu"
    assert _lang_of("Integritás Hatóság") == "hu"


# ---------- őszinte időtartam ----------

def test_fixed_mode_fills_the_asked_length_and_reports_overruns():
    from app.tools import video
    story = {"title": "T", "scenes": [{"id": f"s{i}", "headline": "H", "caption": "C"} for i in range(1, 4)], "end": {"title": "", "subtitle": ""}}
    clips = [(None, 1.0)] + [(Path(f"/x{i}.wav"), 2.5) for i in range(3)]
    fitted, over = video.fit_to_length(story, clips, 24.0)
    D = video.compose(fitted, fmt="landscape", total=24.0, images=[None] * 4, accent=None, credits="", voice=None, voice_s=None, music=None)
    assert over == 0 and abs(D["total"] - 24.0) < 0.05
    assert all(sc["duration"] >= 2.5 + 1.4 for sc in fitted["scenes"])
    old = video.compose(video.fit_to_narration(story, clips), fmt="landscape", total=24.0, images=[None] * 4, accent=None,
                        credits="", voice=None, voice_s=None, music=None)
    assert old["total"] < 20                                     # a régi út: a narráció felülírta a kért 24 s-ot
    _, over = video.fit_to_length(story, [(None, 1.0)] + [(Path("/x.wav"), 12.0)] * 3, 24.0)
    assert over > 10
    assert video.duration_mode(None, 24) == "fixed" and video.duration_mode(None, None) == "narration"
    assert video.duration_mode("narration", 24) == "narration"
    with pytest.raises(ValueError, match="fixed .* narration"):
        video.duration_mode("exact", 24)
    assert video.alpha_kinds(True) == ["mov"] and video.alpha_kinds("both") == ["mov", "webm"] and video.alpha_kinds(None) == []
    with pytest.raises(ValueError, match="alpha"):
        video.alpha_kinds("gif")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg needed")
def test_check_video_compares_with_the_asked_length(tmp_path):
    from app.tools.checks import check_video
    p = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=17",
                    "-pix_fmt", "yuv420p", str(p)], check=True)
    c = check_video(p, 17.0, None, False, 24.0, "narration")
    assert c["status"] == "needs_check"
    assert "Asked for 24 s, it is 17 s (narration mode: the voice sets the length)." in c["findings"]
    assert not any("as asked" in v for v in c["verified"])
    c = check_video(p, 17.0, None, False, 24.0, "fixed", "fixed mode: the narration needs about 7 s more")
    assert any(f.startswith("Asked for 24 s, it is 17 s (fixed mode: the narration") for f in c["findings"])
    c = check_video(p, 17.0, None, False, 17.0, "fixed")
    assert c["status"] == "ok" and any("Length as asked: 17 s" in v for v in c["verified"])
    c = check_video(p, 24.0, None, False, None, "footage")
    assert any("timeline is 24.0 s" in f for f in c["findings"])


# ---------- előnézet és klip rétegre ----------

def test_preview_and_clip_find_a_layer_by_id(tmp_path):
    from app.storage import new_file_id, output_path, write_meta
    from app.tools import video
    pid, vid = new_file_id("ecproj"), new_file_id("mp4")
    output_path(pid).write_text("{}", "utf-8")
    output_path(vid).write_bytes(b"x")
    rows = [{"id": "L1", "start": 2.0, "end": 6.0, "type": "lower_third", "layer": True},
            {"id": "L2", "start": 6.0, "end": 9.0, "type": "chapter", "layer": True}]
    write_meta(pid, extra={"render_file_id": vid, "spec": {"kind": "overlay", "story": {"layers": [{"id": "L1"}, {"id": "L2"}]},
                                                             "timeline": [], "layer_timeline": rows}})
    write_meta(vid, source_file_id=pid)
    assert video.frame_time(vid, "L2") == (vid, 8.4, "L2 (chapter, 6.0–9.0 s)")
    assert video.frame_time(pid, None)[1] == 5.4                              # alapból az első réteg
    k, ly, row = video._scene_index(pid, "l1")
    assert k == 0 and ly["id"] == "L1" and row["layer"]
    with pytest.raises(ValueError, match="layers: L1, L2"):
        video.frame_time(vid, "L7")


def test_video_tool_describes_overlay_layers_alpha_and_duration():
    from app import mcp_server
    fn = inspect.unwrap(mcp_server.video)
    params = inspect.signature(fn).parameters
    for p in ("layers", "alpha", "duration_mode"):
        assert p in params
    doc = fn.__doc__
    for s in ('action="overlay"', "lower_third", "split", "fullscreen", "duration_mode", "ProRes 4444", '"webm"',
              "AI NARRATION IS ON BY DEFAULT", 'narration="none"', 'scene="L2"', "add_layers", "not checked against a source"):
        assert s in doc, s
    assert "text-to-speech" not in doc.lower()


# ---------- végponttól végpontig, EffectCraft-tal ----------

def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI (EFFECTCRAFT_CLI) and ffmpeg needed")
def test_overlay_on_own_footage_alpha_edit_and_layer_clip(tmp_path, put_file):
    from app.storage import output_path, read_meta
    from app.tools import video
    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=48000", "-t", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(clip)], check=True)
    fid = put_file(clip)
    layers = [{"at": "00:00.5", "dur": 2.5, "type": "lower_third", "name": "Anna Kovács", "role": "Mayor"},
              {"at": 2, "dur": 3, "type": "chart", "chart": COLUMN},
              {"at": 0.2, "until": 5.8, "type": "source", "text": "Source: City of Szeged"},
              {"at": 3.2, "dur": 2.5, "type": "split", "side": "left", "text": "47.5 bn Ft"}]
    res = asyncio.run(video.overlay({"file_id": fid, "layers": layers, "text": SRC, "alpha": True}))
    assert res["check"]["status"] in ("ok", "needs_check"), res["check"]
    assert res["duration"]["mode"] == "footage" and abs(res["duration"]["actual"] - 6.0) < 0.15
    vs = [s for s in _probe(output_path(res["video"]["file_id"]))["streams"]]
    v = next(s for s in vs if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (640, 360) and v["r_frame_rate"] == "25/1"      # a felvétel mérete és fps-e marad
    assert any(s["codec_type"] == "audio" for s in vs)                                  # és a hangja
    mov = res["alpha"][0]
    assert mov["file_id"].endswith(".mov") and mov["alpha"] and mov["pix_fmt"].startswith("yuva")
    assert any(x.startswith("Transparent graphics: ProRes 4444") for x in res["check"]["verified"])
    assert [x["id"] for x in res["layers"]] == ["L3", "L1", "L2", "L4"]
    assert any("Layer L2" in x and "in the source text" in x for x in res["check"]["verified"])
    # célzott szerkesztés: L1 neve, L4 ki → új változat, a felvétel ugyanaz a fájl
    ed = asyncio.run(video.edit(res["video"]["file_id"], {"layers": [{"id": "L1", "name": "Kovács Anna"}, {"id": "L4", "remove": True}]}))
    assert read_meta(ed["file_id"])["source_file_id"] == res["file_id"]
    assert video.assets_of(ed["file_id"])["footage.mp4"] == video.assets_of(res["file_id"])["footage.mp4"]
    info = video.info(ed["file_id"])
    assert [(x["id"], x["text"]) for x in info["layers"]][:2] == [("L3", "Source: City of Szeged"), ("L1", "Kovács Anna — Mayor")]
    assert "L4" not in {x["id"] for x in info["layers"]} and ed.get("alpha")            # az alpha a változatban is megvan
    _, t, label = video.frame_time(ed["file_id"], "L1")
    assert label.startswith("L1 (lower_third") and 0.5 < t < 3.0
    c = asyncio.run(video.render_scene(ed["file_id"], "L2"))
    assert c["layer"] == "L2" and abs(float(_probe(output_path(c["file_id"]))["format"]["duration"]) - 3.0) < 0.2


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI (EFFECTCRAFT_CLI) and ffmpeg needed")
def test_make_video_takes_layers_and_hits_the_fixed_length(put_file):
    from app.tools import video
    scenes = [{"headline": "A bigger budget", "caption": "Szeged plans to spend 47.5 billion forints."},
              {"headline": "Growth", "caption": "The budget grew every year.", "chart": COLUMN}]
    res = asyncio.run(video.make({"mode": "article", "text": SRC, "scenes": scenes, "title": "Budget", "format": "square",
                                  "pictures": "none", "narration": "none", "duration": 14,
                                  "layers": [{"at": 4, "dur": 3, "type": "lower_third", "name": "City of Szeged"},
                                             {"at": 1, "until": 13, "type": "source", "text": "Source: city budget"}]}))
    assert res["duration"]["requested"] == 14 and res["duration"]["mode"] == "fixed" and abs(res["duration"]["actual"] - 14) <= 0.6
    assert any("Length as asked: 14 s" in v for v in res["check"]["verified"])
    assert {x["id"] for x in res["layers"]} == {"L1", "L2"}
    _, t, label = video.frame_time(res["file_id"], "L1")
    assert label.startswith("L1 (lower_third") and 4 < t < 7
    info = video.info(res["file_id"])
    assert info["layers"] and info["scenes"] and info["video"]["duration_mode"] == "fixed"
