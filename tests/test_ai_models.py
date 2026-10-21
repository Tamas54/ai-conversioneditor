"""A kie-modellek és az ellenőrzött árak (kie.ai/pricing, 2026-10-21: Wan 3.0, Topaz) a pricing_rules induló soraiként; a
becslés (Studio-kredit) a szabályból, a padló-szorzóval; az AI-klip beállításainak ellenőrzése (motor, módok nem
keverhetők, 2–30 s, referencia-limitek)."""
from __future__ import annotations

import pytest


def test_seeded_prices_match_the_verified_kie_table(db_client):
    from app import credits
    call = db_client.portal.call
    call(credits.seed_rules)
    got = {(r["operation"], r["model"], r["tier"]): float(r["provider_cost"]) for r in call(credits.rules)
           if r["provider"] == "kie"}
    assert got[("ai_clip", "wan/3-0-video", "480p")] == 8
    assert got[("ai_clip", "wan/3-0-video", "720p")] == 16
    assert got[("ai_clip", "wan/3-0-video", "1080p")] == 32
    assert got[("ai_clip", "wan/3-0-video-prime", "720p")] == 25.2
    assert got[("enhance", "topaz/video-upscale", "2x")] == 8 and got[("enhance", "topaz/video-upscale", "4x")] == 14
    assert not any("seedance" in m for _, m, _ in got)
    # az admin emelése újraindításkor (újra-seed) megmarad
    r = next(x for x in call(credits.rules) if x["model"] == "wan/3-0-video" and x["tier"] == "1080p")
    call(lambda: credits.rule_save({**r, "multiplier": 0.5}))
    call(credits.seed_rules)
    assert float(next(x for x in call(credits.rules) if x["model"] == "wan/3-0-video" and x["tier"] == "1080p")["multiplier"]) == 0.5
    call(lambda: credits.rule_save(r))                                  # visszaállítás (a többi teszt ára)


def test_clip_estimates(db_client):
    from app import credits
    from app.tools import ai_video
    call = db_client.portal.call
    call(credits.seed_rules)
    e = call(lambda: ai_video.estimate_clip({"prompt": "boat", "resolution": "480p", "duration": 4}))
    assert e["credits"] == 11 and e["provider_cost"] == 32 and e["engine"] == "wan-3"     # 4 s × 8 kie-kredit
    assert call(lambda: ai_video.estimate_clip({"prompt": "x"}))["credits"] == 26            # alap: Wan 3.0, 720p, 5 s
    assert call(lambda: ai_video.estimate_clip({"prompt": "x", "resolution": "1080p", "duration": 10}))["credits"] == 102
    assert call(lambda: ai_video.estimate_clip({"prompt": "x", "engine": "wan-3-fast"}))["credits"] == 41


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
    assert e["seconds"] == 3 and e["credits"] == 14 and "1280×960" in e["what"]    # ceil(3 × 14 × padló = 13,4)
    assert call(lambda: ai_video.estimate_enhance(fid, 2))["credits"] == 8           # ceil(3 × 8 × padló = 7,6)
    with pytest.raises(ai_video.AIVideoError, match="factor"):
        call(lambda: ai_video.estimate_enhance(fid, 3))


@pytest.mark.parametrize("opts, msg", [
    ({}, "Say what should happen"),
    ({"prompt": "x", "duration": 1}, "2–30"),
    ({"prompt": "x", "duration": 31}, "2–30"),
    ({"prompt": "x", "resolution": "4k"}, "resolution"),
    ({"prompt": "x", "draft": True}, "Drafts were a Seedance feature"),
    ({"prompt": "x", "engine": "kling"}, "engine"),
    ({"prompt": "x", "reference_images": [f"{i}.png" for i in range(11)]}, "reference_images: at most 10"),
    ({"prompt": "x", "aspect": "21:9"}, "aspect"),
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
    c = ai_video.clip_options({"prompt": "x"})
    assert (c["engine"], c["model"], c["resolution"], c["duration"]) == ("wan-3", "wan/3-0-video", "720p", 5)
    assert ai_video.clip_options({"prompt": "x", "engine": "wan-3-fast"})["model"] == "wan/3-0-video-prime"
