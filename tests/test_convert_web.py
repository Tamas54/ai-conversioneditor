"""A weboldal konverter-widgetjének végpontjai: célok listája (bővítve), átalakítás feladatként, mentett beállítások."""
from __future__ import annotations

import time


def _poll(client, job):
    for _ in range(300):
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.05)
        job = client.get(f"/api/jobs/{job['id']}").json()
    raise AssertionError("job didn't finish")


def test_targets_all_extends_legacy_order(client):
    legacy = [t["fmt"] for t in client.get("/api/convert/targets", params={"ext": "png"}).json()["targets"]]
    full = client.get("/api/convert/targets", params={"ext": "png", "all": 1}).json()["targets"]
    fmts = [t["fmt"] for t in full]
    assert fmts[:len(legacy)] == legacy and {"avif", "heic", "ico", "svg"} <= set(fmts)
    assert all(t["category"] for t in full)


def test_convert_run_job_with_options(client, samples, put_file):
    fid = put_file(samples.png(size=(400, 300)))
    job = client.post("/api/convert/run", json={"file_id": fid, "target": "webp", "options": {"width": 100}}).json()
    job = _poll(client, job)
    assert job["status"] == "done", job
    res = job["result"]
    assert res["file_id"].endswith(".webp") and res["engines"] == ["pillow"]
    from PIL import Image
    from app.storage import output_path
    with Image.open(output_path(res["file_id"])) as im:
        assert im.width == 100


def test_convert_run_validation(client, samples, put_file):
    fid = put_file(samples.png())
    assert client.post("/api/convert/run", json={"file_id": fid}).status_code == 400
    assert client.post("/api/convert/run", json={"file_id": "nincs.png", "target": "jpg"}).status_code == 404
    r = client.post("/api/convert/run", json={"file_id": fid, "target": "jpg", "options": {"quality": 900}})
    assert r.status_code == 400 and "quality" in r.json()["detail"]
    job = _poll(client, client.post("/api/convert/run", json={"file_id": fid, "target": "mp3"}).json())
    assert job["status"] == "failed" and "Can't convert" in job["error"]


def test_guest_can_convert_but_not_ai_tools(client, monkeypatch, samples):
    from app.config import settings
    monkeypatch.setattr(settings, "DASHBOARD_PASSWORD", "pw")
    up = client.post("/api/upload", files={"file": ("kep.png", samples.png().read_bytes(), "image/png")}).json()
    job = client.post("/api/convert/run", json={"file_id": up["file_id"], "target": "jpg"}).json()
    assert _poll(client, job)["status"] == "done"
    assert client.get("/api/presets").json() == {"presets": []}


def test_presets_need_account_and_roundtrip(db_client, samples, put_file):
    from app import db
    from app.auth import COOKIE, new_session
    call = db_client.portal.call
    u = call(db.user_create, "preset@example.com", "jelszó123", "Preset Ödön", 0)
    db_client.cookies.set(COOKIE, new_session(u["id"]))
    r = db_client.post("/api/presets", json={"name": "Webes kép", "scope": "webp", "options": {"quality": 70, "width": 1200}})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert db_client.post("/api/presets", json={"name": "Rossz", "scope": "webp", "options": {"quality": 0}}).status_code == 400
    lst = db_client.get("/api/presets", params={"scope": "webp"}).json()["presets"]
    assert [p["name"] for p in lst] == ["Webes kép"] and lst[0]["options"]["quality"] == 70
    # a preset a felhasználó munkaterületén futó átalakításra érvényes (a kérés opciói felülírják)
    from app import tenancy
    tok = tenancy.set_user(u["id"])
    try:
        fid = put_file(samples.png(size=(2000, 1000)))
    finally:
        tenancy.reset(tok)
    job = db_client.post("/api/convert/run", json={"file_id": fid, "target": "webp", "preset_id": pid, "options": {"width": 300}}).json()
    job = _poll(db_client, job)
    assert job["status"] == "done", job
    assert db_client.delete(f"/api/presets/{pid}").status_code == 200
    assert db_client.get("/api/presets").json()["presets"] == []
    db_client.cookies.clear()


def test_app_converter_reaches_every_format(client, samples, put_file):
    """Az app saját „Convert” készítője: a bevált célok változatlanul elöl, a többi formátum is választható,
    és a régi végpont az új célokat az univerzális konverterrel csinálja meg."""
    full = client.get("/api/convert/targets", params={"ext": "png", "all": 1}).json()["targets"]
    assert [t["fmt"] for t in full if t["top"]] == ["jpg", "webp", "pdf", "tif", "psd"]
    assert any(t["fmt"] == "avif" and not t["top"] for t in full)
    fid = put_file(samples.png())
    r = client.post("/api/make/convert", json={"file_id": fid, "target": "avif"})
    assert r.status_code == 200, r.text
    assert r.json()["file_id"].endswith(".avif") and r.json()["engines"] == ["pillow"]
