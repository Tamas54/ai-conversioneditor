"""A/B változatok (10-22, a MoneyPrinterTurbo ötletlistájából): ugyanabból a projektből 1–3 új változat — a kliens
adja a különbséget (más nyitás, zene…), vagy automatikusan, AI és költség nélkül a kinézet változik."""
from __future__ import annotations

import asyncio
import shutil

import pytest


def test_given_variants_are_edits_and_are_checked():
    from app.tools import video
    plans = video.variant_plan({}, {"variants": [{"title": "Másik nyitás"}, {"title": "Harmadik", "music": "dramatic"}]})
    assert [p["name"] for p in plans] == ["B", "C"] and plans[1]["changes"] == {"title": "Harmadik", "music": "dramatic"}
    for bad, msg in (({"variants": []}, "a list of 1–3"), ({"variants": [{}]}, "a list of 1–3"),
                     ({"variants": [{"title": str(i)} for i in range(4)]}, "At most 3"), ({"count": 5}, "1–3"),
                     ({"count": "sok"}, "how many")):
        with pytest.raises(ValueError, match=msg):
            video.variant_plan({}, bad)


def test_automatic_variants_change_the_look_not_the_content():
    from app.tools import video
    made = {"opts": {"accent": "#E63946", "transition": "mix", "music": "upbeat"}}
    plans = video.variant_plan(made, {"count": 3})
    assert [p["name"] for p in plans] == ["B", "C", "D"]
    accents = [p["changes"]["accent"] for p in plans]
    assert "#E63946" not in accents and len(set(accents)) == 3
    assert all(p["changes"]["transition"] != "mix" for p in plans)
    assert all(p["changes"]["music"] != "upbeat" for p in plans) and len({p["changes"]["music"] for p in plans}) == 3
    assert set(plans[0]["changes"]) == {"accent", "transition", "music"}                  # nincs cím / szöveg / kép
    own = video.variant_plan({"opts": {"music_file_id": "x.mp3"}}, {"count": 1})[0]["changes"]
    assert "music" not in own                                                              # a saját zene marad
    ov = video.variant_plan({"kind": "overlay", "opts": {"caption_style": "bold", "subtitles": [{"start": 0, "end": 1, "text": "x"}]}},
                            {"count": 2})
    assert [p["changes"]["caption_style"] for p in ov] == ["karaoke", "highlight"] and "transition" not in ov[0]["changes"]
    assert "caption_style" not in video.variant_plan({"kind": "overlay", "opts": {}}, {"count": 1})[0]["changes"]


def test_variants_need_a_made_video(client, put_file, tmp_path):
    src = tmp_path / "x.mp4"
    src.write_bytes(b"x")
    fid = put_file(src)
    r = client.post("/api/video/variants", json={"file_id": fid, "changes": {"count": 2}})
    assert r.status_code == 400


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI (EFFECTCRAFT_CLI) and ffmpeg needed")
def test_variants_are_their_own_projects(tmp_path, put_file):
    from PIL import Image
    from app.storage import display_label, read_meta
    from app.tools import video
    pics = []
    for col in ("red", "blue"):
        p = tmp_path / f"{col}.png"
        Image.new("RGB", (720, 1280), col).save(p)
        pics.append(put_file(p))
    made = asyncio.run(video.make({"mode": "text", "format": "vertical", "narration": "none", "title": "Eredeti cím",
                                   "duration": 10, "draft": True,
                                   "scenes": [{"headline": "Egy", "caption": "Első.", "image": pics[0]},
                                              {"headline": "Kettő", "caption": "Második.", "image": pics[1]}]}))
    pid = made["project"]["file_id"]
    res = asyncio.run(video.variants(pid, {"variants": [{"title": "Más nyitás", "accent": "#2A9D8F"}]}, draft=True))
    a, b = res["variants"]
    assert a["name"] == "A" and a["project"]["file_id"] == pid
    bid = b["project"]["file_id"]
    m = read_meta(bid)
    assert m["spec"]["story"]["title"] == "Más nyitás" and m["variant"] == "B" and m["variant_of"] == pid
    assert display_label(bid).endswith("variant B.ecproj") and b["video"]["file_id"].endswith(".mp4")
    assert read_meta(pid)["spec"]["story"]["title"] == "Eredeti cím"                     # az eredeti érintetlen
    assert "B — title" in res["check"]["verified"][0] or "B — accent, title" in res["check"]["verified"][0]


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI (EFFECTCRAFT_CLI) and ffmpeg needed")
def test_an_edit_keeps_the_caption_style(tmp_path, put_file):
    """10-22: az overlay-projekt nem tárolta a feliratstílust — egy rétegcsere a bold feliratot box-ra cserélte."""
    import subprocess
    from app.storage import read_meta
    from app.tools import video
    src = tmp_path / "f.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:size=640x360:rate=25:duration=3",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(src)], check=True)
    res = asyncio.run(video.overlay({"file_id": put_file(src), "caption_style": "bold", "render": False,
                                     "subtitles": [{"start": 0.2, "end": 2.5, "text": "Szia, itt vagyok."}]}))
    pid = res["project"]["file_id"]
    assert read_meta(pid)["spec"]["opts"]["caption_style"] == "bold"
    again = asyncio.run(video.edit(pid, {"title": "Új cím"}, render=False))
    assert read_meta(again["project"]["file_id"])["spec"]["opts"]["caption_style"] == "bold"
    plan = video.variant_plan(read_meta(pid)["spec"], {"count": 1})
    assert plan[0]["changes"]["caption_style"] == "karaoke"
    swapped = asyncio.run(video.edit(pid, plan[0]["changes"], render=False))
    assert read_meta(swapped["project"]["file_id"])["spec"]["opts"]["caption_style"] == "karaoke"
