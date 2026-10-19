"""kie.ai adapter (OMLETT 2.1), hálózat nélkül: modellenkénti mezőnevek, 429 → visszalépés, a törzs hibakódja, a
recordInfo / callback értelmezése, webhook-aláírás; a feladat életciklusa egy hamis szolgáltatóval: hold → feladat →
lekérdezés → letöltés a saját tárhelyre → capture a tényleges költséggel; hiba → teljes release; webhook idempotensen."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

import httpx
import pytest


# ---------- az adapter ----------

def test_field_mapping_per_model():
    from app.providers import ProviderError
    from app.providers.kie import map_input
    got = map_input("bytedance/seedance-2-5", {"prompt": "a cat", "duration": "5", "resolution": "480p", "audio": 0,
                                               "first_frame": "https://x/f.jpg", "draft": True})
    assert got == {"prompt": "a cat", "duration": 5, "resolution": "480p", "generate_audio": False,
                   "first_frame_url": "https://x/f.jpg", "draft": True}
    assert map_input("topaz/video-upscale", {"video": "https://x/v.mp4", "factor": 2}) == \
        {"video_url": "https://x/v.mp4", "upscale_factor": "2"}
    assert map_input("bytedance/seedance-2-5", {"generate_audio": True}) == {"generate_audio": True}   # a modell saját neve is
    with pytest.raises(ProviderError, match="doesn't take"):
        map_input("topaz/video-upscale", {"video": "u", "resolution": "1080p"})
    with pytest.raises(ProviderError, match="can't be mixed"):
        map_input("bytedance/seedance-2-5", {"first_frame": "a", "reference_images": ["b"]})
    with pytest.raises(ProviderError, match="first frame"):
        map_input("bytedance/seedance-2-5", {"last_frame": "a"})
    with pytest.raises(ProviderError, match="mapping table"):
        map_input("bytedance/seedance-9", {"prompt": "x"})


_REAL_CLIENT = httpx.AsyncClient


def _mock(monkeypatch, handler):
    from app.providers import kie
    monkeypatch.setattr(kie.httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    slept = []

    async def sleep(s):
        slept.append(s)
    monkeypatch.setattr(kie.asyncio, "sleep", sleep)
    return slept


async def test_create_backs_off_on_429_and_reads_body_codes(monkeypatch):
    from app.providers import ProviderError
    from app.providers.kie import KieProvider
    calls = []

    def handler(req: httpx.Request):
        calls.append(json.loads(req.content))
        assert req.headers["authorization"] == "Bearer k-test"
        if len(calls) < 3:
            return httpx.Response(200, json={"code": 429, "msg": "rate limited"})     # a kie a hibát is 200-zal adja
        return httpx.Response(200, json={"code": 200, "msg": "success", "data": {"taskId": "task_1"}})
    slept = _mock(monkeypatch, handler)
    p = KieProvider("k-test")
    assert await p.create("topaz/video-upscale", {"video": "https://x/v.mp4", "factor": 4}, callback_url="https://cb") == "task_1"
    assert slept == [1.0, 2.0] and calls[-1] == {"model": "topaz/video-upscale", "callBackUrl": "https://cb",
                                                 "input": {"video_url": "https://x/v.mp4", "upscale_factor": "4"}}

    _mock(monkeypatch, lambda req: httpx.Response(200, json={"code": 402, "msg": "Credits insufficient"}))
    with pytest.raises(ProviderError) as e:
        await p.create("topaz/video-upscale", {"video": "u", "factor": 2})
    assert e.value.code == 402


async def test_status_balance_and_upload(monkeypatch):
    from app.providers.base import SUCCESS
    from app.providers.kie import KieProvider

    def handler(req: httpx.Request):
        if req.url.path.endswith("/recordInfo"):
            assert req.url.params["taskId"] == "t9"
            return httpx.Response(200, json={"code": 200, "data": {
                "taskId": "t9", "state": "success", "creditsConsumed": 112,
                "resultJson": json.dumps({"resultUrls": ["https://tempfile.example/a.mp4"], "lastFrameUrl": ["https://t/l.jpg"]})}})
        if req.url.path.endswith("/chat/credit"):
            return httpx.Response(200, json={"code": 200, "msg": "success", "data": 987.5})
        assert req.url.host == "kieai.redpandaai.co" and b"uploadPath" in req.content     # a feltöltés KÜLÖN host
        return httpx.Response(200, json={"success": True, "code": 200, "data": {"downloadUrl": "https://tempfile.redpandaai.co/x.png"}})
    _mock(monkeypatch, handler)
    p = KieProvider("k")
    st = await p.status("t9")
    assert st.state == SUCCESS and st.urls == ["https://tempfile.example/a.mp4"] and st.cost == 112
    assert st.extra == {"last_frame_url": "https://t/l.jpg"}
    assert await p.balance() == 987.5


def test_parse_failed_record_and_signature():
    from app.providers.base import FAIL, PENDING, RUNNING
    from app.providers.kie import parse_record, verify_signature
    st = parse_record({"taskId": "t", "state": "fail", "failCode": "400", "failMsg": "bad prompt", "creditsConsumed": 0})
    assert st.state == FAIL and st.error == "bad prompt" and st.cost == 0 and st.urls == []
    assert parse_record({"state": "queuing"}).state == PENDING and parse_record({"state": "generating"}).state == RUNNING
    ts = str(int(time.time()))
    sig = base64.b64encode(hmac.new(b"hk", f"t1.{ts}".encode(), hashlib.sha256).digest()).decode()
    assert verify_signature("t1", ts, sig, "hk")
    assert not verify_signature("t2", ts, sig, "hk") and not verify_signature("t1", "1", sig, "hk")


# ---------- az életciklus egy hamis szolgáltatóval ----------

class FakeProv:
    name = "kie"

    def __init__(self):
        from app.providers.base import RUNNING, TaskState
        self.tasks: dict[str, TaskState] = {}
        self.created: list[tuple[str, dict]] = []
        self._ts, self._run = TaskState, RUNNING

    def same_key(self):
        return True

    async def create(self, model, input, callback_url=None):
        tid = f"t{len(self.created) + 1}_{secrets.token_hex(3)}"
        self.created.append((model, input))
        self.tasks[tid] = self._ts(task_id=tid, state=self._run)
        return tid

    async def status(self, tid):
        return self.tasks[tid]

    async def fetch_result(self, st):
        from app.storage import new_file_id, output_path
        fid = new_file_id("mp4")
        output_path(fid).write_bytes(b"clip:" + st.task_id.encode())
        return [fid]

    async def cancel(self, tid):
        return False

    def finish(self, tid, ok=True, cost=80.0):
        from app.providers.base import FAIL, SUCCESS
        self.tasks[tid] = self._ts(task_id=tid, state=SUCCESS if ok else FAIL, cost=cost if ok else 0.0,
                                   urls=["https://x/r.mp4"] if ok else [], error=None if ok else "content policy")


@pytest.fixture()
def fake(db_client, monkeypatch):
    from app import credits, providers
    from app.tools import ai_video
    prov = FakeProv()
    monkeypatch.setattr(providers, "get", lambda name="kie": prov)
    monkeypatch.setattr(providers, "enabled_for", lambda uid=None: True)
    monkeypatch.setattr(ai_video, "POLL_FIRST_S", 0.05)
    db_client.portal.call(lambda: credits.rule_save({"operation": "t_gen", "model": "m", "unit": "second",
                                                     "provider_cost": 28, "multiplier": 0.15, "min_charge": 1}))
    return prov


def _user(call, n):
    from app import db
    return call(db.user_create, f"{secrets.token_hex(6)}@example.com", "jelszó123456", None, n)["id"]


def _as(uid, fn):
    from app import tenancy

    async def run():
        tok = tenancy.set_user(uid)
        try:
            return await fn()
        finally:
            tenancy.reset(tok)
    return run


def test_lifecycle_success_captures_actual_cost(db_client, fake):
    from app import db, tenancy
    from app.storage import output_path
    from app.tools import ai_video
    call = db_client.portal.call
    uid = _user(call, 40)
    op = call(_as(uid, lambda: ai_video.submit("t_gen", "m", [{"prompt": "x"}], hold=17, meta={"units": 4})))
    assert call(db.user_get, uid)["credits"] == 23                          # a becsült maximum lefoglalva
    tid = call(db._rows, "SELECT provider_task_id FROM provider_tasks WHERE id = %s", (op,))[0]["provider_task_id"]
    fake.finish(tid, cost=80)                                               # a kie tényleges költsége: 80 kie-kredit

    res = call(_as(uid, lambda: ai_video.wait(op)))
    assert res["status"] == "done" and res["credits_charged"] == 12          # ceil(80 × 0.15)
    assert call(db.user_get, uid)["credits"] == 28
    fid = res["result"]["files"][0]
    tok = tenancy.set_user(uid)
    try:
        assert output_path(fid).read_bytes().startswith(b"clip:")           # a saját tárhelyen
    finally:
        tenancy.reset(tok)
    led = call(db._rows, "SELECT kind, delta, provider_cost FROM credit_ledger WHERE user_id = %s ORDER BY id", (uid,))
    assert [(r["kind"], r["delta"]) for r in led] == [("grant", 40), ("hold", -17), ("release", 17), ("capture", -12)]
    assert float(led[-1]["provider_cost"]) == 80
    assert call(_as(uid, lambda: ai_video.get(op)))["status"] == "done"
    other = _user(call, 1)
    with pytest.raises(ai_video.AIVideoError):                              # más feladata nem látszik
        call(_as(other, lambda: ai_video.get(op)))


def test_lifecycle_failure_releases_everything(db_client, fake):
    from app import db
    from app.tools import ai_video
    call = db_client.portal.call
    uid = _user(call, 40)
    op = call(_as(uid, lambda: ai_video.submit("t_gen", "m", [{"prompt": "x"}, {"prompt": "y"}], hold=30, meta={"units": 7})))
    tids = [r["provider_task_id"] for r in call(db._rows, "SELECT provider_task_id FROM provider_tasks WHERE meta->>'op' = %s "
                                                           "ORDER BY id", (op,))]
    fake.finish(tids[0])
    fake.finish(tids[1], ok=False)
    res = call(_as(uid, lambda: ai_video.wait(op)))
    assert res["status"] == "failed" and "content policy" in res["error"]
    assert call(db.user_get, uid)["credits"] == 40


def test_insufficient_credits_never_creates_a_provider_task(db_client, fake):
    from app import db
    from app.tools import ai_video
    call = db_client.portal.call
    uid = _user(call, 5)
    with pytest.raises(db.InsufficientCredits) as e:
        call(_as(uid, lambda: ai_video.submit("t_gen", "m", [{"prompt": "x"}], hold=17, meta={})))
    assert (e.value.needed, e.value.available) == (17, 5) and fake.created == []
    assert call(db.user_get, uid)["credits"] == 5


def test_webhook_is_idempotent_and_guarded(db_client, fake, monkeypatch):
    from app import db
    from app.tools import ai_video
    call = db_client.portal.call
    monkeypatch.setenv("KIE_WEBHOOK_SECRET", "s3cret")
    uid = _user(call, 40)
    op = call(_as(uid, lambda: ai_video.submit("t_gen", "m", [{"prompt": "x"}], hold=17, meta={"units": 4})))
    tid = call(db._rows, "SELECT provider_task_id FROM provider_tasks WHERE id = %s", (op,))[0]["provider_task_id"]
    body = {"code": 200, "data": {"taskId": tid, "state": "success"}}
    assert db_client.post("/webhooks/kie?token=wrong", json=body).status_code == 403
    r = db_client.post("/webhooks/kie?token=s3cret", json=body).json()
    assert r["result"] == "not finished"                    # a callback csak jelzés: a kie szerint még fut
    fake.finish(tid, cost=100)
    assert db_client.post("/webhooks/kie?token=s3cret", json=body).json()["result"] == "settled"
    assert db_client.post("/webhooks/kie?token=s3cret", json=body).json()["result"] == "duplicate"
    assert call(db.user_get, uid)["credits"] == 25           # 40 − ceil(100 × 0.15) — egyszer
    assert db_client.post("/webhooks/kie?token=s3cret", json={"data": {"taskId": "nope"}}).json()["result"] == "unknown"
    # HMAC is be van kapcsolva → aláírás nélkül elutasítva
    monkeypatch.setenv("KIE_WEBHOOK_HMAC_KEY", "hk")
    assert db_client.post("/webhooks/kie?token=s3cret", json=body).status_code == 403
    ts = str(int(time.time()))
    sig = base64.b64encode(hmac.new(b"hk", f"{tid}.{ts}".encode(), hashlib.sha256).digest()).decode()
    r = db_client.post("/webhooks/kie?token=s3cret", json=body, headers={"X-Webhook-Timestamp": ts, "X-Webhook-Signature": sig})
    assert r.status_code == 200 and r.json()["result"] == "duplicate"


def test_webhook_off_without_secret(client, monkeypatch):
    monkeypatch.delenv("KIE_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("KIE_WEBHOOK_HMAC_KEY", raising=False)
    assert client.post("/webhooks/kie", json={"data": {"taskId": "x"}}).status_code == 404


def test_polling_fallback_and_cancel(db_client, fake):
    """Senki nem vár rá (újraindítás): a lekérdezés (resume) fejezi be; a megszakítás a holdot visszaadja."""
    from app import db
    from app.tools import ai_video
    call = db_client.portal.call
    uid = _user(call, 40)
    op = call(_as(uid, lambda: ai_video.submit("t_gen", "m", [{"prompt": "x"}], hold=17, meta={"units": 4})))
    tid = call(db._rows, "SELECT provider_task_id FROM provider_tasks WHERE id = %s", (op,))[0]["provider_task_id"]
    fake.finish(tid, cost=40)
    rows = call(db._rows, "SELECT * FROM provider_tasks WHERE id = %s", (op,))
    call(lambda: ai_video.poll(rows))
    assert call(db.user_get, uid)["credits"] == 34           # ceil(40 × 0.15) = 6
    op2 = call(_as(uid, lambda: ai_video.submit("t_gen", "m", [{"prompt": "y"}], hold=17, meta={"units": 4})))
    assert call(db.user_get, uid)["credits"] == 17
    assert call(_as(uid, lambda: ai_video.cancel(op2)))["status"] == "cancelled"
    assert call(db.user_get, uid)["credits"] == 34


def test_feature_flag(monkeypatch):
    from app import providers
    monkeypatch.delenv("AI_VIDEO", raising=False)
    monkeypatch.setenv("KIE_API_KEY", "x")
    assert providers.flag() == "" and not providers.enabled_for("admin") and providers.active() == {}
    monkeypatch.setenv("AI_VIDEO", "admin")
    assert providers.enabled_for("admin") and not providers.enabled_for("abc123def")
    monkeypatch.setenv("AI_VIDEO", "1")
    assert providers.enabled_for("abc123def") and list(providers.active()) == ["kie"]
    monkeypatch.delenv("KIE_API_KEY")
    assert not providers.enabled_for("admin")
