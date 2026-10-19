"""fal.ai adapter — a videó-felskálázás (élesítés) szolgáltatója; a GPU a falnál fut, a szerveren nem kell (10-19:
a kie Topaza egész nap failCode 500-at adott, a kie saját mintáján is; a fal a Topaz hivatalos partnere, és a SeedVR2 is
ott van, így egy kulccsal összemérhetők).

    POST https://queue.fal.run/{endpoint}               a modell bemenete JSON-ban → request_id, status_url, response_url
    GET  https://queue.fal.run/{app}/requests/{id}/status   IN_QUEUE | IN_PROGRESS | COMPLETED (+ error, error_type)
    GET  https://queue.fal.run/{app}/requests/{id}          az eredmény (modellenként; itt: video.url)
    PUT  https://queue.fal.run/{app}/requests/{id}/cancel   202 CANCELLATION_REQUESTED | 400 ALREADY_COMPLETED
    Fejléc: Authorization: Key $FAL_KEY. {app} = az endpoint első két tagja (fal-ai/seedvr/upscale/video → fal-ai/seedvr).
    Feltöltés (a hivatalos fal_client szerint, 2026-10): POST https://rest.fal.ai/storage/auth/token?storage_type=fal-cdn-v3
    → {token, token_type, base_url}; POST https://v3.fal.media/files/upload (a nyers bájtok, Content-Type,
    X-Fal-File-Name; Authorization: <token_type> <token>) → access_url. 100 MB felett a fal multipartot kér — a hívó
    darabol (MAX_INPUT_MB).
A fal a válaszban nem adja meg a költséget: a levonás a becslés (a kimenet mérete × az árlap), egyenleg-API sincs —
a napi egyeztetés kihagyja (has_balance = False). Feladat-azonosító nálunk: "{app}|{request_id}" (a lekérdezéshez az
app kell). Webhook nincs bekötve: a lekérdezés (és a resume_loop) fejezi be.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any, Optional

import httpx

from app.providers.base import FAIL, PENDING, RUNNING, SUCCESS, Provider, ProviderError, TaskState

log = logging.getLogger("aice.providers.fal")

QUEUE = "https://queue.fal.run"
TOKEN_URL = "https://rest.fal.ai/storage/auth/token?storage_type=fal-cdn-v3"
CDN_UPLOAD = "https://v3.fal.media/files/upload"
MAX_INPUT_MB = 95                     # egy darabban feltölthető (a fal 100 MB felett multipartot kér)

SEEDVR = "fal-ai/seedvr/upscale/video"
TOPAZ = "fal-ai/topaz/upscale/video"

# a saját (egységes) bemenet → a modell mezői; ami nincs itt, az nem megy ki (elírás ne számlázzon más beállítást)
MODELS: dict[str, dict[str, Any]] = {
    SEEDVR: {"fields": {"video": "video_url", "factor": "upscale_factor"},
             "fixed": {"upscale_mode": "factor", "output_format": "X264 (.mp4)", "output_quality": "maximum"}},
    TOPAZ: {"fields": {"video": "video_url", "factor": "upscale_factor", "topaz_model": "model"},
            "fixed": {"H264_output": True}},
}

# fal-árak (fal.ai/models/…/llms.txt, 2026-10-19), USD: (művelet, modell, szint, egység, $/egység, megjegyzés)
# SeedVR2: $0.001 / megapixel kimenet (szélesség × magasság × képkockák) — 1080×1920 @30 = ~$0.062/s.
# Topaz: a KIMENET felbontása szerint másodpercenként; a generatív Starlight-modellek ára a lapon nincs külön — élesítés
# (AI_VIDEO=1) előtt ellenőrizni.
PRICES: list[tuple[str, str, str, str, float, str]] = [
    ("enhance", SEEDVR, "any", "megapixel", 0.001, "SeedVR2 on fal: $0.001 per output megapixel (w×h×frames)"),
    ("enhance", TOPAZ, "720p", "second", 0.01, "Topaz on fal, output ≤720p: $0.01/s"),
    ("enhance", TOPAZ, "1080p", "second", 0.02, "Topaz on fal, output ≤1080p: $0.02/s"),
    ("enhance", TOPAZ, "4k", "second", 0.08, "Topaz on fal, output >1080p: $0.08/s (Starlight price unverified)"),
]


def app_of(model: str) -> str:
    return "/".join(model.split("/")[:2])


def map_input(model: str, data: dict[str, Any]) -> dict[str, Any]:
    spec = MODELS.get(model)
    if spec is None:
        raise ProviderError(f"fal model not in the mapping table: {model}")
    out: dict[str, Any] = dict(spec.get("fixed") or {})
    for k, v in data.items():
        if v is None:
            continue
        name = spec["fields"].get(k)
        if name is None:
            raise ProviderError(f"{model} doesn't take {k!r} (takes: {', '.join(spec['fields'])})")
        out[name] = v
    return out


class FalProvider(Provider):
    name = "fal"
    has_balance = False
    RETRIES = 4

    def __init__(self, key: Optional[str] = None):
        self._key = key or os.getenv("FAL_KEY", "")

    def same_key(self) -> bool:
        return self._key == os.getenv("FAL_KEY", "")

    async def _call(self, method: str, url: str, *, headers: Optional[dict[str, str]] = None, ok: tuple = (200,),
                    **kw: Any) -> Any:
        """Egy fal-hívás; 429 és a kapcsolódási hiba → visszalépéssel újra (1, 2, 4, 8 s)."""
        delay, timeout = 1.0, kw.pop("timeout", 60)
        for attempt in range(self.RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout) as c:
                    r = await c.request(method, url, headers=headers or {"Authorization": f"Key {self._key}"}, **kw)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                if attempt >= self.RETRIES:
                    raise ProviderError(f"fal unreachable: {e}") from None
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if r.status_code == 429 and attempt < self.RETRIES:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            try:
                body = r.json()
            except ValueError:
                body = {"detail": r.text[:300]}
            if r.status_code not in ok:
                detail = body.get("detail") if isinstance(body, dict) else body
                raise ProviderError(f"fal error {r.status_code}: {str(detail)[:300]}",
                                    402 if r.status_code in (402, 403) and "balance" in str(detail).lower() else r.status_code)
            return body
        raise ProviderError("fal: gave up after retries", 429)

    async def create(self, model: str, input: dict[str, Any], *, callback_url: Optional[str] = None) -> str:
        body = await self._call("POST", f"{QUEUE}/{model}", json=map_input(model, input), ok=(200, 201, 202))
        rid = (body or {}).get("request_id")
        if not rid:
            raise ProviderError(f"fal: no request_id in the answer ({str(body)[:200]})")
        return f"{app_of(model)}|{rid}"

    async def status(self, task_id: str) -> TaskState:
        app, _, rid = task_id.partition("|")
        st = await self._call("GET", f"{QUEUE}/{app}/requests/{rid}/status", ok=(200, 202))
        s = str((st or {}).get("status") or "").upper()
        raw = {k: st.get(k) for k in ("status", "error", "error_type", "queue_position", "metrics") if isinstance(st, dict)}
        if s == "IN_QUEUE":
            return TaskState(task_id=task_id, state=PENDING, raw=raw)
        if s != "COMPLETED":
            return TaskState(task_id=task_id, state=RUNNING, raw=raw)
        if st.get("error"):
            return TaskState(task_id=task_id, state=FAIL, error=str(st["error"])[:300], cost=0.0, raw=raw)
        try:
            res = await self._call("GET", f"{QUEUE}/{app}/requests/{rid}")
        except ProviderError as e:                     # a modell hibája az eredmény-hívásban jön (pl. 422 rossz bemenet)
            return TaskState(task_id=task_id, state=FAIL, error=str(e)[:300], cost=0.0, raw=raw)
        v = (res or {}).get("video") or {}
        url = v.get("url") if isinstance(v, dict) else None
        return TaskState(task_id=task_id, state=SUCCESS, urls=[url] if url else [], raw=raw)

    async def cancel(self, task_id: str) -> bool:
        app, _, rid = task_id.partition("|")
        try:
            await self._call("PUT", f"{QUEUE}/{app}/requests/{rid}/cancel", ok=(202,))
            return True
        except ProviderError:
            return False

    async def balance(self) -> float:
        raise ProviderError("fal has no balance API")

    async def upload(self, path: Any) -> str:
        """Helyi fájl → a fal CDN-je → URL a feladat bemenetéhez."""
        p = Path(path)
        if p.stat().st_size > MAX_INPUT_MB * 1024 * 1024:
            raise ProviderError(f"fal upload: {p.name} is over {MAX_INPUT_MB} MB — cut it first")
        tok = await self._call("POST", TOKEN_URL, json={}, ok=(200, 201))
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        body = await self._call("POST", CDN_UPLOAD, content=p.read_bytes(), timeout=600, ok=(200, 201),
                                headers={"Authorization": f"{tok['token_type']} {tok['token']}", "Content-Type": ctype,
                                         "X-Fal-File-Name": p.name})
        url = (body or {}).get("access_url")
        if not url:
            raise ProviderError(f"fal upload: no URL in the answer ({str(body)[:200]})")
        return url
