"""Mentés a Fotókba (OMLETT 3.1): a szerver a Fotókba illő fájlt adja (MP4/MOV H.264/HEVC + AAC, JPEG/PNG/HEIC),
a WebM-et előbb MP4-re alakítja, helyadat nélkül; a böngészőben a gomb navigator.share({files})-t hív, ha a
canShare igaz, különben letölt (Playwright, a Web Share API-t a teszt adja)."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from test_privacy import _tags, gps_clip, gps_photo

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg needed")


def _webm(path: Path) -> Path:
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25", "-f", "lavfi", "-i",
                        "sine=frequency=440", "-t", "1", "-c:v", "libvpx", "-b:v", "300k", "-c:a", "libvorbis", str(path)])
    if r.returncode:
        pytest.skip("no VP8/Vorbis encoder in this ffmpeg")
    return path


def _codecs(p: Path) -> list[str]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "json", str(p)],
                         capture_output=True, text=True, check=True).stdout
    return [s["codec_name"] for s in json.loads(out)["streams"]]


def _poll(client, job):
    for _ in range(300):
        if job.get("status") != "running":
            return job
        time.sleep(0.1)
        job = client.get(f"/api/jobs/{job['id']}").json()
    raise AssertionError(job)


def test_iphone_clip_goes_as_is_without_location(client, tmp_path, put_file):
    fid = put_file(gps_clip(tmp_path / "IMG_0001.MOV"))
    p = client.get(f"/api/photos/{fid}").json()
    assert p["type"] == "video/quicktime" and p["name"] == "IMG_0001.mov" and p["converted"] is False
    r = client.get(p["url"])
    assert r.status_code == 200 and r.headers["content-type"] == "video/quicktime"
    assert 'filename="IMG_0001.mov"' in r.headers["content-disposition"]
    got = tmp_path / "got.mov"
    got.write_bytes(r.content)
    assert _tags(got) == [] and _codecs(got)[:2] == ["h264", "aac"]


def test_webm_is_converted_to_mp4_first(client, tmp_path, put_file):
    fid = put_file(_webm(tmp_path / "klip.webm"))
    assert client.get(f"/api/photos/{fid}/file").status_code == 409          # előbb el kell készíteni
    job = client.get(f"/api/photos/{fid}").json()
    assert job["status"] == "running"
    res = _poll(client, job)["result"]
    assert res == {**res, "type": "video/mp4", "name": "klip.mp4", "converted": True}
    r = client.get(res["url"])
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4" and 'filename="klip.mp4"' in r.headers["content-disposition"]
    got = tmp_path / "got.mp4"
    got.write_bytes(r.content)
    assert _codecs(got)[:2] == ["h264", "aac"]
    again = client.get(f"/api/photos/{fid}").json()                      # második kérés: kész, a gyorsítótárból
    assert again["type"] == "video/mp4" and again["size"] == len(r.content)


def test_photos_and_pictures(client, tmp_path, put_file):
    jpg = put_file(gps_photo(tmp_path / "IMG_0002.jpg"))
    p = client.get(f"/api/photos/{jpg}").json()
    assert p["type"] == "image/jpeg" and p["converted"] is False
    got = tmp_path / "got.jpg"
    got.write_bytes(client.get(p["url"]).content)
    assert _tags(got) == []
    from PIL import Image
    Image.new("RGB", (40, 30), "red").save(tmp_path / "kep.webp")
    webp = put_file(tmp_path / "kep.webp")
    res = _poll(client, client.get(f"/api/photos/{webp}").json())
    res = res.get("result", res)
    assert res["type"] == "image/jpeg" and res["name"] == "kep.jpg"
    assert client.get(res["url"]).content[:2] == b"\xff\xd8"
    (tmp_path / "level.md").write_text("# x")
    (tmp_path / "p.ecproj").write_text("{}")
    doc, proj = put_file(tmp_path / "level.md"), put_file(tmp_path / "p.ecproj")
    assert client.get(f"/api/photos/{doc}").status_code == 415
    r = client.get(f"/api/photos/{proj}")
    assert r.status_code == 409 and "Render the video first" in r.json()["detail"]


# ---------- a gomb a böngészőben ----------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_url():
    uvicorn = pytest.importorskip("uvicorn")
    from app.main import app
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(5)


SHARE_API = """
window.__shared = null;
Navigator.prototype.canShare = function (d) { return (d.files || []).every(f => f.size < (window.__maxShare || 1e9)); };
Navigator.prototype.share = async function (d) { window.__shared = d.files.map(f => ({ name: f.name, type: f.type, size: f.size })); };
"""


@pytest.mark.skipif(not os.getenv("CHROMIUM_EXECUTABLE"), reason="Chromium needed")
def test_button_shares_the_file_or_downloads(live_url, samples):
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        br = p.chromium.launch(executable_path=os.environ["CHROMIUM_EXECUTABLE"])
        plain = br.new_page()                                   # asztali böngésző, Web Share nélkül: a gomb nem látszik
        plain.goto(f"{live_url}/app")
        fid = plain.evaluate("""async (b64) => {
          const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
          const fd = new FormData(); fd.append("file", new File([bytes], "omlett.mp4", { type: "video/mp4" }));
          return (await (await fetch("/api/upload", { method: "POST", body: fd })).json()).file_id;
        }""", __import__("base64").b64encode(samples.mp4().read_bytes()).decode())
        plain.goto(f"{live_url}/app/file/{fid}/preview")
        plain.locator("#btn-download").wait_for()
        assert plain.locator("#btn-photos").is_hidden()

        page = br.new_context(accept_downloads=True).new_page()
        page.add_init_script(SHARE_API)
        page.goto(f"{live_url}/app/file/{fid}/preview")
        btn = page.locator("#btn-photos")
        btn.wait_for()
        btn.click()
        page.wait_for_function("window.__shared !== null")
        shared = page.evaluate("window.__shared")
        assert shared[0]["type"] == "video/mp4" and shared[0]["name"] == "omlett.mp4" and shared[0]["size"] > 1000
        page.evaluate("window.__maxShare = 100")                 # ezt a fájlt a böngésző nem adja át → letöltés
        with page.expect_download() as dl:
            btn.click()
        assert dl.value.suggested_filename == "omlett.mp4"
        br.close()
