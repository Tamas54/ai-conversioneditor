"""Nehéz munka és a régi feltöltés (10-23, kódátnézés).

- a videó- és hangkódolás (fájleszközök, konverter) közös kapun át fut: egyszerre HEAVY_JOB_SLOTS, a többi job
  „queued”, a helyével és egy mondattal; sorra kerülve fut; a sorból leállított job helyét a következő kapja;
- a kapu munkánként foglal, nem ffmpeg-folyamatonként: a beágyazott hívás és a munka al-taskjai átmennek;
- az ffmpeg a kapun belül HEAVY_JOB_THREADS szálon kódol (az x264 a fájlba is beírja);
- a régi /upload lemezre ír, a korlát fölött 413 ugyanazzal a szöveggel, mint a darabolt feltöltés — a megadott
  hosszon túli törzset el sem olvassa, hossz nélkül a korlátnál abbahagyja.
"""
from __future__ import annotations

import asyncio
import shutil
import threading
import time

import httpx
import pytest

MB = 1024 * 1024


def _until(cond, what: str, timeout: float = 10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        got = cond()
        if got:
            return got
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture()
def blocking_tool(monkeypatch, put_file, tmp_path):
    """A trim-video kezelője helyett egy, ami a bemenete nevére szóló jelig fut — így látszik, ki fut és ki vár."""
    from app.catalog import tools as T
    started: list[str] = []
    release: dict[str, threading.Event] = {}

    async def handler(inputs, o, out):
        name = inputs[0].name
        started.append(name)
        while not release[name].is_set():
            await asyncio.sleep(0.02)
        p = out / "trimmed.txt"
        p.write_text(name)
        return [p]
    monkeypatch.setattr(T, "handler", lambda key: handler)

    def start(client, label: str) -> tuple[str, str]:
        src = tmp_path / f"{label}.mp4"
        src.write_bytes(b"\x00\x00\x00\x18ftypmp42" + label.encode() * 64)
        fid = put_file(src)
        from app.storage import input_path
        release[input_path(fid).name] = threading.Event()
        r = client.post("/api/tools/run", json={"tool": "trim-video", "file_ids": [fid], "options": {"start": "1"}})
        assert r.status_code == 200, r.text
        return r.json()["id"], input_path(fid).name
    yield start, started, release
    for ev in release.values():                         # a kapu tiszta maradjon a többi tesztnek
        ev.set()


def _job(client, jid: str) -> dict:
    return client.get(f"/api/jobs/{jid}").json()


def test_third_heavy_job_waits_queued_with_its_place_then_runs(client, monkeypatch, blocking_tool):
    if not shutil.which("ffmpeg"):
        pytest.skip("trim-video needs ffmpeg on this machine")
    monkeypatch.setenv("HEAVY_JOB_SLOTS", "2")
    start, started, release = blocking_tool
    (j1, a), (j2, b) = start(client, "a"), start(client, "b")
    _until(lambda: {a, b} <= set(started), "the first two jobs to start")
    (j3, c), (j4, d) = start(client, "c"), start(client, "d")
    third = _until(lambda: (j := _job(client, j3))["status"] == "queued" and j, "the third job to queue")
    fourth = _until(lambda: (j := _job(client, j4))["status"] == "queued" and j, "the fourth job to queue")
    time.sleep(0.2)
    assert c not in started and d not in started               # két hely: a harmadik és a negyedik nem fut
    assert third["position"] == 1 and fourth["position"] == 2
    assert third["message"] == ("Waiting for a free slot: the server runs 2 video and audio jobs at a time, and yours "
                                "is next. It starts by itself.")
    assert "1 job is ahead of yours" in fourth["message"]
    assert _job(client, j1)["status"] == "running" and "position" not in _job(client, j1)

    release[a].set()                                            # az első kész → a harmadik indul, a negyedik előrelép
    _until(lambda: _job(client, j1)["status"] == "done", "the first job to finish")
    _until(lambda: c in started, "the third job to start")
    j = _job(client, j3)
    assert j["status"] == "running" and j["stage"] == "trim-video" and "position" not in j and "message" not in j
    assert _job(client, j4)["position"] == 1 and "yours is next" in _job(client, j4)["message"]
    assert d not in started

    for x in (b, c, d):
        release[x].set()
    for jid in (j2, j3, j4):
        _until(lambda: _job(client, jid)["status"] == "done", f"job {jid} to finish")
    assert started.index(c) < started.index(d)                  # érkezési sorrend
    assert _job(client, j3)["result"]["files"][0]["label"].endswith(".txt")


def test_a_job_stopped_in_the_queue_gives_its_place_on(client, monkeypatch, blocking_tool):
    if not shutil.which("ffmpeg"):
        pytest.skip("trim-video needs ffmpeg on this machine")
    monkeypatch.setenv("HEAVY_JOB_SLOTS", "1")
    start, started, release = blocking_tool
    j1, a = start(client, "a")
    _until(lambda: a in started, "the first job to start")
    (j2, b), (j3, c) = start(client, "b"), start(client, "c")
    _until(lambda: _job(client, j3).get("position") == 2, "the third job to queue behind the second")
    r = client.delete(f"/api/jobs/{j2}")
    assert r.status_code == 200
    _until(lambda: _job(client, j2)["status"] == "cancelled", "the queued job to stop")
    assert "position" not in _job(client, j2)
    assert _job(client, j3)["position"] == 1
    release[a].set()
    release[c].set()
    _until(lambda: _job(client, j3)["status"] == "done", "the third job to run")
    assert b not in started


async def test_the_gate_is_per_job_not_per_ffmpeg(monkeypatch):
    """A gyors render egy munkán belül párhuzamos ffmpeg-szeletekkel kódol: a beágyazott hívás és az al-taskok nem
    várnak a saját munkájukra (egy hellyel sem akad el)."""
    from app import jobs
    from app.convert import engines
    monkeypatch.setenv("HEAVY_JOB_SLOTS", "1")
    monkeypatch.setenv("HEAVY_JOB_THREADS", "3")
    args = ("ffmpeg", "-y", "-ss", "2", "-i", "a.mp4", "-i", "b.srt", "-c:v", "libx264", "-threads", "16", "out.mp4")
    assert engines._threads(args) == args                       # a kapun kívül: változatlan

    async def piece() -> tuple:
        async with jobs.heavy():
            await asyncio.sleep(0.01)
            return engines._threads(args)
    async with jobs.heavy():
        async with jobs.heavy():                                # konverzió egy eszközön belül
            pass
        got = await asyncio.wait_for(asyncio.gather(*(piece() for _ in range(4))), 5)
    assert got[0] == ("ffmpeg", "-filter_threads", "3", "-filter_complex_threads", "3", "-y", "-ss", "2",
                      "-threads", "3", "-i", "a.mp4", "-threads", "3", "-i", "b.srt", "-c:v", "libx264",
                      "-threads", "16", "-threads", "3", "out.mp4")
    assert jobs._heavy_running == 0 and not jobs._heavy_queue


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg needed")
async def test_ffmpeg_in_a_heavy_job_encodes_on_the_capped_threads(tmp_path, monkeypatch):
    from app import jobs
    from app.convert.engines import run
    monkeypatch.setenv("HEAVY_JOB_THREADS", "3")
    out = tmp_path / "t.mp4"
    async with jobs.heavy():
        await run("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=1:s=640x360",
                  "-vf", "scale=320:180", "-c:v", "libx264", "-threads", "12", str(out), timeout=60)
    assert b" threads=3 " in out.read_bytes()                   # az x264 a beállításait a fájlba írja


# ---------- a régi /upload ----------

def test_legacy_upload_goes_to_disk(client):
    from app.storage import input_path
    data = bytes(range(256)) * 9000                             # ~2,2 MB
    r = client.post("/upload", files={"file": ("adat.bin", data, "application/octet-stream")})
    assert r.status_code == 200, r.text
    fid = r.json()["file_id"]
    assert input_path(fid).read_bytes() == data and r.json()["size_kb"] == len(data) // 1024 and fid.endswith(".bin")
    assert not list(input_path(fid).parent.glob(".*.part"))
    assert client.post("/upload", data={"x": "1"}).status_code == 400          # nincs „file” mező


async def test_oversized_legacy_upload_is_refused_without_reading_it(client, monkeypatch):
    from app.main import app
    from app.storage import input_path, new_file_id
    monkeypatch.setenv("API_MAX_FILE_MB", "1")
    inputs = input_path(new_file_id("bin")).parent
    before = set(inputs.iterdir())
    want = client.post("/api/upload/chunks", json={"filename": "big.mov", "size": 50 * MB}).json()["detail"]
    assert want == "Files up to 1 MB."
    sent = {"n": 0}

    async def body():                                           # 50 MB multipart, darabonként; számoljuk, mennyit kért el
        head = b'--XX\r\nContent-Disposition: form-data; name="file"; filename="big.mov"\r\n\r\n'
        for block in [head] + [b"\x00" * (256 * 1024)] * 200 + [b"\r\n--XX--\r\n"]:
            sent["n"] += len(block)
            yield block
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        hdr = {"Content-Type": "multipart/form-data; boundary=XX"}
        r = await c.post("/upload", content=body(), headers={**hdr, "Content-Length": str(50 * MB + 120)})
        assert r.status_code == 413 and r.json()["detail"] == want
        assert sent["n"] == 0                                   # megadott hossz: a törzsből semmit nem olvasott
        r = await c.post("/upload", content=body(), headers=hdr)    # hossz nélkül (chunked)
        assert r.status_code == 413 and r.json()["detail"] == want
        assert sent["n"] <= 2 * MB                              # a korlát (1 MB + keret) után abbahagyta
    assert set(inputs.iterdir()) == before                      # se fájl, se félkész darab


async def test_guest_streaming_over_the_limit_is_offered_sign_in(client, monkeypatch):
    """Az app feltöltése is a közös 413-at mondja, akkor is, ha a hossz nincs megadva (a köztes réteg nem látja)."""
    from app.config import settings
    from app.main import app
    monkeypatch.setattr(settings, "DASHBOARD_PASSWORD", "pw")   # jelszó be → belépés nélkül vendég

    async def body():
        yield b'--XX\r\nContent-Disposition: form-data; name="file"; filename="big.mov"\r\n\r\n'
        for _ in range(26):
            yield b"\x00" * MB
        yield b"\r\n--XX--\r\n"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        r = await c.post("/api/upload", content=body(), headers={"Content-Type": "multipart/form-data; boundary=XX"})
    assert r.status_code == 413
    assert r.json()["detail"] == "Files up to 25 MB without an account — sign in or create a free one for bigger files."
