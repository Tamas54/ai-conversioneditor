"""kie.ai adapter (docs.kie.ai, ellenőrizve 2026-10-19; Wan 3.0 és a képmodellek 2026-10-21).

    POST https://api.kie.ai/api/v1/jobs/createTask   {model, input, callBackUrl?} → data.taskId
    GET  https://api.kie.ai/api/v1/jobs/recordInfo?taskId=…   state: waiting | queuing | generating | success | fail;
         resultJson (JSON-szöveg) = {resultUrls: […], lastFrameUrl?}; creditsConsumed = a TÉNYLEGES költség (hibánál 0)
    GET  https://api.kie.ai/api/v1/chat/credit       → data = az egyenleg (kie-kredit; 1 kredit ≈ $0.005)
    POST https://kieai.redpandaai.co/api/file-stream-upload   (KÜLÖN host; multipart file + uploadPath) → data.downloadUrl
         (ingyenes, 24 óra után törlődik)
A hiba is HTTP 200, a kód a törzsben (401 kulcs, 402 nincs kredit, 422 rossz bemenet, 429 túl sok kérés, 455/5xx).
Korlát: fiókonként 20 új feladat / 10 s; a 429-es kérés nem kerül sorba → visszalépéssel újrapróbálható. A generált
fájlt 14 napig tartják → az eredményt AZONNAL letöltjük. Feladat-megszakítás nincs az API-ban (cancel: csak nálunk).
A callback (webhook) opcionálisan HMAC-cal aláírt (X-Webhook-Timestamp / X-Webhook-Signature, a kie beállításaiban
kapcsolható): base64(HMAC-SHA256(taskId + "." + timestamp, kulcs)) — a törzset nem fedi, ezért a callback után a
végeredményt mindig a recordInfo-ból olvassuk.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

import httpx

from app.providers.base import FAIL, PENDING, RUNNING, SUCCESS, Provider, ProviderError, TaskState

log = logging.getLogger("aice.kie")

BASE = os.getenv("KIE_BASE_URL", "https://api.kie.ai/api/v1")
UPLOAD_URL = os.getenv("KIE_UPLOAD_URL", "https://kieai.redpandaai.co/api/file-stream-upload")
STATES = {"waiting": PENDING, "queuing": PENDING, "generating": RUNNING, "success": SUCCESS, "fail": FAIL}

# Modellenkénti mezőnevek: a saját (egységes) bemenetünk kulcsa → a modell mezője (és típusa). Ami nincs a táblában,
# az nem mehet ki (elírás ne számlázzon más beállítást); a modell saját nevét is elfogadjuk.
_WAN = {
    "fields": {"prompt": "prompt", "first_frame": "first_frame_url", "last_frame": "last_frame_url",
               "reference_images": "reference_image_urls", "reference_videos": "reference_video_urls",
               "reference_audios": "reference_audio_urls", "audio": "audio", "resolution": "resolution",
               "aspect": "aspect_ratio", "duration": "duration", "seed": "seed", "nsfw_checker": "nsfw_checker"},
    "types": {"duration": int, "audio": bool, "resolution": lambda v: str(v).upper(), "seed": int, "nsfw_checker": bool},
    # első (+utolsó) kocka ÉS referencia mód nem keverhető (a kie elutasítaná — itt érthetőbben)
    "exclusive": (("first_frame", "last_frame"), ("reference_images", "reference_videos", "reference_audios")),
}
_FLUX2 = {"fields": {"prompt": "prompt", "aspect": "aspect_ratio", "resolution": "resolution", "images": "input_urls",
                     "nsfw_checker": "nsfw_checker"},
          "types": {"nsfw_checker": bool}}
MODELS: dict[str, dict[str, Any]] = {
    "wan/3-0-video": _WAN,                     # szöveg→videó, kép→videó (első / első+utolsó kocka), referencia mód
    "wan/3-0-video-prime": _WAN,               # ugyanaz, a kie „high-speed” változata (drágább)
    "kling/ai-avatar-standard": {"fields": {"image": "image_url", "audio": "audio_url", "prompt": "prompt"}},   # beszélő avatar
    "kling/ai-avatar-pro": {"fields": {"image": "image_url", "audio": "audio_url", "prompt": "prompt"}},
    "flux-2/pro-text-to-image": _FLUX2,
    "flux-2/pro-image-to-image": _FLUX2,
    "flux1-kontext": {                         # FLUX.1 Kontext [pro]: retus, helyreállítás, színezés (input.model választ)
        "fields": {"prompt": "prompt", "image": "input_image", "aspect": "aspect_ratio", "kontext_model": "model",
                   "format": "output_format", "translate": "enable_translation", "safety": "safety_tolerance"},
        "types": {"translate": bool, "safety": int},
    },
    "topaz/video-upscale": {
        "fields": {"video": "video_url", "factor": "upscale_factor"},      # upscale_factor SZÖVEG: "1" | "2" | "4"
        "types": {"factor": lambda v: str(int(float(v)))},
        "max_input_mb": 50,                                                # mp4 / mov / mkv
    },
    "topaz/image-upscale": {
        "fields": {"image": "image_url", "factor": "upscale_factor"},
        "types": {"factor": lambda v: str(int(float(v)))},
        "max_input_mb": 10,
    },
}


# kie-árak (a kie.ai/pricing adatforrása — api.kie.ai/client/v1/model-pricing/page —, ellenőrizve 2026-10-21; 1 kie-kredit
# = $0.005) — a pricing_rules induló sorai: (művelet, modell, szint, egység, kie-kredit/egység, megjegyzés). A Wan 3.0
# díjköteles másodpercei = a kimenet + a referencia-videók hossza (ugyanazon az áron); hang nélkül sem olcsóbb.
PRICES: list[tuple[str, str, str, str, float, str]] = [
    ("ai_clip", "wan/3-0-video", "480p", "second", 8, "Wan 3.0 480p, $0.04/s (+ reference video seconds)"),
    ("ai_clip", "wan/3-0-video", "720p", "second", 16, "Wan 3.0 720p, $0.08/s (+ reference video seconds)"),
    ("ai_clip", "wan/3-0-video", "1080p", "second", 32, "Wan 3.0 1080p, $0.16/s (+ reference video seconds)"),
    ("ai_clip", "wan/3-0-video-prime", "480p", "second", 12.2, "Wan 3.0 Prime (fast) 480p, $0.061/s"),
    ("ai_clip", "wan/3-0-video-prime", "720p", "second", 25.2, "Wan 3.0 Prime (fast) 720p, $0.126/s"),
    ("ai_clip", "wan/3-0-video-prime", "1080p", "second", 50.4, "Wan 3.0 Prime (fast) 1080p, $0.252/s"),
    ("avatar", "kling/ai-avatar-standard", "720p", "second", 8, "Kling AI Avatar 720p (lip sync, ≤ 5 min audio), $0.04/s"),
    ("avatar", "kling/ai-avatar-pro", "1080p", "second", 16, "Kling AI Avatar Pro 1080p, $0.08/s"),
    ("enhance", "topaz/video-upscale", "1x", "second", 8, "Topaz video upscale 1x/2x, $0.04/s"),
    ("enhance", "topaz/video-upscale", "2x", "second", 8, "Topaz video upscale 1x/2x, $0.04/s"),
    ("enhance", "topaz/video-upscale", "4x", "second", 14, "Topaz video upscale 4x, $0.07/s"),
]
# AI-képek: rögzített listaár (credits.list_price, ugyanaz a képlet), képenként: (művelet, modell, szint, kie-kredit).
# FLUX.2 [pro]: 1K ≈ 1 MP, 2K ≈ 4 MP (a nagyobb kért méret a 2K — kisebbet nem adunk, mint amit kértek).
IMAGE_PRICES: list[tuple[str, str, str, float]] = [
    ("ai_image", "flux-2/pro-text-to-image", "1K", 5),
    ("ai_image", "flux-2/pro-text-to-image", "2K", 7),
    ("ai_edit", "flux1-kontext", "pro", 5),
]


def image_cost(model: str, tier: str) -> float:
    return next(c for _, m, t, c in IMAGE_PRICES if m == model and t == tier)


def map_input(model: str, data: dict[str, Any]) -> dict[str, Any]:
    """A saját bemenet → a modell mezői (a táblából). Ismeretlen modell vagy mező → ProviderError (nem megy ki)."""
    spec = MODELS.get(model)
    if spec is None:
        raise ProviderError(f"kie model not in the mapping table: {model}")
    fields, types = spec["fields"], spec.get("types", {})
    native = set(fields.values())
    out: dict[str, Any] = {}
    for k, v in data.items():
        if v is None:
            continue
        name = fields.get(k) or (k if k in native else None)
        if name is None:
            raise ProviderError(f"{model} doesn't take {k!r} (takes: {', '.join(fields)})")
        key = next((c for c, n in fields.items() if n == name), k)
        out[name] = types[key](v) if key in types else v
    excl = spec.get("exclusive")
    if excl:
        used = [any(fields[k] in out for k in g) for g in excl]
        if sum(used) > 1:
            raise ProviderError("first/last-frame mode and reference mode can't be mixed — choose one")
    if "last_frame_url" in out and "first_frame_url" not in out:
        raise ProviderError("a last frame needs a first frame too")
    return out


class _Bucket:
    """Kliensoldali fék: legfeljebb n új feladat / ablak (a kie 20/10 s-e alatt maradunk)."""

    def __init__(self, n: int = 18, window: float = 10.0):
        self.n, self.window, self.q = n, window, deque()
        self.lock = asyncio.Lock()

    async def take(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                while self.q and self.q[0] < now - self.window:
                    self.q.popleft()
                if len(self.q) < self.n:
                    self.q.append(now)
                    return
                await asyncio.sleep(self.q[0] + self.window - now + 0.05)


class KieProvider(Provider):
    name = "kie"
    RETRIES = 5

    def __init__(self, key: Optional[str] = None):
        self._key = key or os.getenv("KIE_API_KEY", "")
        self._bucket = _Bucket()

    def same_key(self) -> bool:
        return self._key == os.getenv("KIE_API_KEY", "")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"}

    async def _call(self, method: str, url: str, *, retry_on_429: bool = True, **kw: Any) -> Any:
        """Egy kie-hívás: a törzs kódját nézi (HTTP 200 + code); 429 → exponenciális visszalépés (1, 2, 4, 8, 16 s)."""
        delay, timeout = 1.0, kw.pop("timeout", 60)
        for attempt in range(self.RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout) as c:
                    r = await c.request(method, url, headers=self._headers(), **kw)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:   # el sem ment: biztonságosan újrapróbálható
                if attempt >= self.RETRIES:
                    raise ProviderError(f"kie unreachable: {e}") from None
                await asyncio.sleep(delay)
                delay *= 2
                continue
            try:
                body = r.json()
            except ValueError:
                body = {"code": r.status_code, "msg": r.text[:200]}
            code = body.get("code", r.status_code) if isinstance(body, dict) else r.status_code
            if code == 429 or r.status_code == 429:
                if not retry_on_429 or attempt >= self.RETRIES:
                    raise ProviderError("kie: too many requests (429)", 429)
                log.info("kie 429 — %.0f s múlva újra", delay)
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if code != 200:
                raise ProviderError(f"kie error {code}: {str(body.get('msg') if isinstance(body, dict) else body)[:300]}",
                                    int(code) if str(code).isdigit() else None)
            return body.get("data")
        raise ProviderError("kie: gave up after retries", 429)

    async def create(self, model: str, input: dict[str, Any], *, callback_url: Optional[str] = None) -> str:
        payload: dict[str, Any] = {"model": model, "input": map_input(model, input)}
        if callback_url:
            payload["callBackUrl"] = callback_url
        await self._bucket.take()
        data = await self._call("POST", f"{BASE}/jobs/createTask", json=payload)
        tid = (data or {}).get("taskId")
        if not tid:
            raise ProviderError(f"kie: no taskId in the answer ({str(data)[:200]})")
        return tid

    async def status(self, task_id: str) -> TaskState:
        data = await self._call("GET", f"{BASE}/jobs/recordInfo", params={"taskId": task_id}) or {}
        return parse_record(data, task_id)

    async def cancel(self, task_id: str) -> bool:
        return False            # a kie API-ban nincs megszakítás: a hívó nálunk zárja le (a hold visszajár)

    async def balance(self) -> float:
        return float(await self._call("GET", f"{BASE}/chat/credit") or 0)

    async def upload(self, path: Any) -> str:
        """Helyi fájl → a kie feltöltő hostja (24 óráig él) → URL a feladat bemenetéhez."""
        p = Path(path)
        data = await self._call("POST", UPLOAD_URL, files={"file": (p.name, p.read_bytes())},     # újrapróbálható
                                data={"uploadPath": "studio", "fileName": p.name}, timeout=600)
        url = (data or {}).get("downloadUrl") or (data or {}).get("fileUrl")
        if not url:
            raise ProviderError(f"kie upload: no URL in the answer ({str(data)[:200]})")
        return url


def parse_record(data: dict[str, Any], task_id: str = "") -> TaskState:
    """recordInfo vagy callback data → egységes állapot (a resultJson JSON-SZÖVEG)."""
    raw_state = str(data.get("state") or "").lower()
    rj = data.get("resultJson") or data.get("response") or {}
    if isinstance(rj, str):
        try:
            rj = json.loads(rj) if rj.strip() else {}
        except ValueError:
            rj = {}
    urls = [u for u in (rj.get("resultUrls") or []) if isinstance(u, str)]
    cost = data.get("creditsConsumed")
    lf = rj.get("lastFrameUrl")
    return TaskState(task_id=data.get("taskId") or task_id, state=STATES.get(raw_state, PENDING), urls=urls,
                     cost=float(cost) if cost is not None else None,
                     error=(data.get("failMsg") or (f"failed ({data.get('failCode')})" if data.get("failCode") else None))
                     if raw_state == "fail" else None,
                     extra={"last_frame_url": lf[0] if isinstance(lf, list) and lf else lf} if lf else {},
                     raw={k: data.get(k) for k in ("state", "failCode", "failMsg", "costTime", "creditsConsumed", "model")})


def verify_signature(task_id: str, timestamp: str, signature: str, key: str, tolerance: int = 600) -> bool:
    """A kie webhook-aláírása (ha a kie beállításaiban be van kapcsolva): base64(HMAC-SHA256(taskId.timestamp))."""
    if not (task_id and timestamp and signature and key):
        return False
    try:
        if abs(time.time() - int(timestamp)) > tolerance:
            return False
    except ValueError:
        return False
    want = base64.b64encode(hmac.new(key.encode(), f"{task_id}.{timestamp}".encode(), hashlib.sha256).digest()).decode()
    return hmac.compare_digest(want, signature)
