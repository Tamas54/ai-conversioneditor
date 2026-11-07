"""Látómodell-hívás elfogyott egyenleggel (11-07): a háttér-eltávolítás a SiliconFlow 402-jét 2 modell × 6 próbával,
exponenciális várakozással ismételte (~70 s) — a kliens időtúllépéssel feladta, mire a kész kép elkészült.
Most: a 4xx (a 429 kivételével) azonnal hiba, mint a chat-kliensben, és 402-nél a tartalékmodell sem fut."""
from __future__ import annotations

import asyncio

import httpx
import pytest


def _fake_client(monkeypatch, status: int, calls: list):
    from app.llm import vision
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(status, json={"code": 30001, "message": "Sorry, your account balance is insufficient"})

    monkeypatch.setattr(vision.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(vision.settings, "SILICONFLOW_API_KEY", "test-key")


@pytest.mark.parametrize("status", [400, 402, 403])
def test_vision_call_does_not_retry_a_client_error(monkeypatch, status):
    from app.llm import vision
    from app.llm.siliconflow import LLMServiceError
    calls: list = []
    _fake_client(monkeypatch, status, calls)
    with pytest.raises(LLMServiceError) as e:
        asyncio.run(vision._call([{"role": "user", "content": "x"}], model="m"))
    assert len(calls) == 1 and e.value.status == status
    if status == 402:
        assert "out of credit" in str(e.value)


def test_find_objects_skips_the_fallback_model_when_out_of_credit(monkeypatch):
    from PIL import Image
    from app.llm import vision
    from app.llm.siliconflow import LLMServiceError
    from app.tools import cutout
    models: list = []

    async def fake_call(msgs, max_tokens=0, model=None):
        models.append(model)
        raise LLMServiceError(402, model)
    monkeypatch.setattr(vision, "_call", fake_call)
    out = asyncio.run(cutout._find_objects(Image.new("RGB", (64, 64)), [], held=True))
    assert len(models) == 1                                    # a tartalékmodell ugyanazon a fiókon: nem próbáljuk
    assert out["objects"] == [] and "out of credit" in out["error"]
