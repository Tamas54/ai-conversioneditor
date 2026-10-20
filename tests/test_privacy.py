"""Helyadat (GPS) megosztáskor és exportkor (OMLETT 4, adatvédelem): szintetikus iPhone-szerű klip (location + Apple
com.apple.quicktime.location.ISO6709, forgatás 90°) és GPS-es JPEG/PNG — valódi felvétel nélkül."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_share import world  # noqa: F401  (A, B, C fiók, bekapcsolt megosztás)

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg needed")
LOC = "+47.4979+019.0402/"


def gps_clip(path: Path, rotate: int = 90) -> Path:
    raw = path.with_name("raw-" + path.name)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25", "-f", "lavfi", "-i",
                    "sine=frequency=440", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(raw)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-c", "copy", "-metadata:s:v:0", f"rotate={rotate}",
                    "-metadata", f"location={LOC}", "-metadata", "com.apple.quicktime.location.ISO6709=+47.4979+019.0402+100.000/",
                    "-metadata", "com.apple.quicktime.make=Apple", "-movflags", "use_metadata_tags", str(path)], check=True)
    raw.unlink()
    return path


def gps_photo(path: Path) -> Path:
    from PIL import Image
    im = Image.new("RGB", (64, 48), (200, 100, 50))
    ex = Image.Exif()
    ex[0x010F], ex[0x0112] = "Apple", 6
    ex[0x8825] = {1: "N", 2: (47.0, 29.0, 52.44), 3: "E", 4: (19.0, 2.0, 24.72), 6: 100.0}
    im.save(path, exif=ex, **({"quality": 92} if path.suffix == ".jpg" else {}))
    return path


def _rotation(p: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream_side_data_list:stream_tags=rotate",
                         "-of", "json", str(p)], capture_output=True, text=True, check=True).stdout
    s = json.loads(out)["streams"][0]
    side = [d.get("rotation") for d in s.get("side_data_list") or [] if "rotation" in d]
    return int(side[0]) if side else -int((s.get("tags") or {}).get("rotate", 0))


def _tags(p: Path) -> list[str]:
    from app.storage import privacy
    return asyncio.run(privacy.location_tags(p))


def test_video_location_stripped_rotation_and_mtime_kept(tmp_path):
    from app.storage import privacy
    clip = gps_clip(tmp_path / "IMG_0001.MOV")
    assert set(_tags(clip)) == {"format:location", "format:location-eng", "format:com.apple.quicktime.location.ISO6709"}
    rot, mtime = _rotation(clip), clip.stat().st_mtime_ns
    assert rot in (90, -270, -90, 270)
    assert asyncio.run(privacy.strip(clip)) is True
    assert _tags(clip) == [] and _rotation(clip) == rot and clip.stat().st_mtime_ns == mtime
    assert asyncio.run(privacy.strip(clip)) is False                       # tiszta fájl: nincs mit tenni


@pytest.mark.parametrize("ext", ["jpg", "png"])
@pytest.mark.parametrize("exiftool", [True, False])
def test_photo_gps_stripped_pixels_and_orientation_kept(tmp_path, monkeypatch, ext, exiftool):
    from PIL import Image
    from app.storage import privacy
    if exiftool and not shutil.which("exiftool"):
        pytest.skip("exiftool not installed")
    if not exiftool:
        real = shutil.which
        monkeypatch.setattr(privacy.shutil, "which", lambda n: None if n == "exiftool" else real(n))
    src = gps_photo(tmp_path / f"foto.{ext}")
    keep = tmp_path / f"orig.{ext}"
    shutil.copy(src, keep)
    assert _tags(src)
    assert asyncio.run(privacy.strip(src)) is True
    assert _tags(src) == []
    with Image.open(src) as a, Image.open(keep) as b:
        assert not a.getexif().get_ifd(0x8825)
        assert a.getexif().get(0x0112) == 6 and a.tobytes() == b.tobytes()


def test_download_and_preview_have_no_gps_original_untouched(client, tmp_path, put_file):
    from app.storage import input_path, public_url
    vid = put_file(gps_clip(tmp_path / "omlett.mov"))
    img = put_file(gps_photo(tmp_path / "tojas.jpg"))
    for fid, url in ((vid, public_url(vid)), (img, public_url(img)), (img, f"/preview/{img}")):
        r = client.get(url[url.index("/files/"):] if "/files/" in url else url)
        assert r.status_code == 200, r.text
        got = tmp_path / f"got-{fid}"
        got.write_bytes(r.content)
        assert _tags(got.rename(got.with_suffix("." + fid.rsplit(".", 1)[1]))) == [], url
        assert _tags(input_path(fid))                     # az eredeti a munkaterületen érintetlen (csak a kimenő tiszta)
    r = client.get(public_url(vid)[len("http://testserver"):], headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100  # lejátszók Range-kérése a tiszta másolatból


def test_export_flag_off_keeps_location(client, tmp_path, put_file, monkeypatch):
    from app.storage import public_url
    monkeypatch.setenv("STRIP_LOCATION", "0")
    vid = put_file(gps_clip(tmp_path / "marad.mov"))
    got = tmp_path / "got.mov"
    got.write_bytes(client.get(public_url(vid)[len("http://testserver"):]).content)
    assert _tags(got)


async def test_cloud_export_and_pipeline_outputs_leave_without_gps(tmp_path, put_file):
    """A sima ffmpeg-eszközök (átméretezés, fűzés) kimenete ÁTVISZI a location címkét (mérve, ffmpeg 4.4) — kifelé
    mégsem megy: az export (itt felhő) szűr."""
    import httpx
    from app import connectors
    from app.ops import run_tool
    from app.storage import privacy
    a, b = put_file(gps_clip(tmp_path / "a.mov")), put_file(gps_clip(tmp_path / "b.mov", rotate=0))
    outs = [(await run_tool("resize-video", [a], {"resolution": "640x360"}, charge=False))["file_id"],
            (await run_tool("merge-files", [a, b], {}, charge=False))["file_id"]]
    sent: list[bytes] = []

    def handler(req: httpx.Request):
        sent.append(req.content)
        return httpx.Response(200, json={"id": "x", "path_display": "/out/x"})
    await connectors.push("dropbox", {"path": "/out/", "access_token": "tok"}, [a, *outs], transport=httpx.MockTransport(handler))
    assert len(sent) == 3
    for i, (fid, data) in enumerate(zip([a, *outs], sent)):
        p = tmp_path / f"sent{i}.{fid.rsplit('.', 1)[1]}"
        p.write_bytes(data)
        assert await privacy.location_tags(p) == [], fid


def test_share_removes_location_unless_kept(world, tmp_path):
    from app.config import settings
    w = world
    clip = gps_clip(tmp_path / "IMG_0042.MOV").read_bytes()
    photo = gps_photo(tmp_path / "IMG_0043.jpg").read_bytes()
    v1, p1, v2 = w.upload("a", "IMG_0042.MOV", clip), w.upload("a", "IMG_0043.jpg", photo), w.upload("a", "IMG_0044.MOV", clip)
    root = settings.DATA_DIR / "u" / w.u["a"]["id"] / "inputs"
    r = w.req("a", "POST", "/api/share", json={"file_id": v1, "email": w.u["b"]["email"], "role": "viewer"}).json()
    assert r["location_removed"] == 1 and _tags(root / v1) == []
    r = w.req("a", "POST", "/api/share", json={"file_id": p1, "email": w.u["b"]["email"], "role": "viewer"}).json()
    assert r["location_removed"] == 1 and _tags(root / p1) == []
    r = w.req("a", "POST", "/api/share", json={"file_id": v2, "email": w.u["b"]["email"], "role": "viewer",
                                               "keep_location": True}).json()
    assert r["location_removed"] == 0 and _tags(root / v2)
