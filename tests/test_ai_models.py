"""OMLETT 2.2: a kie-modellek és az ellenőrzött árak (kie.ai/pricing, 2026-10-19) a pricing_rules induló soraiként; a
becslés (Studio-kredit) a szabályból; az AI-klip beállításainak ellenőrzése (módok nem keverhetők, 4–30 s, draft = 480p)."""
from __future__ import annotations

import pytest


def test_seeded_prices_match_the_verified_kie_table(db_client):
    from app import credits
    call = db_client.portal.call
    call(credits.seed_rules)
    got = {(r["operation"], r["model"], r["tier"]): float(r["provider_cost"]) for r in call(credits.rules)
           if r["provider"] == "kie"}
    assert got[("ai_clip", "bytedance/seedance-2-5", "480p")] == 28
    assert got[("ai_clip", "bytedance/seedance-2-5", "720p")] == 63
    assert got[("ai_clip", "bytedance/seedance-2-5", "1080p")] == 158
    assert got[("ai_clip", "bytedance/seedance-2-5", "720p+video")] == 38
    assert got[("enhance", "topaz/video-upscale", "2x")] == 8 and got[("enhance", "topaz/video-upscale", "4x")] == 14
    # az admin módosítása újraindításkor (újra-seed) megmarad
    r = next(x for x in call(credits.rules) if x["tier"] == "1080p+video")
    call(lambda: credits.rule_save({**r, "multiplier": 0.2}))
    call(credits.seed_rules)
    assert float(next(x for x in call(credits.rules) if x["tier"] == "1080p+video")["multiplier"]) == 0.2


def test_clip_estimates(db_client):
    from app import credits
    from app.tools import ai_video
    call = db_client.portal.call
    call(credits.seed_rules)
    e = call(lambda: ai_video.estimate_clip({"prompt": "boat", "draft": True, "duration": 4}))
    assert e["credits"] == 17 and e["provider_cost"] == 112          # 4 s × 28 kie-kredit (élesben mérve: 112), × 0.15
    assert call(lambda: ai_video.estimate_clip({"prompt": "x", "resolution": "720p", "duration": 5}))["credits"] == 48
    assert call(lambda: ai_video.estimate_clip({"prompt": "x", "resolution": "1080p", "duration": 10}))["credits"] == 237


def test_enhance_estimate_from_the_video_length(db_client, tmp_path, put_file):
    import subprocess
    from app import credits
    from app.tools import ai_video
    src = tmp_path / "c.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2.5",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    fid = put_file(src)
    call = db_client.portal.call
    call(credits.seed_rules)
    e = call(lambda: ai_video.estimate_enhance(fid, "4x"))
    assert e["seconds"] == 3 and e["credits"] == 7 and "1280×960" in e["what"]     # ceil(3 × 14 × 0.15 = 6.3)
    assert call(lambda: ai_video.estimate_enhance(fid, 2))["credits"] == 4           # ceil(3 × 8 × 0.15 = 3.6)
    with pytest.raises(ai_video.AIVideoError, match="factor"):
        call(lambda: ai_video.estimate_enhance(fid, 3))


@pytest.mark.parametrize("opts, msg", [
    ({}, "Say what should happen"),
    ({"prompt": "x", "duration": 3}, "4–30"),
    ({"prompt": "x", "duration": 31}, "4–30"),
    ({"prompt": "x", "resolution": "4k"}, "resolution"),
    ({"prompt": "x", "draft": True, "resolution": "720p"}, "always 480p"),
    ({"prompt": "x", "last_frame": "a.png"}, "needs a first frame"),
    ({"prompt": "x", "first_frame": "a.png", "reference_images": ["b.png"]}, "can't be mixed"),
    ({"prompt": "x", "aspect": "2:1"}, "aspect"),
])
def test_clip_options_refuse_clearly(opts, msg):
    from app.tools import ai_video
    with pytest.raises(ai_video.AIVideoError, match=msg):
        ai_video.clip_options(opts)


def test_clip_modes():
    from app.tools import ai_video
    assert ai_video.clip_options({"prompt": "x"})["mode"] == "text"
    c = ai_video.clip_options({"prompt": "x", "image": "f.png"})
    assert c["mode"] == "first_frame" and c["aspect"] == "adaptive" and c["resolution"] == "720p" and c["audio"] is False
    assert ai_video.clip_options({"prompt": "x", "first_frame": "a", "last_frame": "b"})["mode"] == "first_last"
    assert ai_video.clip_options({"prompt": "x", "reference_images": ["a", "b"]})["mode"] == "reference"
    assert ai_video.clip_options({"prompt": "x", "draft": True})["resolution"] == "480p"
