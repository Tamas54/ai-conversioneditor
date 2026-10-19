"""OMLETT 5. fázis (a videós apróságok, 10-19): make — külön scrim, olvasható piros felcím a fotón, a fix hossz kiegyenlíti
az áttűnéseket; opacity 0–1 egységesen; a grafikai réteg címkéje; status wait; a jobs tool a videós jobot is látja;
gyors draft-render; „Pictures and animations” a kreditben, ha Lottie is van; a be nem töltött grafika-elem finding."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


def _story(n: int = 3, dur: float = 2.5) -> dict:
    return {"title": "Omlett", "kicker": "Tamás TV",
            "scenes": [{"id": f"s{i}", "headline": f"Lépés {i}", "caption": "Felverjük a tojást.", "duration": dur} for i in range(1, n + 1)],
            "end": {"title": "", "subtitle": ""}}


def test_fixed_length_compensates_the_crossfades_of_given_scene_lengths():
    """10-19: 10 s kérve, 3 × 2,5 s-os jelenet → 8,9 s lett (cím + zárókép + 4 áttűnés átfedése)."""
    from app.tools import video
    kw = dict(fmt="square", images=[None] * 4, accent=None, credits="", voice=None, voice_s=None, music=None)
    D = video.compose(_story(), total=10.0, fill=True, **kw)
    assert abs(D["total"] - 10.0) < 0.02 and all(sc["dur"] > 2.5 for sc in D["scenes"])
    assert abs(video.compose(_story(), total=10.0, **kw)["total"] - 8.9) < 0.02          # narration mód: a hossz a sajátja
    long_ = video.compose(_story(3, 5.0), total=10.0, fill=True, **kw)                  # túl hosszú jelenetek: nem rövidül
    assert long_["total"] > 13


def test_opacity_is_stored_as_0_to_1_and_drawn_as_percent():
    from app.tools.video import _graphic_elements, _graphic_from_plan
    ch, _ = _graphic_from_plan({"type": "graphic", "elements": [
        {"shape": "rect", "x": 0.5, "y": 0.5, "w": 0.2, "h": 0.1, "opacity": 0.94},
        {"shape": "rect", "x": 0.5, "y": 0.5, "w": 0.2, "h": 0.1, "opacity": 55},
        {"shape": "rect", "x": 0.5, "y": 0.5, "w": 0.2, "h": 0.1, "opacity": 1}]}, "")
    assert [e["opacity"] for e in ch["elements"]] == [0.94, 0.55, 1.0]                # a mentett spec: 0–1 (eddig 94.0)
    assert [e["opacity"] for e in _graphic_elements(ch, 1080, 1920, [1, 0, 0])] == [94, 55, 100]
    old = {"type": "graphic", "elements": [{"shape": "rect", "x": 0.5, "y": 0.5, "w": 0.2, "h": 0.1, "opacity": 94.0}]}
    assert _graphic_elements(old, 1080, 1920, [1, 0, 0])[0]["opacity"] == 94           # régi, százalékos spec is jó


def test_graphic_layers_are_listed_by_their_title_or_first_text():
    from app.tools import video
    ls = video.layers_from([{"at": 1, "type": "graphic", "elements": [{"shape": "rect", "x": 0.5, "y": 0.5},
                                                                      {"shape": "text", "text": "RENDKÍVÜLI KIADÁS", "x": 0.5, "y": 0.1}]},
                            {"at": 2, "type": "graphic", "title": "Útvonal", "elements": [{"shape": "text", "text": "A", "x": 0.5, "y": 0.5}]},
                            {"at": 3, "type": "graphic", "elements": [{"shape": "circle", "x": 0.5, "y": 0.5}]}], "", [])
    assert [r["text"] for r in video.layer_listing(ls)] == ["graphic “RENDKÍVÜLI KIADÁS”", "graphic “Útvonal”", "graphic (1 element)"]


def test_credit_line_says_animations_whenever_a_lottie_is_in_the_video():
    from app.tools.video import _credit_line
    story = {"layers": [{"id": "L1", "chart": {"type": "graphic", "elements": [{"shape": "lottie", "file": "gfx-01.json"}]}}]}
    full, short = _credit_line(["Photo: A (Pexels) — https://x.y/1", "LottieFiles: Confetti by Kim"], story)
    assert full.startswith("Pictures and animations: ") and short == "Pictures and animations: A (Pexels) · LottieFiles: Confetti by Kim"
    assert _credit_line(["Photo: A (Pexels)"], {"scenes": []})[0].startswith("Pictures: ")


def test_status_waits_and_the_jobs_tool_sees_video_jobs():
    from app import jobs, mcp_ext, mcp_server

    async def main():
        async def work(progress):
            await asyncio.sleep(0.4)
            return {"file_id": "x.mp4"}
        jid = jobs.start("video", work)["id"]
        st = await mcp_server.video(action="status", job_id=jid, wait=5)
        assert st["status"] == "done" and st["result"]["file_id"] == "x.mp4"
        jid2 = jobs.start("video", work)["id"]
        j = await mcp_ext.jobs(action="status", job_id=jid2, wait=5)            # eddig: „No such job.”
        assert j["status"] == "done" and j["result"]["file_id"] == "x.mp4" and "video" in j["note"]
        with pytest.raises(ValueError, match="No such job"):
            await mcp_ext.jobs(action="status", job_id="nincs-ilyen")
    asyncio.run(main())


def test_make_takes_scrim_and_scenes_keep_their_own():
    from app.tools import video
    notes: list = []
    sc = video._scene_from({"headline": "H", "caption": "C", "scrim": 0.2}, "", notes)
    assert sc["scrim"] == 0.2 and video._scene_from({"headline": "H", "scrim": 40}, "", notes)["scrim"] == 0.4
    D = video.compose({**_story(2), "scenes": [sc, {"id": "s2", "headline": "x", "caption": "y"}]}, fmt="square", total=10.0,
                      images=[None] * 3, accent=None, credits="", voice=None, voice_s=None, music=None, scrim=0.0)
    assert D["title"]["scrim"] == 0.0 and D["scenes"][0]["scrim"] == 0.2 and D["scenes"][1]["scrim"] == 0.0


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_make_scrim_kicker_backdrop_and_fixed_length(tmp_path, put_file):
    from app.storage import output_path
    from app.tools import video
    pic = tmp_path / "p.jpg"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=800x800", "-frames:v", "1", str(pic)], check=True)
    fid = put_file(pic)
    scenes = [{"headline": f"Lépés {i}", "caption": "Felverjük a tojást.", "duration": 2.5, "image": fid} for i in range(1, 4)]
    base = {"mode": "article", "scenes": scenes, "title": "Omlett", "kicker": "Rendkívüli", "title_image": fid, "format": "square",
            "pictures": "mine", "narration": "none", "duration": 10, "render": False}
    res = asyncio.run(video.make({**base, "scrim": 0}))
    assert abs(res["duration"]["actual"] - 10.0) < 0.05, res["duration"]                 # 10 s kérve → 10 s (eddig 8,9)
    proj = output_path(res["file_id"]).read_text("utf-8")
    assert '"Shade"' not in proj                                                          # scrim 0: nincs sötétítés
    assert "Kicker backdrop" in proj                                                      # a piros felcím sötét alapon
    res2 = asyncio.run(video.make(base))
    assert '"Shade"' in output_path(res2["file_id"]).read_text("utf-8")


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_draft_render_and_failed_graphic_elements_are_findings(tmp_path, put_file):
    from app.tools import video
    clip = tmp_path / "c.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10", "-t", "3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True)
    layers = [{"id": "G", "at": 0.5, "dur": 2, "type": "graphic", "elements": [
        {"shape": "text", "text": "Hello", "x": 0.3, "y": 0.3}, {"shape": "lottie", "lottie": "nincs-ilyen.json", "x": 0.7, "y": 0.5}]}]
    res = asyncio.run(video.overlay({"file_id": put_file(clip), "layers": layers, "draft": True}))
    v = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                                   str(video.output_path(res["video"]["file_id"]))], capture_output=True, text=True).stdout)["streams"][0]
    assert (v["width"], v["height"]) == (320, 180) and "(draft)" in res["video"]["label"]
    assert video.frame_time(res["file_id"], "G")[0] == res["video"]["file_id"]           # az előnézet a draftot mutatja
    assert any(f.startswith("Layer G, element 2 (lottie)") and "left out" in f for f in res["check"]["findings"]), res["check"]
