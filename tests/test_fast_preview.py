"""Gyors előnézet + a végleges render a háttérben, becsült idővel (10-23, Kommandant: „az eredményt GYORSAN akarom
látni… a tökéletes eredmény sokáig tarthat, de ki kell írni, mennyi idő”).

- mi változott: rétegek ±2 s, vágáspontok ±3 s, új feliratok, zárókártya, minták a színkorrekcióhoz; összevonás, korlát;
- a becslés: a grafika ideje, a képpont-sebesség szerinti arány, a kalibráció kiugró-szűrője, a hátralévő idő;
- a háttérmunka alacsony prioritással fut (nice);
- végponttól végpontig: az overlay / edit előnézettel és háttérmunkával tér vissza, az előnézet nem vár a futó
  végleges renderre, a háttérmunka eredménye a végleges MP4.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.tools import render_eta, video_preview as vp


def test_pick_merges_snaps_and_caps():
    spans = [(10.0, 14.0, "L1 chart"), (14.5, 18.0, "L2 quote"), (40.0, 46.0, "cut"), (100.0, 160.0, "L3 graphic")]
    out, left = vp.pick(spans, 200.0, cap=30.0, fps=25)
    assert out == [(10.0, 18.0, "L1 chart, L2 quote"), (40.0, 46.0, "cut")] and left == 1   # a 60 s-os nem fér bele
    first, left = vp.pick([(100.0, 160.0, "L3")], 200.0, cap=30.0, fps=25)
    assert first == [(100.0, 130.0, "L3")] and left == 0                                      # az első levágva is benne


def test_changed_spans_of_a_new_video_and_an_edit():
    spec = {"story": {"layers": [{"id": "L1", "type": "chart", "at": 10, "dur": 4}, {"id": "L2", "type": "quote", "at": 50, "dur": 3}],
                      "subtitles": [{"start": 1, "end": 3, "text": "a"}]},
            "base": {"keep": [[0, 20], [30, 100]], "end_card": {"dur": 4}, "plain": {"duration": 90}}}
    new = vp.spans_new(spec, 94.0)
    assert (8.0, 16.0, "L1 chart") in new and (17.0, 23.0, "cut") in new and (89.0, 94.0, "end card") in new
    old = {"story": {"layers": [{"id": "L1", "type": "chart", "at": 10, "dur": 4}, {"id": "L2", "type": "quote", "at": 50, "dur": 3},
                                {"id": "L3", "type": "stat", "at": 70, "dur": 5}], "subtitles": [{"start": 1, "end": 3, "text": "a"}]},
           "base": {}}
    edited = {"story": {"layers": [{"id": "L1", "type": "chart", "at": 10, "dur": 4}, {"id": "L2", "type": "quote", "at": 52, "dur": 3},
                                   {"id": "L4", "type": "title", "at": 30, "dur": 2}],
                        "subtitles": [{"start": 1, "end": 3, "text": "a"}, {"start": 80, "end": 82, "text": "új sor"}]},
              "base": {}}
    changes = {"layers": [{"id": "L2", "at": 52}, {"id": "L3", "remove": True}], "add_layers": [{"at": 30, "type": "title"}],
               "subtitles": [...], "adjust": {"auto": True}}
    got = vp.spans_edit(old, edited, changes, 100.0)
    labels = {lab for _, _, lab in got}
    assert {"L2 quote", "L4 title", "L3 stat (removed)", "subtitles", "colour and light"} <= labels
    assert not any(lab.startswith("L1") for _, _, lab in got)                                 # ami nem változott, nincs benne
    assert (79.5, 82.5, "subtitles") in got and (68.0, 77.0, "L3 stat (removed)") in got


def test_eta_scales_with_pixels_and_counts_graphics():
    spec = {"base": {"width": 1920, "height": 1080, "fps": 30, "duration": 600, "hdr": "hlg", "proxy": {"w": 960, "h": 540},
                     "end_card": {"dur": 4}},
            "story": {"layers": [{"at": 0, "dur": 600}, {"at": 10, "dur": 5}, {"at": 13, "dur": 5}, {"at": 100, "dur": 10}]},
            "opts": {"camera": [{"at": 200, "zoom": 1.2, "dur": 0.6}]}}
    g = render_eta.graphics_seconds(spec, 600.0)
    assert g == pytest.approx(8 + 10 + 4 + 0.6 + render_eta.PROBE_S)      # 10–18 egybe, 100–110, a kártya, a kamera, a minta
    render_eta._CACHE.update(at=time.time(), rates=dict(render_eta.DEFAULTS))
    hd = asyncio.run(render_eta.final(spec))["seconds"]
    uhd = asyncio.run(render_eta.final({**spec, "base": {**spec["base"], "width": 3840, "height": 2160}}))["seconds"]
    assert 3.6 < (uhd - render_eta.OVERHEAD_S) / (hd - render_eta.OVERHEAD_S) < 4.4
    sdr = asyncio.run(render_eta.final({**spec, "base": {**spec["base"], "hdr": None}}))["seconds"]
    assert sdr < hd
    assert render_eta.text(40) == "about 40 s" and render_eta.text(35 * 60) == "about 35 min" and render_eta.text(3 * 3600) == "about 3.0 h"


def test_calibration_skips_outliers(monkeypatch):
    from app import db
    seen = []

    async def fake_observe(stage, key, rate, weight=0.3):
        seen.append((stage, key, round(rate, 3)))
    monkeypatch.setattr(db, "render_rate_observe", fake_observe)
    render_eta._CACHE.update(at=time.time(), rates=dict(render_eta.DEFAULTS))
    asyncio.run(render_eta.observe("final", "hdr", 900.0, 1000.0))             # 0,9: rendben
    asyncio.run(render_eta.observe("proxy", "hdr", 0.3, 1800.0))               # gyorsítótárból jött: kiugró, nem számít
    asyncio.run(render_eta.observe("graphics", "any", 5.0, 0.2))               # túl kevés mérnivaló
    assert seen == [("final", "hdr", 0.9)]


def test_job_remaining_time_and_background_priority():
    from app import jobs
    from app.convert import engines
    job = {"id": "x", "kind": "render", "owner": None, "status": "running", "eta_s": 600, "eta_from": time.time() - 100}
    pub = jobs._public(job)
    assert 495 <= pub["remaining_s"] <= 500 and pub["remaining"] == "about 8 min" and "T" in pub["ready_at"]
    late = jobs._public({**job, "eta_from": time.time() - 700})
    assert late["remaining_s"] is None and "longer than estimated" in late["remaining"]
    assert engines.niced(["ffmpeg", "-i", "x"])[0] == "ffmpeg"
    with engines.background():
        argv = engines.niced(["ffmpeg", "-i", "x"])
    assert argv[1:4] == ["-n", "10", "ffmpeg"] and engines.niced(["ffmpeg"]) == ["ffmpeg"]


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


def _clip(path: Path, secs: int = 8) -> Path:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=10", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=48000", "-t", str(secs), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def _probe(p: Path) -> dict:
    return json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(p)],
                                     capture_output=True, text=True, check=True).stdout)


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_long_video_gets_a_quick_preview_and_the_final_in_the_background(tmp_path, put_file):
    from app import jobs
    from app.storage import output_path
    from app.tools import video
    fid = put_file(_clip(tmp_path / "talk.mp4"))
    layers = [{"at": 1, "dur": 1.5, "type": "lower_third", "name": "Anna Kovács", "role": "Mayor"},
              {"at": 5, "dur": 1.5, "type": "source", "text": "Forrás: teszt"}]

    async def main():
        res = await video.overlay({"file_id": fid, "brand": "none", "layers": layers, "final": "background"})
        pv = res["preview"]
        assert [(p["from"], p["to"]) for p in pv["parts"]] == [(0.0, 4.5), (3.0, 8.0)] or len(pv["parts"]) == 1
        d = float(_probe(output_path(pv["file_id"]))["format"]["duration"])
        assert d == pytest.approx(sum(p["to"] - p["from"] for p in pv["parts"]), abs=0.4)
        v = next(s for s in _probe(output_path(pv["file_id"]))["streams"] if s["codec_type"] == "video")
        assert v["width"] <= 960                                                   # kicsiben, a munkamásolatból
        fin = res["final"]
        assert fin["job_id"] and fin["eta_s"] > 0 and fin["eta"].startswith("about") and "video" not in res
        assert any(x.startswith("Quick preview") for x in res["check"]["verified"])
        assert any("rendering in the background" in x for x in res["check"]["verified"])
        st = await jobs.wait_status(fin["job_id"], 1)
        assert st["status"] in ("running", "done") and st.get("final") is True
        # az előnézet nem vár a futó végleges renderre: a render kapuját valaki fogja, a szerkesztés mégis visszajön
        async with video._render_gate():
            t0 = time.time()
            ed = await video.edit(res["file_id"], {"layers": [{"id": "L2", "text": "Forrás: második"}]}, final="background")
            assert time.time() - t0 < 120 and ed["preview"]["parts"][0]["what"].startswith("L2")
            ed_job = ed["final"]["job_id"]
            assert (await jobs.wait_status(ed_job, 0.1))["status"] == "running"     # vár a kapura
        for jid in (fin["job_id"], ed_job):
            for _ in range(40):
                st = await jobs.wait_status(jid, 15)
                if st["status"] != "running":
                    break
            assert st["status"] == "done", st
            out = output_path(st["result"]["file_id"])
            assert float(_probe(out)["format"]["duration"]) == pytest.approx(8.0, abs=0.3)
            assert {(s["width"], s["height"]) for s in _probe(out)["streams"] if s["codec_type"] == "video"} == {(1280, 720)}
    asyncio.run(main())


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_short_video_still_renders_now(tmp_path, put_file):
    """Rövid videó (a becslés a küszöb alatt): minden a régi módon, a válaszban a végleges MP4."""
    from app.tools import video
    fid = put_file(_clip(tmp_path / "short.mp4", secs=3))
    res = asyncio.run(video.overlay({"file_id": fid, "brand": "none",
                                     "layers": [{"at": 0.5, "dur": 1, "type": "source", "text": "Forrás: teszt"}]}))
    assert res.get("video") and "final" not in res and "preview" not in res
