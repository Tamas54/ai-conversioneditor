"""OMLETT 2.5 „Minőségjavítás” (Topaz via kie.ai), hálózat nélkül: egy hamis szolgáltató 2×-esre skáláz (ffmpeg) és
eldobja a hangot, mint egy valódi felskálázó tenné; az eredmény a fájl ÚJ változata, az eredeti hanggal; az 50 MB-nál
nagyobb bemenet darabokban megy és visszaáll egy videóvá; a levonás a tényleges költség; MCP-ből is (becslés + job)."""
from __future__ import annotations

import asyncio
import json
import secrets
import subprocess
from pathlib import Path

import pytest

HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class UpscaleProv:
    """kie helyett: upload → „URL” a helyi útra; a feladat kész, amikor a teszt azt mondja; az eredmény 2×-es, néma."""
    name = "kie"

    def __init__(self):
        from app.providers.base import RUNNING, TaskState
        self.tasks, self.inputs, self._ts, self._run = {}, {}, TaskState, RUNNING

    def same_key(self):
        return True

    async def upload(self, path):
        import tempfile
        keep = Path(tempfile.mkdtemp()) / f"up_{secrets.token_hex(4)}{Path(path).suffix}"
        keep.write_bytes(Path(path).read_bytes())          # a darabok mappája a feltöltés után törlődik
        url = f"fake://{keep}"
        return url

    async def create(self, model, input, callback_url=None):
        assert model == "topaz/video-upscale" and input["factor"] in (1, 2, 4)
        tid = f"tz_{secrets.token_hex(4)}"
        self.tasks[tid], self.inputs[tid] = self._ts(task_id=tid, state=self._run), (input["video"][7:], input["factor"])
        return tid

    async def status(self, tid):
        return self.tasks[tid]

    def finish_all(self, ok=True):
        from app.providers.base import FAIL, SUCCESS
        for tid, st in self.tasks.items():
            if st.state not in (SUCCESS, FAIL):
                self.tasks[tid] = self._ts(task_id=tid, state=SUCCESS if ok else FAIL, cost=24.0 if ok else 0.0,
                                           urls=[f"fake://{tid}"] if ok else [], error=None if ok else "internal error")

    async def fetch_result(self, st):
        from app.storage import new_file_id, output_path
        src, f = self.inputs[st.task_id]
        fid = new_file_id("mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vf", f"scale=iw*{f}:ih*{f}", "-an", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", str(output_path(fid))], check=True)
        return [fid]

    async def cancel(self, tid):
        return False


@pytest.fixture()
def prov(db_client, monkeypatch):
    from app import credits, providers
    from app.tools import ai_video
    p = UpscaleProv()
    monkeypatch.setattr(providers, "get", lambda name="kie": p)
    monkeypatch.setattr(providers, "enabled_for", lambda uid=None: True)
    monkeypatch.setattr(ai_video, "POLL_FIRST_S", 0.05)
    db_client.portal.call(credits.seed_rules)
    return p


def _clip(tmp_path, secs=3, size="320x240", audio=True, name="c.mp4", gop=250):
    out = tmp_path / name
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration={secs}"]
                   + (["-f", "lavfi", "-i", f"sine=frequency=440:duration={secs}", "-c:a", "aac", "-shortest"] if audio else [])
                   + ["-c:v", "libx264", "-g", str(gop), "-pix_fmt", "yuv420p", str(out)], check=True)
    return out


def _probe(path):
    d = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                                  capture_output=True, text=True).stdout)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    return {"w": v["width"], "h": v["height"], "audio": any(s["codec_type"] == "audio" for s in d["streams"]),
            "dur": float(d["format"]["duration"])}


def _user(call, n):
    """n FIZETETT kredit (az élesítés csak abból foglalhat: credits.PAID_ONLY_OPS)."""
    from app import db
    from paid import add_paid
    u = call(db.user_create, f"{secrets.token_hex(6)}@example.com", "jelszó123456", None, 0)
    add_paid(call, u["id"], n)
    return u["id"], call(db.api_key_create, u["id"], "mcp")["key"]


def _put_as(uid, src):
    from app import tenancy
    from app.storage import input_path, new_file_id, write_meta
    tok = tenancy.set_user(uid)
    try:
        fid = new_file_id("mp4")
        input_path(fid).write_bytes(Path(src).read_bytes())
        write_meta(fid, original_filename="konyha.mp4")
        return fid
    finally:
        tenancy.reset(tok)


def _run_job(call, prov, uid, coro_fn, ok=True):
    """Indít (a hívó felhasználójaként), megvárja, hogy a szolgáltatóhoz érjen, befejezteti, és megvárja a jobot."""
    from app import jobs, tenancy

    async def go():
        tok = tenancy.set_user(uid)
        try:
            job = await coro_fn()
        finally:
            tenancy.reset(tok)
        for _ in range(400):
            if any(t.state == "running" for t in prov.tasks.values()):
                prov.finish_all(ok)
            j = jobs._JOBS[job["id"]]
            if j["status"] != "running":
                return job, j
            await asyncio.sleep(0.05)
        raise AssertionError("job did not finish")
    return call(go)


def test_enhance_makes_a_new_version_with_the_original_sound(db_client, prov, tmp_path):
    from app import db, tenancy
    from app.storage import output_path, read_meta
    from app.tools import ai_video
    call = db_client.portal.call
    uid, _ = _user(call, 20)
    fid = _put_as(uid, _clip(tmp_path))
    job, j = _run_job(call, prov, uid, lambda: ai_video.start_enhance(fid, 2))
    assert job["estimate"]["credits"] == 8                       # 3 s × 8 kie-kredit × padló = 7,6 → 8 (foglalva)
    assert j["status"] == "done", j.get("error")
    r = j["result"]
    assert r["credits_charged"] == 8 and r["made_from"] == fid and r["label"] == "konyha (enhanced 2x).mp4"
    tok = tenancy.set_user(uid)
    try:
        info = _probe(output_path(r["file_id"]))
        assert (info["w"], info["h"]) == (640, 480) and info["audio"]          # 2×, és visszakapta a hangot
        assert read_meta(r["file_id"])["source_file_id"] == fid               # a forrás új változata (verziófa)
    finally:
        tenancy.reset(tok)
    assert call(db.user_get, uid)["credits"] == 12


def test_big_input_goes_in_parts_and_comes_back_whole(db_client, prov, tmp_path, monkeypatch):
    from app import tenancy
    from app.storage import output_path
    from app.tools import ai_video
    monkeypatch.setattr(ai_video, "TOPAZ_MAX_MB", 0.06)             # 50 MB helyett ~60 kB: a 8 s-os klip darabokban megy
    call = db_client.portal.call
    uid, _ = _user(call, 40)
    src = _clip(tmp_path, secs=8, size="640x360", gop=25)            # telefonos felvétel: másodpercenként kulcskocka
    assert src.stat().st_size > 0.06 * 1024 * 1024
    fid = _put_as(uid, src)
    job, j = _run_job(call, prov, uid, lambda: ai_video.start_enhance(fid, 1))
    assert j["status"] == "done", j.get("error")
    assert len(prov.tasks) >= 2                                     # több szolgáltatói feladat, egy felhasználói művelet
    tok = tenancy.set_user(uid)
    try:
        info = _probe(output_path(j["result"]["file_id"]))
    finally:
        tenancy.reset(tok)
    assert abs(info["dur"] - 8) < 0.6 and (info["w"], info["h"]) == (640, 360) and info["audio"]
    assert 0 < j["result"]["credits_charged"] <= job["estimate"]["credits"]           # sosem több a foglaltnál


def test_failed_enhance_costs_nothing(db_client, prov, tmp_path):
    from app import db
    from app.tools import ai_video
    call = db_client.portal.call
    uid, _ = _user(call, 20)
    fid = _put_as(uid, _clip(tmp_path))
    _, j = _run_job(call, prov, uid, lambda: ai_video.start_enhance(fid, 4), ok=False)
    assert j["status"] == "failed" and "came back" in j["error"]
    assert call(db.user_get, uid)["credits"] == 20


def test_enhance_over_mcp_estimate_and_insufficient(db_client, prov, tmp_path):
    from app import db
    call = db_client.portal.call

    def mcp(key, args):
        hs = {**HDR, "Authorization": f"Bearer {key}"}
        db_client.post("/mcp/", headers=hs, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        res = db_client.post("/mcp/", headers=hs, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                        "params": {"name": "video", "arguments": args}}).json()["result"]
        t = res["content"][0]["text"]
        return res.get("isError"), (json.loads(t) if t.startswith("{") else t)
    uid, key = _user(call, 10)
    fid = _put_as(uid, _clip(tmp_path))
    err, est = mcp(key, {"action": "estimate", "file_id": fid, "factor": 4})
    assert not err and est["credits"] == 14 and est["balance"] == 10            # 3 s × 14 × padló = 13,4 → 14
    err, text = mcp(key, {"action": "enhance", "file_id": fid, "factor": 4})
    assert err and "insufficient_credits" in text and f"needs {est['credits']}" in text and "have 10" in text
    assert prov.tasks == {} and call(db.user_get, uid)["credits"] == 10         # semmi nem indult
    err, job = mcp(key, {"action": "enhance", "file_id": fid, "factor": 2})
    assert not err and job["status"] == "running" and job["estimate"]["credits"] == 8

    async def finish():
        from app import jobs
        for _ in range(400):
            if any(t.state == "running" for t in prov.tasks.values()):
                prov.finish_all()
            if jobs._JOBS[job["job_id"]]["status"] != "running":
                return jobs._JOBS[job["job_id"]]
            await asyncio.sleep(0.05)
    assert call(finish)["status"] == "done"
    err, st = mcp(key, {"action": "status", "job_id": job["job_id"]})
    assert not err and st["result"]["label"].endswith("(enhanced 2x).mp4")
