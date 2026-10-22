"""Videó háttér nélkül (10-22): zöld / kék vászon kulcsolása (ffmpeg chromakey + despill, AI és letöltött modell
nélkül) → új háttér vagy átlátszó videó; az átlátszó videó a pip-rétegben keret nélkül, az alfájával; az avatar
háttere a fotóról levéve, zöld elé, a kész videóból kikulcsolva."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from test_ai_clip import _as, _job, _put_as, _user
from test_avatar import AvatarProv, _face, _wav

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg needed")


def _person(path: Path) -> Path:
    im = Image.new("RGBA", (360, 620), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((20, 250, 340, 620), 60, fill=(48, 56, 72, 255))
    d.ellipse((95, 40, 265, 250), fill=(224, 176, 144, 255))
    d.ellipse((90, 20, 270, 140), fill=(70, 50, 40, 255))
    im.save(path)
    return path


def _screen_clip(tmp_path: Path, color: str = "0x00B140", secs: float = 3) -> Path:
    """Zöld (enyhén egyenetlenül megvilágított) vászon előtt mozgó alak, hanggal."""
    out = tmp_path / f"screen_{color}.mp4"
    small = tmp_path / "p_small.png"
    Image.open(_person(tmp_path / "p.png")).resize((220, 380)).save(small)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=960x540:r=25:d={secs}", "-loop", "1",
                    "-i", str(small), "-f", "lavfi", "-i", f"sine=frequency=220:duration={secs}",
                    "-filter_complex", "[0:v]geq=r='r(X,Y)':g='g(X,Y)*(0.82+0.18*X/W)':b='b(X,Y)'[g];"
                    "[g][1:v]overlay=x='300+60*t':y=110:shortest=1[v]",
                    "-map", "[v]", "-map", "2:a", "-t", str(secs), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(out)], check=True)
    return out


def test_the_screen_is_found_from_the_frame_edges():
    from app.tools import video_key
    g = np.zeros((90, 160, 3), np.uint8)
    g[:] = (0, 160, 60)
    g[:, :, 1] = (g[:, :, 1] * np.linspace(0.8, 1.0, 160)).astype(np.uint8)    # egyenetlen fény
    g[30:90, 60:100] = (220, 180, 150)                                       # az alak (a széltől távol)
    s = video_key.screen_of([g, g])
    assert s["kind"] == "green" and s["share"] > 0.9 and 0.06 <= s["similarity"] <= 0.3
    b = np.zeros((90, 160, 3), np.uint8)
    b[:] = (10, 60, 190)
    assert video_key.screen_of([b])["kind"] == "blue"
    room = np.random.default_rng(1).integers(0, 255, (90, 160, 3), dtype=np.uint8)
    assert video_key.screen_of([room]) is None and video_key.screen_of([np.full((90, 160, 3), 128, np.uint8)]) is None
    assert video_key.key_of("green") == "#00B140" and video_key.key_of("#00ff00") == "#00FF00" and video_key.key_of(None) is None
    with pytest.raises(ValueError, match="auto"):
        video_key.key_of("piros")
    assert video_key.background_of("transparent") is None and video_key.background_of("#112233") == {"color": "#112233"}


def test_cutout_makes_a_see_through_video_and_a_new_background(tmp_path, put_file):
    from app.storage import output_path
    from app.tools import video, video_key
    fid = put_file(_screen_clip(tmp_path))
    res = asyncio.run(video_key.cutout({"file_id": fid}))
    assert res["screen"]["kind"] == "green" and res["check"]["status"] == "ok", res["check"]
    webm = res["files"][0]
    assert webm["file_id"].endswith(".webm") and webm["alpha"] and asyncio.run(video.has_alpha(output_path(webm["file_id"])))
    seen = next(v for v in res["check"]["verified"] if v.startswith("Transparent .webm"))
    assert "see-through" in seen
    proof = json.loads(json.dumps(asyncio.run(video._alpha_proof(output_path(webm["file_id"]), 1.5))))
    assert proof["transparent"] > 0.6 and proof["graphics"] > 0.05
    on = asyncio.run(video_key.cutout({"file_id": fid, "background": "#112233"}))
    mp4 = output_path(on["files"][0]["file_id"])
    png = tmp_path / "f.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1.5", "-i", str(mp4), "-frames:v", "1", str(png)], check=True)
    im = Image.open(png).convert("RGB")
    corner = im.getpixel((15, 15))
    assert all(abs(a - b) < 14 for a, b in zip(corner, (0x11, 0x22, 0x33))), corner         # a zöld helyén az új szín
    assert sum(1 for x in range(0, im.width, 6) for y in range(0, im.height, 6)
               if (lambda p: p[1] > p[0] + 60 and p[1] > p[2] + 60)(im.getpixel((x, y)))) < 15   # zöld nem maradt


def test_no_screen_says_so(tmp_path, put_file):
    from app.tools import video_key
    src = tmp_path / "room.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    with pytest.raises(ValueError, match="no green or blue screen"):
        asyncio.run(video_key.cutout({"file_id": put_file(src)}))


def test_a_transparent_cut_in_keeps_its_alpha_and_the_pip_loses_its_frame(tmp_path, put_file):
    from app.storage import output_path
    from app.tools import video, video_key
    res = asyncio.run(video_key.cutout({"file_id": put_file(_screen_clip(tmp_path))}))
    src = output_path(res["files"][0]["file_id"])
    cut = asyncio.run(video._cut_clip(src, (0.5, 1.0), tmp_path / "c.webm", alpha=True))
    assert asyncio.run(video.has_alpha(cut)) and not asyncio.run(video.has_alpha(_screen_clip(tmp_path)))
    ly, pip = video._pip_layer({"type": "pip", "image": "x.webm"}, "L1")
    ly["pip"] = pip
    video._pip_geometry(ly, 1080, 1920)
    assert "_skip" not in ly["chart"]["elements"][0]
    ly["chart"]["elements"][1]["alpha"] = True
    video._pip_geometry(ly, 1080, 1920)
    assert ly["chart"]["elements"][0]["_skip"] is True                                   # keret nélkül


class GreenAvatarProv(AvatarProv):
    """A „Kling” a feltöltött (zöld hátterű) fotót mozgatja: a kész videó a kép, hanggal."""
    async def fetch_result(self, st):
        from app.storage import new_file_id, output_path
        img = next(p for p in self.uploads if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
        fid = new_file_id("mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", str(img), "-f", "lavfi", "-i",
                        "sine=frequency=300:duration=3", "-vf", "scale=640:-2", "-t", "3", "-c:v", "libx264", "-pix_fmt",
                        "yuv420p", "-c:a", "aac", "-shortest", str(output_path(fid))], check=True)
        return [fid]


@pytest.fixture()
def gprov(db_client, monkeypatch):
    from app import credits, providers
    from app.tools import ai_video, cutout
    p = GreenAvatarProv()
    monkeypatch.setattr(providers, "get", lambda name="kie": p)
    monkeypatch.setattr(providers, "enabled_for", lambda uid=None: True)
    monkeypatch.setattr(ai_video, "POLL_FIRST_S", 0.05)
    monkeypatch.setattr(cutout, "available", lambda: True)

    async def cut(src, dst, **kw):                    # a fotó kivágása: az alak marad, a többi átlátszó
        with Image.open(src) as im:
            W, H = im.size
            out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            out.paste(Image.open(_person(Path(dst).with_name("person.png"))).resize((W // 2, H * 3 // 4)), (W // 4, H // 4))
            out.save(dst)
        return {"status": "ok"}
    monkeypatch.setattr(cutout, "remove_background", cut)
    db_client.portal.call(credits.seed_rules)
    return p


def test_a_talking_avatar_without_its_background(db_client, gprov, tmp_path):
    from app.tools import ai_video
    call = db_client.portal.call
    uid, _ = _user(call, 200)
    face = _put_as(uid, _face(tmp_path / "face.png"), "png")
    voice = _put_as(uid, _wav(tmp_path / "v.wav", 3.0), "wav")
    with pytest.raises(ai_video.AIVideoError, match="transparent avatar goes over"):
        call(_as(uid, lambda: ai_video.start_avatar({"image": face, "audio": voice, "consent": True, "background": "transparent",
                                                      "at": 2}, "p.ecproj")))
    _, j = _job(call, gprov, uid, lambda: ai_video.start_avatar({"image": face, "audio": voice, "consent": True,
                                                                  "background": "transparent"}))
    assert j["status"] == "done", j.get("error")
    up = Image.open(gprov.uploads[0]).convert("RGB")
    assert all(abs(a - b) < 12 for a, b in zip(up.getpixel((5, 5)), ai_video.GREEN))          # a zöld vászon a fotó mögött
    r = j["result"]
    assert r["file_id"].endswith(".webm") and r["background"] == "transparent" and r["green_screen"]["file_id"].endswith(".mp4")
    assert r["cutout_check"]["status"] == "ok", r["cutout_check"]
    from app.storage import output_path

    async def both():
        return output_path(r["file_id"]).exists(), output_path(r["green_screen"]["file_id"]).exists()
    assert call(_as(uid, both)) == (True, True)                                           # a zöld változat is megmarad
    _, j2 = _job(call, gprov, uid, lambda: ai_video.start_avatar({"image": face, "audio": voice, "consent": True,
                                                                   "background": "#112233"}))
    assert j2["result"]["file_id"].endswith(".mp4") and j2["result"]["background"] == "replaced"
    with pytest.raises(ai_video.AIVideoError, match="background"):
        ai_video.avatar_background({"nope": 1})
