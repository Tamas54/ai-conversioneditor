"""
Vision capability — Kimi K2.6 with image input.

Same model as the text agent (Kimi is multimodal); we just call it with
an image_url content block instead of a tool-calling loop.

Use cases (each is a thin wrapper):
    - ocr_image: read all text from the image
    - describe_image: free-form Q&A on the image
    - extract_table_from_image: structured rows extraction
"""
from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings

log = logging.getLogger("aice.vision")


_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
}


_MAX_IMAGE_LONG_EDGE = 1280  # SiliconFlow Kimi K2.6 vision sweet spot
_MAX_IMAGE_BYTES = 2_000_000  # ~2 MB cap for the encoded payload


def image_to_data_url(path: Path, long_edge: Optional[int] = None) -> str:
    """Encode an image as a data URL. If the image is too large (long edge
    > _MAX_IMAGE_LONG_EDGE px or file > _MAX_IMAGE_BYTES), downscale via
    Pillow before encoding. SiliconFlow returns HTTP 400 on oversize images."""
    suffix = path.suffix.lower().lstrip(".")
    raw = path.read_bytes()
    needs_resize = False
    try:
        from PIL import Image
        with Image.open(path) as img:
            w, h = img.size
            edge = long_edge or _MAX_IMAGE_LONG_EDGE
            if max(w, h) > edge or len(raw) > _MAX_IMAGE_BYTES:
                needs_resize = True
                scale = min(1.0, edge / max(w, h))
                long_edge = int(max(w, h) * scale)
                img = img.convert("RGB")  # ensure JPEG-safe mode
                # Re-encode and shrink further if the JPEG is still oversize.
                # Some heavy photos exceed the byte cap even at the pixel cap.
                quality = 85
                import io
                while True:
                    new_size = (int(w * scale), int(h * scale))
                    resized = img.resize(new_size, Image.LANCZOS) if scale < 1.0 else img
                    buf = io.BytesIO()
                    resized.save(buf, format="JPEG", quality=quality, optimize=True)
                    raw = buf.getvalue()
                    if len(raw) <= _MAX_IMAGE_BYTES or (long_edge <= 512 and quality <= 60):
                        break
                    if quality > 60:
                        quality -= 10
                    else:
                        long_edge = int(long_edge * 0.85)
                        scale = long_edge / max(w, h)
                mime = "image/jpeg"
    except ImportError:
        pass
    if not needs_resize:
        mime = _MIME_BY_EXT.get(suffix, "image/png")
    b64 = base64.b64encode(raw).decode()
    return f"data:{mime};base64,{b64}"


async def _call(messages: list[dict[str, Any]], *, response_format: Optional[dict] = None,
                max_tokens: int = 4096, model: Optional[str] = None) -> str:
    """Call Kimi K2.6 vision via SiliconFlow.

    SiliconFlow Kimi K2.6 vision endpoint returns HTTP 500 when called with
    `response_format={"type": "json_object"}`. We always strip this parameter
    on vision calls and rely on prompt-instruction + strict=False JSON parse
    on the caller side. This is a SiliconFlow-side bug; harmless to ignore.
    """
    if not settings.SILICONFLOW_API_KEY:
        raise RuntimeError("SILICONFLOW_API_KEY not set")
    from app import billing
    await billing.ai_step()          # MCP-ről: a szerver saját modellje dolgozik — AI-lépés (havi keret, felette kredit)
    payload: dict[str, Any] = {
        "model": model or settings.LLM_MODEL_VISION,  # DeepSeek-V4.1-Flash (leírás/kérdés/OCR)
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.1,
        # Disable thinking on vision calls. Kimi K2.6 defaults to thinking mode
        # which on a 1280px image OCR generates ~7000 reasoning tokens before
        # the answer — measured 172s vs 60s on the same Hungarian book page.
        # OCR is character recognition, not a reasoning task; thinking buys
        # nothing on accuracy here and triples the wall time.
        "enable_thinking": False,
    }
    # Intentionally NOT setting response_format on vision calls — see docstring.

    # Vision OCR on detailed images can take 60-180s — use a generous timeout
    # that's larger than the chat-only LLM_TIMEOUT_S
    vision_timeout = max(settings.LLM_TIMEOUT_S, 240)

    last_exc: Optional[Exception] = None
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(6),
        wait=wait_exponential(min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError, httpx.ReadTimeout)),
        reraise=False,
    ):
        with attempt:
            async with httpx.AsyncClient(timeout=vision_timeout) as client:
                r = await client.post(
                    f"{settings.SILICONFLOW_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.SILICONFLOW_API_KEY}"},
                    json=payload,
                )
                if r.status_code >= 500:
                    last_exc = httpx.HTTPStatusError(
                        f"vision call {r.status_code}: {r.text[:200]}",
                        request=r.request, response=r,
                    )
                    raise last_exc
                r.raise_for_status()
                data = r.json()
                return data["choices"][0]["message"].get("content") or ""
    raise last_exc or RuntimeError("vision call failed")


def _strip_json_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


async def ocr_image(image_path: Path, *, language_hint: Optional[str] = None) -> str:
    """Read all visible text from the image (handwriting OK)."""
    hint = f" The text appears to be in {language_hint}." if language_hint else ""
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": (
            f"Extract ALL visible text from this image, preserving line breaks "
            f"and structure as best you can.{hint} "
            f"Handwriting is fine — transcribe it.\n"
            f"Output ONLY the text, no commentary, no markdown fences."
        )},
        {"type": "image_url", "image_url": {"url": image_to_data_url(image_path)}},
    ]}]
    return await _call(msgs, max_tokens=4096)


async def describe_image(image_path: Path, question: Optional[str] = None) -> str:
    q = question or "Describe what you see in detail."
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": q},
        {"type": "image_url", "image_url": {"url": image_to_data_url(image_path)}},
    ]}]
    return await _call(msgs, max_tokens=2048)


async def extract_table_from_image(image_path: Path) -> dict[str, Any]:
    """Returns {'headers': [...], 'rows': [[...], ...]}."""
    prompt = (
        "Extract the table visible in this image as STRICT JSON with this shape:\n"
        '{"headers": ["col1","col2",...], "rows": [["v","v",...], ...]}\n\n'
        "Rules:\n"
        "- Identify a header row if present; otherwise headers can be empty array.\n"
        "- All cell values as strings (preserve numbers as strings).\n"
        "- Output ONLY the JSON object, no prose, no markdown fences."
    )
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": image_to_data_url(image_path)}},
    ]}]
    raw = await _call(msgs, response_format={"type": "json_object"}, max_tokens=4096)
    raw = _strip_json_fences(raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"vision returned non-JSON: {e}; raw[:200]={raw[:200]}")
    if not isinstance(data, dict) or "rows" not in data:
        raise ValueError(f"vision returned unexpected shape: keys={list(data) if isinstance(data, dict) else type(data)}")
    if "headers" not in data:
        data["headers"] = []
    return data
