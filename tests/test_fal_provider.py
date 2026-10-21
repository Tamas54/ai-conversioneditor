"""fal.ai adapter (élesítés: SeedVR2 / Topaz, GPU nélküli szerverre), hálózat nélkül: mezők, sor-API (beküldés →
állapot → eredmény), hiba a COMPLETED-ben és az eredmény-hívásban, megszakítás, CDN-feltöltés; a motorválasztás és az
ár a motor árlapja szerint (SeedVR2: kimeneti megapixel, a fal Topaza: felbontás-sáv × mp, a kie Topaza: szorzó × mp)."""
from __future__ import annotations

import json

import httpx
import pytest

_REAL_CLIENT = httpx.AsyncClient


def _mock(monkeypatch, handler):
    from app.providers import fal
    monkeypatch.setattr(fal.httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))

    async def sleep(s):
        return None
    monkeypatch.setattr(fal.asyncio, "sleep", sleep)


def test_field_mapping():
    from app.providers import ProviderError
    from app.providers.fal import SEEDVR, TOPAZ, map_input
    assert map_input(SEEDVR, {"video": "https://x/v.mp4", "factor": 4}) == {
        "video_url": "https://x/v.mp4", "upscale_factor": 4, "upscale_mode": "factor", "output_format": "X264 (.mp4)",
        "output_quality": "maximum"}
    assert map_input(TOPAZ, {"video": "u", "factor": 2, "topaz_model": "Starlight Precise 2.5"}) == {
        "video_url": "u", "upscale_factor": 2, "model": "Starlight Precise 2.5", "H264_output": True}
    with pytest.raises(ProviderError, match="doesn't take"):
        map_input(SEEDVR, {"video": "u", "prompt": "x"})
    with pytest.raises(ProviderError, match="mapping table"):
        map_input("fal-ai/other", {"video": "u"})


async def test_queue_lifecycle_and_upload(monkeypatch):
    from app.providers.base import FAIL, PENDING, RUNNING, SUCCESS
    from app.providers.fal import SEEDVR, FalProvider
    seen = []
    status = {"n": 0}

    def handler(req: httpx.Request):
        seen.append((req.method, str(req.url)))
        if req.url.host == "rest.fal.ai":
            assert req.headers["authorization"] == "Key fk"
            return httpx.Response(200, json={"token": "cdn-t", "token_type": "Bearer", "base_url": "https://v3.fal.media",
                                             "expires_at": "2099-01-01T00:00:00+00:00"})
        if req.url.host == "v3.fal.media":
            assert req.headers["authorization"] == "Bearer cdn-t" and req.headers["x-fal-file-name"] == "in.mp4"
            assert req.content == b"VIDEO"
            return httpx.Response(200, json={"access_url": "https://v3.fal.media/files/abc/in.mp4"})
        assert req.headers["authorization"] == "Key fk"
        if req.method == "POST":
            assert req.url.path == "/" + SEEDVR and json.loads(req.content)["video_url"] == "https://v3.fal.media/files/abc/in.mp4"
            return httpx.Response(200, json={"request_id": "r1", "status_url": "…", "response_url": "…"})
        if req.url.path.endswith("/status"):
            assert req.url.path == "/fal-ai/seedvr/requests/r1/status"          # az app (az első két tag), nem a teljes út
            status["n"] += 1
            return httpx.Response(200, json=[{"status": "IN_QUEUE", "queue_position": 2}, {"status": "IN_PROGRESS"},
                                             {"status": "COMPLETED", "metrics": {"inference_time": 40}}][status["n"] - 1])
        if req.method == "PUT":
            return httpx.Response(202, json={"status": "CANCELLATION_REQUESTED"})
        assert req.url.path == "/fal-ai/seedvr/requests/r1"
        return httpx.Response(200, json={"video": {"url": "https://v3.fal.media/files/out.mp4", "content_type": "video/mp4"}})
    _mock(monkeypatch, handler)
    p = FalProvider("fk")
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "in.mp4"
        src.write_bytes(b"VIDEO")
        url = await p.upload(src)
    tid = await p.create(SEEDVR, {"video": url, "factor": 4})
    assert tid == "fal-ai/seedvr|r1"
    assert (await p.status(tid)).state == PENDING
    assert (await p.status(tid)).state == RUNNING
    st = await p.status(tid)
    assert st.state == SUCCESS and st.urls == ["https://v3.fal.media/files/out.mp4"] and st.cost is None
    assert await p.cancel(tid) is True
    assert not p.has_balance

    def failing(req: httpx.Request):
        if req.url.path.endswith("/status"):
            return httpx.Response(200, json={"status": "COMPLETED", "error": "CUDA out of memory", "error_type": "runner"})
        return httpx.Response(200, json={})
    _mock(monkeypatch, failing)
    st = await p.status(tid)
    assert st.state == FAIL and "out of memory" in st.error and st.cost == 0.0      # a hibás feladat nem fizetős

    def bad_input(req: httpx.Request):
        if req.url.path.endswith("/status"):
            return httpx.Response(200, json={"status": "COMPLETED"})
        return httpx.Response(422, json={"detail": [{"msg": "video too long"}]})
    _mock(monkeypatch, bad_input)
    st = await p.status(tid)
    assert st.state == FAIL and "422" in st.error


def test_engine_choice_and_price_per_engine(monkeypatch):
    from app.tools import ai_video
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.delenv("ENHANCE_ENGINE", raising=False)
    assert ai_video.enhance_engine() == "topaz"                       # fal nélkül a kie Topaza (a régi út)
    monkeypatch.setenv("FAL_KEY", "fk")
    assert ai_video.enhance_engine() == "seedvr"                      # a régi, tömörített anyag referenciája
    monkeypatch.setenv("ENHANCE_ENGINE", "topaz-starlight")
    assert ai_video.enhance_engine() == "topaz-starlight"
    info = {"width": 360, "height": 640, "fps": 30}
    # SeedVR2: 4× → 1440×2560 × 30 fps × 13 s = 1437,6 MP → 1438 egység ($0.001/MP = $1.44)
    assert ai_video._enhance_units("seedvr", 4, info, 13) == ("any", 1438.0)
    assert ai_video._enhance_units("topaz-starlight", 4, info, 13) == ("4k", 13.0)
    assert ai_video._enhance_units("topaz-proteus", 2, info, 13) == ("720p", 13.0)
    assert ai_video._enhance_units("topaz", 4, info, 13) == ("4x", 13.0)


def test_fal_prices_use_the_same_credit_per_dollar_as_kie():
    from app import credits
    rules = {(r["provider"], r["model"], r["tier"]): r for r in credits.default_rules()}
    kie = rules[("kie", "wan/3-0-video", "720p")]
    seed = rules[("fal", "fal-ai/seedvr/upscale/video", "any")]
    # 1 kie-kredit = $0.005 → ugyanannyi Studio-kredit egy dollárért
    assert seed["multiplier"] == pytest.approx(kie["multiplier"] / 0.005)
    assert credits.price(seed, 1438) == credits.price({**kie, "provider_cost": 1}, 1438 * 0.001 / 0.005)
