"""AI-klip (Wan 3.0 via kie.ai, motorválasztással; 10-21 óta — előtte Seedance), hálózat nélkül: szöveg→videó az alap
motoron és 720p-n, a gyors motor, kép→videó a timeline kockájából (a kie követelményei szerint előkészített JPEG), a klip
a timeline-ra (at → teljes képes bevágás, after_scene → saját jelenet), MCP-ből is; kevés (vagy csak ingyenes) kreditnél
semmi nem indul. Árak: a közös képlet (5 s 720p = 26, 4 s 480p = 11 kredit)."""
from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class ClipProv:
    name = "kie"

    def __init__(self):
        from app.providers.base import RUNNING, TaskState
        self.tasks, self.created, self.uploads, self._ts, self._run = {}, [], [], TaskState, RUNNING

    def same_key(self):
        return True

    async def upload(self, path):
        import tempfile
        keep = Path(tempfile.mkdtemp()) / Path(path).name
        shutil.copy(path, keep)
        self.uploads.append(keep)
        return f"https://upload.example/{keep.name}"

    async def create(self, model, input, callback_url=None):
        from app.providers.kie import map_input
        map_input(model, input)                       # a mezőnév-tábla átengedi
        tid = f"wan_{secrets.token_hex(4)}"
        self.created.append((model, dict(input), tid))
        self.tasks[tid] = self._ts(task_id=tid, state=self._run)
        return tid

    async def status(self, tid):
        return self.tasks[tid]

    def finish_all(self, ok=True):
        from app.providers.base import FAIL, SUCCESS
        for model, inp, tid in self.created:
            if self.tasks[tid].state not in (SUCCESS, FAIL):
                secs = int(inp.get("duration") or 5)
                per_s = ({"480p": 12.2, "720p": 25.2, "1080p": 50.4} if model.endswith("prime")
                         else {"480p": 8, "720p": 16, "1080p": 32})[inp["resolution"]]
                cost = per_s * secs
                self.tasks[tid] = self._ts(task_id=tid, state=SUCCESS if ok else FAIL, cost=cost if ok else 0.0,
                                           urls=[f"https://tempfile.example/{tid}.mp4"] if ok else [],
                                           error=None if ok else "content policy")

    async def fetch_result(self, st):
        from app.storage import new_file_id, output_path
        model, inp, _ = next(c for c in self.created if c[2] == st.task_id)
        secs = int(inp.get("duration") or 5)
        fid = new_file_id("mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=854x480:rate=24:duration={secs}"]
                       + (["-f", "lavfi", "-i", f"sine=frequency=500:duration={secs}", "-c:a", "aac", "-shortest"]
                          if inp.get("audio") else [])
                       + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(output_path(fid))], check=True)
        return [fid]

    async def cancel(self, tid):
        return False


@pytest.fixture()
def prov(db_client, monkeypatch):
    from app import credits, providers
    from app.tools import ai_video
    p = ClipProv()
    monkeypatch.setattr(providers, "get", lambda name="kie": p)
    monkeypatch.setattr(providers, "enabled_for", lambda uid=None: True)
    monkeypatch.setattr(ai_video, "POLL_FIRST_S", 0.05)
    db_client.portal.call(credits.seed_rules)
    return p


def _user(call, n, paid=True):
    from app import db
    from paid import add_paid
    u = call(db.user_create, f"{secrets.token_hex(6)}@example.com", "jelszó123456", None, 0 if paid else n)
    if paid:
        add_paid(call, u["id"], n)
    return u["id"], call(db.api_key_create, u["id"], "mcp")["key"]


def _as(uid, fn):
    from app import tenancy

    async def run():
        tok = tenancy.set_user(uid)
        try:
            return await fn()
        finally:
            tenancy.reset(tok)
    return run


def _job(call, prov, uid, fn, ok=True, limit=6000):
    from app import jobs

    async def go():                                   # a timeline-ra tétel renderel: terhelt gépen percekig is tarthat
        job = await _as(uid, fn)()
        for _ in range(limit):
            if any(t.state == "running" for t in prov.tasks.values()):
                prov.finish_all(ok)
            j = jobs._JOBS[job["id"]]
            if j["status"] != "running":
                return job, j
            await asyncio.sleep(0.05)
        raise AssertionError("job did not finish")
    return call(go)


def _put_as(uid, src, ext="mp4"):
    from app import tenancy
    from app.storage import input_path, new_file_id, write_meta
    tok = tenancy.set_user(uid)
    try:
        fid = new_file_id(ext)
        input_path(fid).write_bytes(Path(src).read_bytes())
        write_meta(fid, original_filename=f"source.{ext}")
        return fid
    finally:
        tenancy.reset(tok)


def test_timeline_changes():
    from app.tools.ai_video import timeline_changes
    assert timeline_changes("c.mp4", 5.04, at=12.5, audio=True) == {"add_layers": [
        {"type": "fullscreen", "at": 12.5, "dur": 5.04, "image": "c.mp4", "sound": "mix"}]}
    add = timeline_changes("c.mp4", 4, after_scene="s2")["add"][0]
    assert add["after"] == "s2" and add["duration"] == 4 and add["chart"]["elements"][0]["image"] == "c.mp4"


def test_text_to_video_default_engine_and_720p_then_the_fast_engine(db_client, prov):
    from app import db
    from app.providers.kie import map_input
    from app.tools import ai_video
    call = db_client.portal.call
    uid, _ = _user(call, 200)
    job, j = _job(call, prov, uid, lambda: ai_video.start_clip({"prompt": "A paper boat on a pond"}))
    assert job["estimate"]["credits"] == 26 and j["status"] == "done", j.get("error")     # 5 s × 16 × padló = 25,5 → 26
    r = j["result"]
    assert r["credits_charged"] == 26 and r["label"] == "AI clip — A paper boat on a pond.mp4"
    model, inp, _ = prov.created[0]
    assert model == "wan/3-0-video" and inp["resolution"] == "720p" and inp["duration"] == 5 and inp["nsfw_checker"] is True
    native = map_input(model, inp)
    assert native["resolution"] == "720P" and native["aspect_ratio"] == "16:9" and native["audio"] is False
    assert call(db.user_get, uid)["credits"] == 174
    # a gyors motor (Wan 3.0 Prime): 4 s 480p = 4 × 12,2 × padló = 15,5 → 16
    job2, j2 = _job(call, prov, uid, lambda: ai_video.start_clip({"prompt": "A paper boat", "engine": "wan-3-fast",
                                                                 "resolution": "480p", "duration": 4}))
    assert job2["estimate"]["credits"] == 16 and j2["status"] == "done", j2.get("error")
    assert prov.created[1][0] == "wan/3-0-video-prime" and call(db.user_get, uid)["credits"] == 158
    for bad, msg in (({"prompt": "x", "draft": True}, "Drafts were a Seedance feature"), ({"finalize": "abc"}, "Drafts"),
                     ({"prompt": "x", "engine": "seedance"}, "engine: wan-3 | wan-3-fast")):
        with pytest.raises(ai_video.AIVideoError, match=msg):
            call(_as(uid, lambda: ai_video.start_clip(bad)))


def test_image_to_video_from_the_timeline_frame(db_client, prov, tmp_path):
    from app.tools import ai_video
    call = db_client.portal.call
    uid, _ = _user(call, 100)
    src = tmp_path / "f.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=3",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    vid = _put_as(uid, src)
    _, j = _job(call, prov, uid, lambda: ai_video.start_clip(
        {"prompt": "the camera pulls back", "image": {"frame": 1.5}, "resolution": "720p", "duration": 5, "audio": True}, project=vid))
    assert j["status"] == "done", j.get("error")
    _, inp, _ = prov.created[0]
    assert inp["first_frame"].startswith("https://upload.example/") and inp["aspect"] == "adaptive" and inp["audio"] is True
    jpg = prov.uploads[0]
    d = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(jpg)],
                                  capture_output=True, text=True).stdout)["streams"][0]
    assert min(d["width"], d["height"]) >= 300 and d["codec_name"] == "mjpeg"     # a kie minimuma: 300 px
    assert j["result"]["audio"] is True and j["result"]["credits_charged"] == 26   # 5 s 720p


def test_failed_clip_and_insufficient_or_free_credits_over_mcp(db_client, prov):
    from app import db
    from app.tools import ai_video
    call = db_client.portal.call
    uid, key = _user(call, 20)
    _, j = _job(call, prov, uid, lambda: ai_video.start_clip({"prompt": "x", "resolution": "480p", "duration": 4}), ok=False)
    assert j["status"] == "failed" and call(db.user_get, uid)["credits"] == 20

    def mcp(args, k=key):
        hs = {**HDR, "Authorization": f"Bearer {k}"}
        db_client.post("/mcp/", headers=hs, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        res = db_client.post("/mcp/", headers=hs, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                        "params": {"name": "video", "arguments": args}}).json()["result"]
        t = res["content"][0]["text"]
        return res.get("isError"), (json.loads(t) if t.startswith("{") else t)
    n = len(prov.created)
    err, est = mcp({"action": "estimate", "text": "a red boat", "ai": {"resolution": "1080p", "duration": 10}})
    assert not err and est["credits"] == 102 and est["balance"] == 20 and "Wan 3.0 1080p 10 s" in est["what"]
    err, text = mcp({"action": "ai_clip", "text": "a red boat", "ai": {"resolution": "1080p", "duration": 10}})
    assert err and "insufficient_credits" in text and "needs 102" in text and "have 20" in text
    assert len(prov.created) == n                                         # semmi nem indult
    err, text = mcp({"action": "ai_clip", "ai": {"prompt": "x", "first_frame": "a.png", "reference_images": ["b.png"]}})
    assert err and "can't be mixed" in text
    # csak ingyenes (kezdő) kredit: van egyenleg, mégsem indul — érthető üzenettel
    _, free_key = _user(call, 30, paid=False)
    err, text = mcp({"action": "ai_clip", "text": "a red boat", "ai": {"resolution": "480p", "duration": 4}}, free_key)
    assert err and "paid credits only" in text and "Buy a credit pack" in text and len(prov.created) == n
    err, job = mcp({"action": "ai_clip", "text": "a red boat", "ai": {"resolution": "480p"}, "duration": 4})
    assert not err and job["status"] == "running" and job["estimate"]["credits"] == 11
    sa = job["studio_ai"]                                                # a becsült maximum már a hívásban foglalva
    assert (sa["credits_used"], sa["credits_held"], sa["balance"]) == (0, 11, 9) and job["task_id"]

    async def finish():
        from app import jobs
        for _ in range(600):
            if any(t.state == "running" for t in prov.tasks.values()):
                prov.finish_all()
            if jobs._JOBS[job["job_id"]]["status"] != "running":
                return jobs._JOBS[job["job_id"]]
            await asyncio.sleep(0.05)
    assert call(finish)["status"] == "done" and call(db.user_get, uid)["credits"] == 9


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI (EFFECTCRAFT_CLI) and ffmpeg needed")
def test_clip_lands_on_the_timeline_as_a_cut_in(db_client, prov, tmp_path):
    from app import tenancy
    from app.tools import ai_video, video
    call = db_client.portal.call
    uid, _ = _user(call, 100)
    src = tmp_path / "footage.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=48000", "-t", "8", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(src)], check=True)
    fid = _put_as(uid, src)
    ov = call(_as(uid, lambda: video.overlay({"file_id": fid, "layers": [{"at": 0.5, "dur": 2, "type": "title", "text": "Omlett"}]})))
    _, j = _job(call, prov, uid, lambda: ai_video.start_clip({"prompt": "a pan flips an omelette", "resolution": "480p", "duration": 4,
                                                              "at": 3}, project=ov["file_id"]))
    assert j["status"] == "done", j.get("error")
    tl = j["result"]["timeline"]
    assert tl["file_id"].endswith(".ecproj") and tl["file_id"] != ov["file_id"]
    tok = tenancy.set_user(uid)
    try:
        layers = video.info(tl["file_id"])["layers"]
    finally:
        tenancy.reset(tok)
    assert any(x["type"] == "fullscreen" for x in layers), layers


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI (EFFECTCRAFT_CLI) and ffmpeg needed")
def test_clip_lands_on_the_timeline_as_a_new_scene(db_client, prov):
    from app import tenancy
    from app.tools import ai_video, video
    call = db_client.portal.call
    uid, _ = _user(call, 100)
    made = call(_as(uid, lambda: video.make({"mode": "text", "format": "landscape", "pictures": "none", "narration": "none",
                                              "title": "Omlett", "duration": 10,
                                              "scenes": [{"headline": "Tojás", "caption": "Három tojás."},
                                                         {"headline": "Serpenyő", "caption": "Forró vaj."}]})))
    _, j = _job(call, prov, uid, lambda: ai_video.start_clip({"prompt": "eggs crack into a bowl", "resolution": "480p", "duration": 4,
                                                              "after_scene": "s1"}, project=made["file_id"]))
    assert j["status"] == "done", j.get("error")
    assert "timeline_error" not in j["result"], j["result"].get("timeline_error")
    tok = tenancy.set_user(uid)
    try:
        scenes = video.info(j["result"]["timeline"]["file_id"])["scenes"]
    finally:
        tenancy.reset(tok)
    assert len(scenes) == 3 and (scenes[1].get("chart") or {}).get("type") == "graphic", scenes
