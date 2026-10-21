"""A kredit + kie független felülvizsgálatának (OMLETT) regressziós tesztjei — az AI-feladatok életciklusa, hamis szolgáltatóval (hálózat nélkül):

 1/5  EGY megszakítási szabály: el nem indult op → a hold visszajár; a kie-nél lévő → a hold marad, a végén a tényleges
      költség (ingyen nem lehet kie-pénzt égetni start+cancel ismétlésével); a később el nem induló rész és a lejárt
      várakozás sem enged el korán.
 2    megosztott fájl felskálázása: a webhook / újraindítás is a tulajdonos munkaterületén fejezi be.
 4    a job megszakítása előkészítés közben: a hold visszajár, az op nem ragad a „figyelt” halmazban.
 6    hamis hossz a fejlécben: a valódi hosszat fizeti, és csak a kifizetett másodperceket töltjük fel.
 7    ár nélkül (törölt / 0 szabály) nem indul fizetős feladat.
 11   az utófeldolgozás egyszer fut (az eredmény elszámolás ELŐTT a meta-ban).
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import struct
import subprocess

import pytest

from paid import paid_user
from test_ai_enhance import UpscaleProv, _clip, _put_as, _user  # noqa: F401


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


def _as(uid, fn):
    from app import tenancy

    async def run():
        tok = tenancy.set_user(uid)
        try:
            return await fn()
        finally:
            tenancy.reset(tok)
    return run


async def _until(cond, n=400):
    for _ in range(n):
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


# ---------- 1 / 5: a megszakítás szabálya ----------

def test_cancel_after_launch_keeps_the_hold_and_charges_the_real_cost(db_client, prov, tmp_path):
    from app import db, tenancy
    from app.storage import output_path
    from app.tools import ai_video
    call = db_client.portal.call
    uid, _ = _user(call, 20)
    fid = _put_as(uid, _clip(tmp_path))

    async def go():
        job = await _as(uid, lambda: ai_video.start_enhance(fid, 2))()
        assert await _until(lambda: prov.tasks)
        st = await _as(uid, lambda: ai_video.cancel(job["task_id"]))()
        return job, st
    job, st = call(go)
    assert st["status"] == "cancelling"                                  # a kie-feladat nem állítható le
    held = call(db.user_get, uid)["credits"]
    assert held < 20                                                     # a foglalás marad
    prov.finish_all(True)                                                # a kie befejezi és 24 kie-kreditet számláz
    call(ai_video.resume_once)                                           # egy folytatási kör (újraindítás / resume_loop)
    st = call(_as(uid, lambda: ai_video.get(job["task_id"])))
    assert st["status"] == "cancelled" and st["credits_charged"] == 8    # ceil(24 × padló) — a tényleges költség
    assert call(db.user_get, uid)["credits"] == 12
    tok = tenancy.set_user(uid)
    try:
        assert output_path(st["result"]["file_id"]).exists()             # kifizette: a fájljai közé kerül
    finally:
        tenancy.reset(tok)


def test_a_part_that_fails_to_start_does_not_release_the_running_parts(db_client, prov, monkeypatch):
    """Két rész: az első elindult, a második létrehozása elhasal → a foglalás marad, amíg az első fut; a végén a
    felhasználónak 0, de a szolgáltató költsége a könyvben."""
    from app import db
    from app.providers import ProviderError
    from app.tools import ai_video
    call = db_client.portal.call
    uid, _ = _user(call, 30)
    real = prov.create
    calls = []

    async def create(model, input, callback_url=None):
        calls.append(1)
        if len(calls) == 2:
            raise ProviderError("kie error 500: busy", 500)
        return await real(model, input, callback_url)
    monkeypatch.setattr(prov, "create", create)
    op = call(_as(uid, lambda: ai_video.reserve("enhance", "topaz/video-upscale", hold=10, meta={"units": 8, "tier": "2x"})))
    with pytest.raises(ai_video.AIVideoError):
        call(lambda: ai_video.launch(op, [{"video": "fake:///a.mp4", "factor": 2}, {"video": "fake:///b.mp4", "factor": 2}]))
    rows = call(lambda: ai_video._rows_of(op))
    assert not any(r["settled"] for r in rows) and call(db.user_get, uid)["credits"] == 20    # NEM engedtük el korán
    prov.finish_all(True)
    call(lambda: ai_video.poll(rows))
    rows = call(lambda: ai_video._rows_of(op))
    assert all(r["settled"] for r in rows) and call(db.user_get, uid)["credits"] == 30          # a felhasználónak 0
    rel = call(db._rows, "SELECT provider_cost FROM credit_ledger WHERE idempotency_key = %s", (f"release:{op}",))
    assert float(rel[0]["provider_cost"]) == 24                                                 # a kie költsége könyvelve


def test_wait_timeout_does_not_release_a_running_task(db_client, prov):
    from app import db
    from app.tools import ai_video
    call = db_client.portal.call
    uid, _ = _user(call, 30)
    op = call(_as(uid, lambda: ai_video.submit("enhance", "topaz/video-upscale", [{"video": "fake:///a.mp4", "factor": 2}],
                                               hold=10, meta={"units": 8, "tier": "2x"})))
    with pytest.raises(ai_video.AIVideoError, match="still working"):
        call(lambda: ai_video.wait(op, timeout=0.01))
    assert not call(lambda: ai_video._rows_of(op))[0]["settled"] and call(db.user_get, uid)["credits"] == 20
    prov.finish_all(False)                                               # a kie végül elbukik: a felhasználónak 0
    rows = call(lambda: ai_video._rows_of(op))
    call(lambda: ai_video.poll(rows))
    assert call(lambda: ai_video._rows_of(op))[0]["settled"] and call(db.user_get, uid)["credits"] == 30


def test_job_cancel_while_waiting_follows_the_same_rule(db_client, prov, tmp_path):
    from app import db, jobs
    from app.tools import ai_video
    call = db_client.portal.call
    uid, _ = _user(call, 20)
    fid = _put_as(uid, _clip(tmp_path))

    async def go():
        job = await _as(uid, lambda: ai_video.start_enhance(fid, 2))()
        assert await _until(lambda: prov.tasks)
        await asyncio.sleep(0.2)
        await _as(uid, lambda: jobs.job_cancel(job["job_id"]))()
        await asyncio.sleep(0.3)
        return job, job["task_id"] in ai_video._watching, jobs._JOBS[job["job_id"]]["status"]
    job, watching, jst = call(go)
    assert jst == "cancelled" and not watching
    st = call(_as(uid, lambda: ai_video.get(job["task_id"])))
    assert st["status"] == "cancelling" and call(db.user_get, uid)["credits"] < 20
    prov.finish_all(True)
    call(ai_video.resume_once)
    st = call(_as(uid, lambda: ai_video.get(job["task_id"])))
    assert st["status"] == "cancelled" and st["credits_charged"] == 8 and call(db.user_get, uid)["credits"] == 12


# ---------- 4: a job megszakítása előkészítés közben ----------

def test_job_cancel_during_prepare_releases_the_hold_and_unwatches(db_client, prov, tmp_path, monkeypatch):
    from app import db, jobs
    from app.tools import ai_video
    call = db_client.portal.call
    uid, _ = _user(call, 20)
    fid = _put_as(uid, _clip(tmp_path))

    async def slow_upload(path):
        await asyncio.sleep(30)
        return "fake://x"
    monkeypatch.setattr(prov, "upload", slow_upload)

    async def go():
        job = await _as(uid, lambda: ai_video.start_enhance(fid, 2))()
        await asyncio.sleep(1.0)
        await _as(uid, lambda: jobs.job_cancel(job["job_id"]))()
        await asyncio.sleep(0.5)
        return job, job["task_id"] in ai_video._watching, jobs._JOBS[job["job_id"]]["status"]
    job, watching, jst = call(go)
    rows = call(lambda: ai_video._rows_of(job["task_id"]))
    assert jst == "cancelled" and not watching and prov.tasks == {}
    assert rows[0]["settled"] and rows[0]["status"] == "cancelled" and call(db.user_get, uid)["credits"] == 20


# ---------- 2: megosztott fájl, a webhook fejezi be ----------

def test_shared_enhance_finished_by_the_webhook_lands_in_the_owners_workspace(db_client, prov, tmp_path, monkeypatch):
    from app import db, sharing, tenancy
    from app.auth import COOKIE, new_session
    from app.tools import ai_video
    monkeypatch.setenv("SHARING_ENABLED", "1")
    monkeypatch.setattr(ai_video, "POLL_FIRST_S", 60.0)                # a job saját lekérdezése ne előzze meg
    call = db_client.portal.call
    a = call(db.user_create, f"a{os.urandom(3).hex()}@example.com", "jelszó12345", "A", 5)["id"]
    b = paid_user(call, 20, "B")
    fid = _put_as(a, _clip(tmp_path))
    call(lambda: sharing.share(a, fid, user_id=b, role="editor"))
    db_client.cookies.clear()
    r = db_client.post("/api/ai/enhance", json={"file_id": fid, "factor": 2}, headers={"Cookie": f"{COOKIE}={new_session(b)}"})
    assert r.status_code == 200, r.text
    op = r.json()["task_id"]

    async def go():
        assert await _until(lambda: prov.tasks)
        await asyncio.sleep(0.3)
        prov.finish_all(True)
        return await ai_video.on_webhook(next(iter(prov.tasks)))      # ahogy a nyilvános /webhooks/kie hívná
    assert call(go) == "settled"
    rows = call(lambda: ai_video._rows_of(op))
    out = rows[0]["meta"].get("output") or {}
    assert rows[0]["settled"] and rows[0]["status"] == "success" and out.get("made_from") == fid, rows[0]["error"]
    assert call(db.user_get, b)["credits"] == 12 and call(db.user_get, a)["credits"] == 5     # B fizet, nem A
    assert (tenancy.user_root(a) / "outputs" / out["file_id"]).exists()                         # A munkaterületén


def test_shared_enhance_after_the_share_was_revoked_never_writes_into_the_owners_workspace(db_client, prov, tmp_path,
                                                                                           monkeypatch):
    """Integráció (share × kie): a befejezéskor a jogot újra lekérdezzük — visszavont megosztás után a webhook nem ír A
    munkaterületére, és B a lefoglalt kreditjét visszakapja."""
    from app import db, sharing, tenancy
    from app.auth import COOKIE, new_session
    from app.tools import ai_video
    monkeypatch.setenv("SHARING_ENABLED", "1")
    monkeypatch.setattr(ai_video, "POLL_FIRST_S", 60.0)
    call = db_client.portal.call
    a = call(db.user_create, f"a{os.urandom(3).hex()}@example.com", "jelszó12345", "A", 5)["id"]
    b = paid_user(call, 20, "B")
    fid = _put_as(a, _clip(tmp_path))
    call(lambda: sharing.share(a, fid, user_id=b, role="editor"))
    db_client.cookies.clear()
    r = db_client.post("/api/ai/enhance", json={"file_id": fid, "factor": 2}, headers={"Cookie": f"{COOKIE}={new_session(b)}"})
    assert r.status_code == 200, r.text
    op = r.json()["task_id"]
    before = set(p.name for p in (tenancy.user_root(a) / "outputs").glob("*"))

    async def go():
        assert await _until(lambda: prov.tasks)
        await asyncio.sleep(0.3)
        await sharing.share(a, fid, user_id=b, role="none")                       # A visszavonja
        prov.finish_all(True)
        return await ai_video.on_webhook(next(iter(prov.tasks)))
    call(go)
    rows = call(lambda: ai_video._rows_of(op))
    assert rows[0]["settled"] and not (rows[0]["meta"].get("output") or {}).get("file_id")
    assert call(db.user_get, b)["credits"] == 20                                    # visszakapta
    after = set(p.name for p in (tenancy.user_root(a) / "outputs").glob("*"))
    assert not {n for n in after - before if not n.endswith(".json")}                # A-nál nem keletkezett fájl


# ---------- 6: hamis hossz a fejlécben ----------

def _spoofed(tmp_path, secs=20):
    src = _clip(tmp_path, secs=secs, audio=False, name="long.mp4")
    data = bytearray(src.read_bytes())
    i = data.find(b"mvhd")
    ts = struct.unpack(">I", data[i + 16:i + 20])[0]
    data[i + 20:i + 24] = struct.pack(">I", ts * 1)                   # a movie header szerint 1 s
    out = tmp_path / "spoof.mp4"
    out.write_bytes(bytes(data))
    return out


def test_a_spoofed_header_pays_for_the_real_length_and_uploads_only_that(db_client, prov, tmp_path):
    from app import db
    from app.tools import ai_video, crafts, video
    call = db_client.portal.call
    uid, _ = _user(call, 80)
    src = _spoofed(tmp_path)
    fid = _put_as(uid, src)
    assert call(_as(uid, lambda: video.probe_movie(crafts.existing(fid))))["duration"] < 2      # a fejléc hazudik
    est = call(_as(uid, lambda: ai_video.estimate_enhance(fid, 2)))
    assert est["seconds"] >= 20 and est["credits"] == 51                                        # 20 s × 8 × padló
    with pytest.raises(ai_video.AIVideoError, match=r"is 2\d\.\d s"):        # referenciának is a valódi hossz számít
        call(_as(uid, lambda: ai_video._ref_seconds([fid], "video")))

    async def go():
        job = await _as(uid, lambda: ai_video.start_enhance(fid, 2))()
        assert await _until(lambda: prov.tasks)
        return job
    call(go)
    up = next(iter(prov.inputs.values()))[0]
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0", "-print_format",
                                      "json", "-show_entries", "stream=nb_read_packets", up], capture_output=True, text=True).stdout)
    assert int(info["streams"][0]["nb_read_packets"]) <= 20 * 25 + 1                            # csak a kifizetett
    prov.finish_all(True)

    rows = call(db._rows, "SELECT meta->>'op' AS op FROM provider_tasks WHERE provider_task_id = %s", (next(iter(prov.tasks)),))

    async def done():
        for _ in range(400):
            if all(r["settled"] for r in await ai_video._rows_of(rows[0]["op"])):
                return True
            await asyncio.sleep(0.05)
    assert call(done)
    assert call(db.user_get, uid)["credits"] == 80 - 8                  # a tényleges kie-költség (24 × padló), a foglalt 51-en belül


def test_the_upload_is_cut_to_the_paid_seconds(tmp_path):
    from app.tools import ai_video
    src = _clip(tmp_path, secs=6, audio=False)
    parts = asyncio.run(ai_video._topaz_inputs(src, {"duration": 6}, tmp_path, 2.0))
    d = float(json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", str(parts[0])],
                                        capture_output=True, text=True).stdout)["format"]["duration"])
    assert d <= 2.2


# ---------- 7: ár nélkül nincs ingyen kie ----------

def test_no_price_no_paid_job(db_client, prov):
    from app import db
    from app.tools import ai_video
    call = db_client.portal.call
    uid, _ = _user(call, 30)
    with pytest.raises(ai_video.AIVideoError, match="no price"):
        call(_as(uid, lambda: ai_video.reserve("enhance", "topaz/video-upscale", hold=0, meta={})))
    assert call(db.user_get, uid)["credits"] == 30


# ---------- 11: az utófeldolgozás egyszer fut ----------

def test_post_processing_runs_once_even_if_settling_crashes(db_client, prov, monkeypatch, tmp_path):
    from app import credits, db
    from app.tools import ai_video
    call = db_client.portal.call
    uid, _ = _user(call, 40)
    ran = []

    async def post(rows, files):
        ran.append(1)
        return {"file_id": files[0][0], "label": "x"}
    monkeypatch.setitem(ai_video.POST, "enhance", post)
    src = _clip(tmp_path)
    op = call(_as(uid, lambda: ai_video.submit("enhance", "topaz/video-upscale", [{"video": f"fake://{src}", "factor": 2}],
                                               hold=10, meta={"units": 8, "tier": "2x"})))
    real, crashed = credits.settle, []

    async def settle_once(*a, **k):
        if not crashed:
            crashed.append(1)
            raise RuntimeError("crash between post and settle")
        return await real(*a, **k)
    monkeypatch.setattr(credits, "settle", settle_once)
    prov.finish_all(True)
    rows = call(lambda: ai_video._rows_of(op))
    with pytest.raises(RuntimeError):
        call(lambda: ai_video.poll(rows))
    call(ai_video.resume_once)
    rows = call(lambda: ai_video._rows_of(op))
    assert ran == [1] and all(r["settled"] for r in rows) and call(db.user_get, uid)["credits"] == 32
