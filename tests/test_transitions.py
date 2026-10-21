"""Jelenetváltások (10-22, a vanta / MoneyPrinterTurbo ötletlistájából): mozgás (slide, push, zoom, dip) és az EffectCraft
átmeneti effektjei (wipe, clock, iris, blinds, cards…); a videó egészére vagy jelenetenként, a „mix” váltakozik."""
from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest


def test_transitions_per_cut_mix_and_scene_override():
    from app.tools import video
    story = {"scenes": [{"id": "s1"}, {"id": "s2", "transition": "cards"}, {"id": "s3"}]}
    assert video.transitions_for(story, None) == ["fade", "cards", "fade", "fade"]          # + a zárókártya
    mixed = video.transitions_for(story, "mix")
    assert mixed[1] == "cards" and mixed[0] != mixed[2] and len(mixed) == 4 and "mix" not in mixed
    assert video.transitions_for(story, "Push_Up")[0] == "push-up"
    with pytest.raises(ValueError, match="transition: fade"):
        video.transition_of("glitchy")
    with pytest.raises(ValueError, match="scene transition"):
        video._scene_from({"headline": "x", "caption": "y", "transition": "nope"}, "", [])
    assert video._scene_from({"headline": "x", "caption": "y", "transition": "iris"}, "", [])["transition"] == "iris"


def test_compose_carries_one_transition_per_cut():
    from app.tools import video
    story = {"title": "T", "scenes": [{"headline": f"H{i}", "caption": f"Caption {i}.", "id": f"s{i}"} for i in range(1, 4)]}
    D = video.compose(story, fmt="landscape", total=20.0, images=[None] * 4, accent=None, credits="", voice=None, voice_s=None,
                      music=None, transition="wipe")
    assert D["transitions"] == ["wipe"] * 4


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI (EFFECTCRAFT_CLI) and ffmpeg needed")
def test_a_wipe_shows_both_scenes_side_by_side_mid_cut(tmp_path, put_file):
    """Két egyszínű jelenet (piros → kék) törléssel: a váltás közepén az egyik oldal még piros, a másik már kék — áttűnésnél
    a két oldal egyforma lila lenne."""
    from PIL import Image
    from app.storage import output_path
    from app.tools import video
    pics = []
    for col in ("red", "blue"):
        p = tmp_path / f"{col}.png"
        Image.new("RGB", (1280, 720), col).save(p)
        pics.append(put_file(p))
    res = asyncio.run(video.make({"mode": "text", "format": "landscape", "narration": "none", "title": "W",
                                  "duration": 12, "transition": "wipe",
                                  "scenes": [{"headline": "", "caption": "", "image": pics[0], "motion": "still"},
                                             {"headline": "", "caption": "", "image": pics[1], "motion": "still"}]}))
    from app.storage import read_meta
    tl = {x["id"]: x["start"] for x in read_meta(res["project"]["file_id"])["spec"]["timeline"]}
    png = tmp_path / "mid.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(tl["s2"] + 0.25), "-i", str(output_path(res["video"]["file_id"])),
                    "-frames:v", "1", str(png)], check=True)
    im = Image.open(png).convert("RGB")
    W, H = im.size
    left, right = im.getpixel((int(W * 0.15), int(H * 0.3))), im.getpixel((int(W * 0.85), int(H * 0.3)))
    assert left[2] > left[0] + 40 and right[0] > right[2] + 40, (left, right)       # balra már kék, jobbra még piros
