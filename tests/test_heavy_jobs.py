"""Nehéz munka és a régi feltöltés (10-23, kódátnézés).

- a videó- és hangkódolás (fájleszközök, konverter) közös kapun át fut: egyszerre HEAVY_JOB_SLOTS, a többi job
  „queued”, a helyével és egy mondattal; sorra kerülve fut; a sorból leállított job helyét a következő kapja;
- a kapu munkánként foglal, nem ffmpeg-folyamatonként: a beágyazott hívás és a munka al-taskjai átmennek;
- az ffmpeg a kapun belül HEAVY_JOB_THREADS szálon kódol (az x264 a fájlba is beírja) — a fájleszközöknél; a videó
  munkamásolata (heavy(threads=False)) a saját szeletenkénti -threads-ét tartja, beágyazva is;
- a munkamásolat egy nehéz munka: foglalt helynél a job „queued”, és megmondja, indulva mennyi idő; a hátralévő idő a
  várakozás alatt nem fogy; a meglévő másolat nem kér helyet;
- az előnézet és a háttérben futó végleges render sosem kér helyet: minden hely foglalt, a szerkesztés előnézete mégis
  visszajön, és a végleges MP4 is elkészül;
- a régi /upload lemezre ír, a korlát fölött 413 ugyanazzal a szöveggel, mint a darabolt feltöltés — a megadott
  hosszon túli törzset el sem olvassa, hossz nélkül a korlátnál abbahagyja.
"""
from __future__ import annotations

import asyncio
import contextlib
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


@contextlib.asynccontextmanager
async def _every_place_taken():
    """Valaki más nehéz munkája fogja a helyet (HEAVY_JOB_SLOTS=1 mellett mindet), amíg a blokk tart."""
    from app import jobs
    hold, go = asyncio.Event(), asyncio.Event()

    async def blocker():
        async with jobs.heavy():
            hold.set()
            await go.wait()
    task = asyncio.create_task(blocker())
    await hold.wait()
    try:
        yield
    finally:
        go.set()
        await task


async def _until_queued(jid: str) -> dict:
    from app import jobs
    for _ in range(250):
        st = await jobs.job_status(jid)
        if st["status"] == "queued":
            return st
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {jid} did not queue: {st}")


async def test_video_work_keeps_its_own_threads_even_when_nested(monkeypatch):
    from app import jobs
    from app.convert import engines
    monkeypatch.setenv("HEAVY_JOB_THREADS", "3")
    args = ("ffmpeg", "-i", "a.mp4", "-c:v", "libx264", "-threads", "4", "piece.mp4")
    async with jobs.heavy(threads=False):                       # a munkamásolat
        assert engines._threads(args) == args
        async with jobs.heavy():                                # beágyazva: a külső dönt
            assert engines._threads(args) == args
    async with jobs.heavy():                                    # fájleszköz…
        assert "-filter_threads" in engines._threads(args)
        async with jobs.heavy(threads=False):                   # …benne a munkamásolat: kifejezetten kivétel
            assert engines._threads(args) == args
        assert "-filter_threads" in engines._threads(args)
    assert engines._threads(args) == args
    assert jobs._heavy_running == 0 and not jobs._heavy_queue


async def test_a_queued_job_keeps_its_time_for_when_it_runs(monkeypatch):
    """A sorban álló job megmondja, indulva mennyi idő; a hátralévő idő a várakozás alatt nem fogy, indulva onnan
    számol, és a szakasza visszaáll."""
    from app import jobs
    monkeypatch.setenv("HEAVY_JOB_SLOTS", "1")
    ran, finish = asyncio.Event(), asyncio.Event()

    async def work(progress):
        progress(0, 4, "making a small working copy for the editor")
        progress.eta(600)
        async with jobs.heavy(threads=False):
            ran.set()
            await finish.wait()
        return {"ok": True}
    async with _every_place_taken():
        j = jobs.start("video", work)
        st = await _until_queued(j["id"])
        assert st["position"] == 1 and st["stage"] == "queued" and "remaining_s" not in st
        assert st["message"] == ("Waiting for a free slot: the server runs 1 video and audio jobs at a time, and yours is "
                                 "next. It starts by itself, then takes about 10 min.")
        job = jobs._JOBS[j["id"]]
        left = jobs.remaining_s(job)
        await asyncio.sleep(0.4)
        assert jobs.remaining_s(job) == pytest.approx(left, abs=0.01) and left == pytest.approx(600, abs=1)
    await asyncio.wait_for(ran.wait(), 5)
    st = await jobs.job_status(j["id"])
    assert st["status"] == "running" and st["stage"] == "making a small working copy for the editor"
    assert st["remaining_s"] >= 599 and "message" not in st and "position" not in st   # a 0,4 s várakozás nem fogyott
    finish.set()
    assert (await jobs.wait_status(j["id"], 5))["status"] == "done"
    assert jobs._heavy_running == 0 and not jobs._heavy_queue


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg needed")
async def test_the_working_copy_waits_for_a_place_then_runs_its_slices_in_it(tmp_path, monkeypatch, put_file):
    """A munkamásolat (footage.make_proxy) egy nehéz munka: foglalt helynél vár; utána a szeletei egy helyen futnak, a
    szeletenkénti -threads-üket (magok // szeletek) a kapu nem írja felül (a sapka 3 volna). A meglévő másolat — a
    szerkesztés előnézete ezt kapja — nem kér helyet, akkor sem, ha minden hely foglalt."""
    import os
    import subprocess
    from pathlib import Path
    from app import jobs
    from app.convert import engines
    from app.storage import output_path
    from app.tools import fast_render, footage
    monkeypatch.setenv("HEAVY_JOB_SLOTS", "1")
    monkeypatch.setenv("HEAVY_JOB_THREADS", "3")
    clip = tmp_path / "phone.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=10", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=48000", "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(clip)], check=True)
    fid = put_file(clip)
    base = {"file_id": fid, "width": 1280, "height": 720}
    monkeypatch.setattr(fast_render, "chunks", lambda dur, fps, *a, **k: [(0.0, dur / 2), (dur / 2, dur)])
    seen, real = [], engines.run

    async def spy(*a, **k):
        if Path(a[0]).name == "ffmpeg":
            seen.append((engines._threads(a), jobs._heavy_running))
        return await real(*a, **k)
    monkeypatch.setattr(engines, "run", spy)

    async def work(progress):
        progress(0, 4, "making a small working copy for the editor")
        b, _ = await footage.make_proxy(base, clip)
        return {"b": b, "waited": jobs.waited()}
    async with _every_place_taken():
        j = jobs.start("video", work)
        assert (await _until_queued(j["id"]))["position"] == 1
        await asyncio.sleep(0.3)
        assert not seen                                         # a helyére vár: még egy ffmpeg sem indult
    st = await jobs.wait_status(j["id"], 30)
    assert st["status"] == "done", st
    b = st["result"]["b"]
    assert output_path(b["media_id"]).exists() and b["proxy"] == {"w": 960, "h": 540}
    assert st["result"]["waited"] >= 0.3                        # a sorban töltött idő (a kalibráció levonja)
    slices = [a for a, _ in seen if "libx264" in a]
    want = str(max(2, (os.cpu_count() or 4) // 2))
    assert len(slices) == 2 and all(a.count("-threads") == 1 and a[a.index("-threads") + 1] == want for a in slices)
    assert not any("-filter_threads" in a for a, _ in seen)
    assert all(n == 1 for _, n in seen)                         # egy hely: a szeletek és az összefűzés benne
    assert jobs._heavy_running == 0
    seen.clear()
    async with _every_place_taken():                            # minden hely foglalt: a meglévő másolat azonnal jön
        b2, _ = await asyncio.wait_for(footage.make_proxy(base, clip), 5)
        assert b2["media_id"] == b["media_id"] and not seen and not jobs._heavy_queue


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_previews_and_the_final_render_never_wait_for_a_place(tmp_path, put_file, monkeypatch):
    """Minden hely foglalt (valaki más videója kódol): a szerkesztés előnézete mégis visszajön (a munkamásolat a
    gyorsítótárból, az előnézet a saját kapuján), és a háttérben futó végleges render is elkészül — egyik sem kér helyet."""
    import json
    import subprocess
    from app import jobs
    from app.storage import output_path, read_meta
    from app.tools import video, video_preview
    monkeypatch.setenv("HEAVY_JOB_SLOTS", "1")
    monkeypatch.setenv("VIDEO_PREVIEW_HLS", "0")                 # a PR #40-es MP4-előnézet alakja (a HLS: test_preview_wire)
    for mod, name in ((video, "_gate"), (video, "_bgate"), (video_preview, "_gate")):
        monkeypatch.setattr(mod, name, None)        # a kapuk ennek a tesztnek a loopjában jönnek létre (asyncio.run)
    clip = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=10", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=48000", "-t", "8", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(clip)], check=True)
    fid = put_file(clip)

    async def until_done(jid: str) -> dict:
        for _ in range(16):                                     # legfeljebb 4 perc (egy helyre váró render sosem végezne)
            st = await jobs.wait_status(jid, 15)
            if st["status"] not in ("running", "queued"):
                break
        return st

    async def main():
        res = await video.overlay({"file_id": fid, "brand": "none", "final": "background",
                                   "layers": [{"at": 1, "dur": 1.5, "type": "source", "text": "Forrás: teszt"}]})
        assert (read_meta(fid) or {}).get("proxies")            # a munkamásolat elkészült (szabad helyen)
        async with _every_place_taken():
            t0 = time.time()
            ed = await video.edit(res["file_id"], {"layers": [{"id": "L1", "text": "Forrás: második"}]}, final="background")
            assert time.time() - t0 < 120 and ed["preview"]["parts"][0]["what"].startswith("L1")
            for jid in (res["final"]["job_id"], ed["final"]["job_id"]):
                st = await until_done(jid)
                assert st["status"] == "done", st
                out = output_path(st["result"]["file_id"])
                probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", str(out)],
                                                  capture_output=True, text=True, check=True).stdout)
                assert float(probe["format"]["duration"]) == pytest.approx(8.0, abs=0.3)
            assert jobs._heavy_running == 1 and not jobs._heavy_queue   # csak a másiké: senki nem kért, nem várt helyet
        assert jobs._heavy_running == 0
    asyncio.run(main())


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
