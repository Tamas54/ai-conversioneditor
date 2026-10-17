"""Kép- és kameramozgás, háttér a videóban (10-17, Kommandant: „a kameramozgás (Ken Burns), zoom, a hátterek is
cserélődhetnek — pl. AI-fotók is”; „ezekre az eszközünknek általános jelleggel képesnek kell lennie”):

- jelenetenként: a kép mozgása (motion, focus, zoom), a teljes jelenet kamerája (camera), kép nélküli háttér (background),
  és a jelenet képsora (images: több háttér egymás után) — make-ben, jelenettervben és szerkesztésben ugyanígy;
- a saját felvétel kamerája (overlay camera: ráközelítés egy pillanatra);
- AI-fotó háttérnek: a webes felületen a mi FLUX-unk (kreditért), MCP-n a kliens készíti (10-12 elv)."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_scene_motion_fields_are_checked_and_kept():
    from app.tools import video
    sc = video._scene_from({"headline": "A", "caption": "B", "motion": "pan-left", "focus": [1.4, 0.2], "zoom": 3,
                            "camera": [{"at": "00:02.5", "zoom": 1.6, "to": [0.8, 0.5]}, {"at": 5, "zoom": 1}],
                            "background": "motion"}, "", [])
    assert sc["motion"] == "pan_left" and sc["focus"] == [1.0, 0.2] and sc["zoom"] == 1.8 and sc["background"] == "motion"
    assert [m["at"] for m in sc["camera"]] == [2.5, 5.0] and sc["camera"][0]["to"] == [0.8, 0.5]
    for bad in ({"motion": "spin"}, {"camera": "barrel_roll"}, {"camera": [{"at": "soon"}]}, {"background": "plaid"}):
        with pytest.raises(ValueError):
            video._scene_from({"headline": "A", **bad}, "", [])
    edited = video._scene_from({"camera": "none", "motion": "auto"}, "", [], sc)    # szerkesztés: kikapcsolható
    assert edited["camera"] is None and edited["motion"] is None and edited["background"] == "motion"


@pytest.mark.parametrize("cam", ["push_in", "pull_out", "drift",
                                 [{"at": 1, "zoom": 2.5, "to": [0.95, 0.05], "dur": 0.6}, {"at": 4, "zoom": 1, "dur": 0.8}]])
def test_camera_never_shows_the_frame_edge(cam):
    from app.tools import video
    W, H = 1920, 1080
    sc = video._scene_from({"camera": cam}, "", [])
    keys = video._camera_keys(sc["camera"], 6.0, W, H)
    assert keys and keys[0][0] == 0 and [k[0] for k in keys] == sorted(k[0] for k in keys)
    for t, z, x, y in keys:                                   # a nagyított jelenet mindig kitölti a keretet
        assert z >= 1 and abs(x - W / 2) <= W * (z - 1) / 2 + 0.5 and abs(y - H / 2) <= H * (z - 1) / 2 + 0.5
    if isinstance(cam, list):
        assert keys[1][1:] == keys[0][1:] and keys[-1][1] == 1      # kitart a mozdulásig, a végén vissza az egészre


def test_picture_specs_sequences_and_ai_photos():
    from app import tenancy
    from app.tools import video
    seq = video._img_spec([{"stock": "Szeged"}, "https://x.test/a.jpg", None, {"stock": "Tisza"}, {"stock": "five"}])
    assert seq == [{"stock": "Szeged"}, {"url": "https://x.test/a.jpg"}, {"stock": "Tisza"}]       # legfeljebb 4, üres kimarad
    assert video._img_spec([{"stock": "egy"}]) == {"stock": "egy"} and video._img_spec("none") is None
    assert video._img_spec({"generate": "foggy river at dawn"}) == {"generate": "foggy river at dawn"}   # webes út
    tok = tenancy.client_makes_images.set(True)
    try:
        with pytest.raises(ValueError, match="own image"):
            video._img_spec({"generate": "foggy river at dawn"})                 # MCP: a kliens készíti
    finally:
        tenancy.client_makes_images.reset(tok)


def test_compose_carries_sequences_motion_camera_and_background():
    from app.tools import video
    story = {"title": "T", "kicker": "", "end": {}, "layers": [], "scenes": [
        video._scene_from({"headline": "A", "caption": "Egy kép sorozat.", "motion": "zoom_out", "zoom": 1.4}, "", []),
        video._scene_from({"headline": "B", "caption": "Kamera.", "camera": "push_in"}, "", []),
        video._scene_from({"headline": "C", "caption": "Háttér.", "background": "#123456"}, "", [])]}
    D = video.compose(story, fmt="landscape", total=None, images=[None, ["a.jpg", "b.jpg", "c.jpg"], "d.jpg", None], accent=None,
                      credits="", voice=None, voice_s=None, music=None, focus={"b.jpg": [0.3, 0.4]})
    s1, s2, s3 = D["scenes"]
    assert [p["file"] for p in s1["pics"]] == ["a.jpg", "b.jpg", "c.jpg"] and s1["image"] == "a.jpg"
    assert all(p["motion"] == "kb-out" for p in s1["pics"]) and s1["zoom"] == 1.4 and s1["pics"][1]["focus"] == [0.3, 0.4]
    assert "pics" not in s2 and s2["camera"][-1][1] == 1.1 and s3["bg"] == "#123456" and s3["image"] is None


async def test_a_scene_sequence_downloads_every_picture_and_is_stored_as_a_list(tmp_path, monkeypatch):
    from PIL import Image
    from app.tools import images, video

    async def resolve(spec, aspect=None, max_px=1920):
        p = tmp_path / f"r{len(list(tmp_path.glob('r*.jpg')))}.jpg"
        Image.new("RGB", (64, 36), (120, 80, 40)).save(p)
        return images.ResolvedImage(path=p, attribution=f"Photo: {spec['stock']}", source_file_id=None)
    monkeypatch.setattr(images, "resolve", resolve)
    work = tmp_path / "work"
    work.mkdir()
    names, credits, per = await video._materialize_images([{"stock": "title"}, [{"stock": "a"}, {"stock": "b"}], None], "landscape", work)
    assert names[0] == "picture-01.jpg" and names[1] == ["picture-02.jpg", "picture-02b.jpg"] and names[2] is None
    assert per[1] == ["Photo: a", "Photo: b"] and all((work / n).exists() for n in ["picture-01.jpg", *names[1]])


async def test_ai_photo_mode_makes_ai_specs_on_the_web_and_is_refused_over_mcp(monkeypatch):
    from app import tenancy
    from app.tools import video
    got = {}

    async def assemble(story, specs, opts, **kw):
        got["specs"] = specs
        return {"ok": True}
    monkeypatch.setattr(video, "_assemble", assemble)
    plan = {"mode": "text", "format": "landscape", "narration": "none", "pictures": "ai", "title": "Folyó",
            "scenes": [{"headline": "A Tisza", "caption": "Reggeli köd a folyón.", "image_query": "foggy river at dawn"},
                       {"headline": "Szám", "caption": "Kép nélkül.", "background": "motion"}]}
    await video.make(plan)
    title, s1, s2 = got["specs"]
    assert title["generate"].startswith("foggy river at dawn") and s1["generate"].startswith("foggy river at dawn") and s2 is None
    tok = tenancy.client_makes_images.set(True)
    try:
        with pytest.raises(ValueError, match="own image"):
            await video.make(plan)
    finally:
        tenancy.client_makes_images.reset(tok)


def test_footage_camera_uses_the_same_moves_in_absolute_time():
    from app.tools import video
    cam = video._footage_camera([{"at": "01:02", "zoom": 1.5, "to": [0.3, 0.3]}, {"at": "01:06", "zoom": 1}])
    assert [m["at"] for m in cam] == [62.0, 66.0]
    keys = video._camera_keys(cam, 90.0, 1920, 1080)
    assert keys[1][0] == 62.0 and keys[2][1] == 1.5 and keys[-1][1] == 1
    assert video._footage_camera("none") is None


def test_own_landscape_photo_in_a_vertical_video_is_shown_whole(tmp_path):
    """fit=auto: a saját fekvő csoportkép álló videóban egészében látszik (elmosott kitöltéssel), a stockkép kitölt."""
    from PIL import Image
    from app.tools import video
    Image.new("RGB", (1600, 1200)).save(tmp_path / "own.jpg")
    Image.new("RGB", (1600, 1200)).save(tmp_path / "stock.jpg")
    Image.new("RGB", (1080, 1800)).save(tmp_path / "tall.jpg")
    fits = video._auto_fits(["own.jpg", "stock.jpg", ["tall.jpg"]], ["abc.jpg", {"stock": "x"}, ["def.jpg"]], tmp_path, 1080 / 1920)
    assert fits == {"own.jpg": "contain"}
    sc = video._scene_from({"headline": "A", "fit": "cover"}, "", [])
    assert sc["fit"] == "cover" and video._scene_from({"fit": "auto"}, "", [], sc)["fit"] is None
    with pytest.raises(ValueError):
        video._scene_from({"fit": "stretch"}, "", [])
    story = {"title": "T", "kicker": "", "end": {}, "layers": [], "scenes": [sc]}
    D = video.compose(story, fmt="vertical", total=None, images=[None, "own.jpg"], accent=None, credits="", voice=None,
                      voice_s=None, music=None, fits=fits)
    assert D["scenes"][0]["fit"] == "cover"                                   # a jelenet kifejezett kérése nyer
