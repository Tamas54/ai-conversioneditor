"""Nyilvános REST API (/v1): jobs/tasks, feltöltés (darabolva is), élő státusz (wait, SSE), webhook-aláírás,
sandbox, rate limit, kreditelszámolás, OpenAPI, SSRF-védelem, felhős csatlakozók (S3 SigV4 tesztvektorral)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

ADMIN_KEY = "test-admin-token"


@pytest.fixture()
def api(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "MCP_AUTH_TOKEN", ADMIN_KEY)
    client.headers["Authorization"] = f"Bearer {ADMIN_KEY}"
    yield client
    client.headers.pop("Authorization", None)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode()


def _wait(api, job_id, secs=30):
    return api.get(f"/v1/jobs/{job_id}", params={"wait": secs}).json()


def test_openapi_is_public_and_describes_jobs(client):
    r = client.get("/v1/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert "/jobs" in spec["paths"] and "/jobs/{job_id}/events" in spec["paths"] and "/upload/{task_id}/chunks/{n}" in spec["paths"]
    assert spec["servers"][0]["url"].endswith("/v1")
    assert spec["components"]["securitySchemes"]["bearer"]["scheme"] == "bearer"


def test_requires_key(client):
    r = client.post("/v1/jobs", json={"tasks": {}})
    assert r.status_code == 401


def test_job_base64_convert_export_keeps_accents(api):
    text = "# Árvíztűrő tükörfúrógép\n\nŐszi ű betűk."
    r = api.post("/v1/jobs", json={"tag": "t1", "tasks": {
        "in": {"operation": "import/base64", "file": _b64(text), "filename": "jegyzet.md"},
        "html": {"operation": "convert", "input": "in", "output_format": "html"},
        "out": {"operation": "export/url", "input": "html"}}})
    assert r.status_code == 201, r.text
    job = _wait(api, r.json()["id"])
    assert job["status"] == "finished", job
    out = next(t for t in job["tasks"] if t["name"] == "out")
    f = out["result"]["files"][0]
    assert f["filename"].endswith(".html") and f["url"]
    body = api.get(f["url"].replace("http://testserver", "")).content.decode("utf-8")
    assert "Árvíztűrő tükörfúrógép" in body and "Őszi ű" in body
    assert "file" not in next(t for t in job["tasks"] if t["name"] == "in")["payload"]     # a tartalom nem szivárog vissza


def test_upload_and_chunked_upload(api):
    r = api.post("/v1/jobs", json={"tasks": {"up": {"operation": "import/upload"},
                                             "txt": {"operation": "convert", "input": "up", "output_format": "html"}}}).json()
    up = next(t for t in r["tasks"] if t["name"] == "up")
    for _ in range(50):
        up = api.get(f"/v1/tasks/{up['id']}").json()
        if (up.get("result") or {}).get("form"):
            break
        time.sleep(0.05)
    form = up["result"]["form"]
    path = form["url"].replace("http://testserver", "")
    res = api.post(path, files={"file": ("jegyzet.txt", "első ő ű".encode(), "text/plain")})
    assert res.status_code == 201, res.text
    assert _wait(api, r["id"])["status"] == "finished"
    # darabolt feltöltés
    r = api.post("/v1/jobs", json={"tasks": {"up": {"operation": "import/upload"},
                                             "out": {"operation": "export/url", "input": "up"}}}).json()
    up = next(t for t in r["tasks"] if t["name"] == "up")
    for _ in range(50):
        up = api.get(f"/v1/tasks/{up['id']}").json()
        if (up.get("result") or {}).get("form"):
            break
        time.sleep(0.05)
    ch = up["result"]["form"]["chunks"]
    data = ("ő" * 3000).encode()
    for i, part in enumerate((data[:2500], data[2500:])):
        assert api.put(ch["put"].replace("{n}", str(i)).replace("http://testserver", ""), content=part).status_code == 200
    done = api.post(ch["complete"].replace("http://testserver", ""), json={"filename": "nagy.txt", "chunks": 2})
    assert done.status_code == 201, done.text
    job = _wait(api, r["id"])
    f = next(t for t in job["tasks"] if t["name"] == "out")["result"]["files"][0]
    assert f["size"] == len(data) and f["filename"] == "nagy.txt"
    assert api.post(path, files={"file": ("x.txt", b"x")}).status_code in (403, 409)        # egyszer használatos


@pytest.mark.parametrize("tasks,code", [
    ({"a": {"operation": "nope"}}, "UNKNOWN_OPERATION"),
    ({"a": {"operation": "convert", "output_format": "pdf"}}, "INVALID_TASK"),
    ({"a": {"operation": "convert", "input": "b", "output_format": "pdf"}, "b": {"operation": "convert", "input": "a", "output_format": "pdf"}}, "INVALID_JOB"),
    ({"a": {"operation": "convert", "input": "zzz", "output_format": "pdf"}}, "INVALID_JOB"),
    ({"a": {"operation": "import/url", "url": "ftp://x"}}, "INVALID_TASK"),
    ({"a": {"operation": "import/base64", "file": "x"}}, "INVALID_TASK"),
    ({"a": {"operation": "import/s3", "bucket": "b"}}, "INVALID_TASK"),
])
def test_invalid_jobs(api, tasks, code):
    r = api.post("/v1/jobs", json={"tasks": tasks})
    assert r.status_code == 422 and r.json()["error"]["code"] == code, r.text


def test_failed_task_propagates(api):
    r = api.post("/v1/jobs", json={"tasks": {
        "in": {"operation": "import/base64", "file": _b64("szöveg"), "filename": "a.txt"},
        "x": {"operation": "convert", "input": "in", "output_format": "mp4"},
        "ok": {"operation": "convert", "input": "in", "output_format": "html"},
        "out": {"operation": "export/url", "input": "x"}}}).json()
    job = _wait(api, r["id"])
    st = {t["name"]: (t["status"], t["code"]) for t in job["tasks"]}
    assert job["status"] == "error"
    assert st["x"] == ("error", "CONVERSION_NOT_POSSIBLE") and st["out"] == ("error", "INPUT_TASK_FAILED")
    assert st["ok"][0] == "finished"                                   # a független ág lefut


def test_sse_events(api):
    r = api.post("/v1/jobs", json={"tasks": {"in": {"operation": "import/base64", "file": _b64("ő"), "filename": "a.txt"},
                                             "h": {"operation": "convert", "input": "in", "output_format": "html"}}}).json()
    events = []
    with api.stream("GET", f"/v1/jobs/{r['id']}/events") as s:
        for line in s.iter_lines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
                if events[-1] in ("job.finished", "job.failed"):
                    break
    assert events[0] == "job.snapshot" and events[-1] == "job.finished"


def test_webhook_signed(api, monkeypatch):
    from app import net
    from app.api_v1 import webhooks
    calls = []

    async def fake_post(url, body, headers, timeout=15):
        calls.append((url, body, headers))
        return 200

    async def ok(url):
        return None
    monkeypatch.setattr(net, "post_json", fake_post)
    monkeypatch.setattr(net, "check_url", ok)
    r = api.post("/v1/jobs", json={"webhook_url": "https://hooks.example.com/x", "tasks": {
        "in": {"operation": "import/base64", "file": _b64("ű"), "filename": "a.txt"}}}).json()
    _wait(api, r["id"])
    for _ in range(50):
        if calls:
            break
        time.sleep(0.05)
    url, body, headers = calls[0]
    assert url == "https://hooks.example.com/x" and headers["X-Webhook-Event"] == "job.finished"
    sec = webhooks.secret("admin")
    expect = "sha256=" + hmac.new(sec.encode(), f"{headers['X-Webhook-Timestamp']}.".encode() + body, hashlib.sha256).hexdigest()
    assert headers["X-Signature"] == expect
    assert json.loads(body)["job"]["id"] == r["id"]


def test_rate_limit(api, monkeypatch):
    from app.api_v1 import app as A
    monkeypatch.setenv("API_RATE_PER_MIN", "3")
    A._hits.clear()
    codes = [api.get("/v1/me").status_code for _ in range(4)]
    A._hits.clear()
    assert codes[:3] == [200, 200, 200] and codes[3] == 429


def test_ssrf_blocked(api, monkeypatch):
    monkeypatch.delenv("ALLOW_PRIVATE_URLS", raising=False)
    r = api.post("/v1/jobs", json={"tasks": {"in": {"operation": "import/url", "url": "http://127.0.0.1:9/secret"}}}).json()
    job = _wait(api, r["id"])
    assert job["tasks"][0]["code"] == "UNSAFE_URL"
    assert api.post("/v1/webhooks", json={"url": "http://169.254.169.254/latest"}).json()["error"]["code"] == "UNSAFE_URL"


def test_convert_shortcut(api):
    r = api.post("/v1/convert", json={"file": _b64("első"), "filename": "a.txt", "output_format": "html", "wait": 30})
    job = r.json()
    assert job["status"] == "finished", job
    assert job["tasks"][-1]["result"]["files"][0]["url"]


def test_sigv4_matches_aws_example():
    """AWS dokumentáció: GET /test.txt (examplebucket, 2013-05-24) — ismert aláírás."""
    import datetime as dt
    from app.connectors import sigv4_headers
    h = sigv4_headers("GET", "https://examplebucket.s3.amazonaws.com/test.txt", "us-east-1", "AKIAIOSFODNN7EXAMPLE",
                      "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                      "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                      now=dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc), extra={"Range": "bytes=0-9"})
    assert h["Authorization"].endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


@pytest.mark.asyncio
async def test_connectors_with_mock_transport(tmp_path, samples, put_file):
    import httpx
    from app import connectors
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, str(req.url), dict(req.headers)))
        if req.method == "GET" or "download" in str(req.url):
            if "fields=name" in str(req.url):
                return httpx.Response(200, json={"name": "doc.txt", "mimeType": "text/plain"})
            return httpx.Response(200, content="felhőből ő".encode())
        return httpx.Response(200, json={"id": "abc", "path_display": "/out/x"})
    t = httpx.MockTransport(handler)
    p, name = await connectors.fetch("dropbox", {"path": "/a/b.txt", "access_token": "tok"}, 1 << 20, tmp_path, transport=t)
    assert name == "b.txt" and p.read_text("utf-8") == "felhőből ő"
    assert json.loads(seen[-1][2]["dropbox-api-arg"]) == {"path": "/a/b.txt"}
    p, name = await connectors.fetch("s3", {"bucket": "bk", "key": "dir/f.txt", "region": "eu-central-1",
                                            "access_key_id": "AK", "secret_access_key": "SK"}, 1 << 20, tmp_path, transport=t)
    assert seen[-1][1] == "https://bk.s3.eu-central-1.amazonaws.com/dir/f.txt" and "AWS4-HMAC-SHA256" in seen[-1][2]["authorization"]
    p, name = await connectors.fetch("google-drive", {"file_id": "F1", "access_token": "tok"}, 1 << 20, tmp_path, transport=t)
    assert name == "doc.txt"
    fid = put_file(samples.txt())
    out = await connectors.push("s3", {"bucket": "bk", "key": "out/", "access_key_id": "AK", "secret_access_key": "SK"}, [fid], transport=t)
    assert out[0]["location"].startswith("s3://bk/out/") and seen[-1][0] == "PUT"
    out = await connectors.push("dropbox", {"path": "/out/", "access_token": "tok"}, [fid], transport=t)
    assert out[0]["location"] == "dropbox:///out/x"
    with pytest.raises(ValueError):
        connectors.validate_spec("s3", {"bucket": "b", "key": "k"})


def test_sandbox_and_billing_with_db(db_client):
    from app import billing, db
    call = db_client.portal.call
    u = call(db.user_create, "api@example.com", "jelszó123", "API Ödön", 1)
    live = call(db.api_key_create, u["id"], "live")["key"]
    sand = call(db.api_key_create, u["id"], "sb", True)["key"]
    assert sand.startswith("sk_test_")
    spec = {"tasks": {"in": {"operation": "import/base64", "file": _b64("ő"), "filename": "a.txt"},
                      "h": {"operation": "convert", "input": "in", "output_format": "html"}}}
    hs = {"Authorization": f"Bearer {sand}"}
    r = db_client.post("/v1/jobs", json=spec, headers=hs).json()
    job = db_client.get(f"/v1/jobs/{r['id']}", params={"wait": 30}, headers=hs).json()
    assert job["sandbox"] is True and job["status"] == "finished" and job["credits"] == 0
    assert call(db.usage_get, u["id"], billing.month_key(), "api_tasks") == 0
    hl = {"Authorization": f"Bearer {live}"}
    me = db_client.get("/v1/me", headers=hl).json()
    assert me["sandbox"] is False and me["webhook_secret"].startswith("whsec_")
    r = db_client.post("/v1/jobs", json=spec, headers=hl).json()
    job = db_client.get(f"/v1/jobs/{r['id']}", params={"wait": 30}, headers=hl).json()
    assert job["status"] == "finished"
    assert call(db.usage_get, u["id"], billing.month_key(), "api_tasks") == 1
    # a havi keret felett 20 feladatonként 1 kredit — 1 kredit van, a második blokk már nem fér bele
    call(db.usage_add, u["id"], billing.month_key(), "api_tasks", 249, None)
    r = db_client.post("/v1/jobs", json=spec, headers=hl).json()
    job = db_client.get(f"/v1/jobs/{r['id']}", params={"wait": 30}, headers=hl).json()
    assert job["status"] == "finished" and job["credits"] == 1
    call(db.usage_add, u["id"], billing.month_key(), "api_tasks", 19, None)
    r = db_client.post("/v1/jobs", json=spec, headers=hl).json()
    job = db_client.get(f"/v1/jobs/{r['id']}", params={"wait": 30}, headers=hl).json()
    assert job["status"] == "error" and next(t for t in job["tasks"] if t["name"] == "h")["code"] == "PAYMENT_REQUIRED"
    # a job a DB-ben is megvan (lista)
    assert any(j["id"] == r["id"] for j in db_client.get("/v1/jobs", headers=hl).json()["data"])
