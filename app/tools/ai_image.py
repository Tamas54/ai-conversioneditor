"""AI-képek a kie.ai-n (Kommandant 10-21: „a képgenerátort is a kie-től vegyük … nem akarom keverni”) — ugyanazok a
modellek, mint eddig a SiliconFlow-n: FLUX.2 [pro] a generáláshoz, FLUX.1 Kontext [pro] a szerkesztéshez (retus,
helyreállítás, színezés, szövegtörlés).

Ár: a kie listaára a közös képlettel (credits.list_price — ugyanaz a padló, mint az AI-videónál). A könyvben foglalás →
elszámolás a TÉNYLEGES kie-költséggel (provider_cost), hibánál a foglalás visszajár: így a napi egyeztetés a képeket is
látja, és az admin használata is naplózva van. A képek az ingyenes kreditből is kérhetők (a videó nem: credits.PAID_ONLY_OPS).
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import httpx

from app import credits, db, tenancy
from app.providers.base import FAIL, SUCCESS, ProviderError, TaskState
from app.providers.kie import image_cost

log = logging.getLogger("aice.ai_image")

GEN_MODEL = "flux-2/pro-text-to-image"
EDIT_MODEL = "flux1-kontext"
GEN_ASPECTS = {"1:1": 1.0, "4:3": 4 / 3, "3:4": 3 / 4, "16:9": 16 / 9, "9:16": 9 / 16, "3:2": 1.5, "2:3": 2 / 3}
TASK_TIMEOUT_S = 300
POLL_S = 3.0


def _wh(size: str) -> tuple[int, int]:
    try:
        w, h = (int(v) for v in size.lower().split("x"))
    except ValueError:
        raise ValueError(f"size must be WIDTHxHEIGHT (e.g. 1792x1024), not {size!r}") from None
    if not (256 <= w <= 2048 and 256 <= h <= 2048):
        raise ValueError(f"size: 256–2048 px per side ({size})")
    return w, h


def gen_tier(size: str) -> str:
    """1K ≈ 1 MP, 2K ≈ 4 MP: a kért méretnél kisebbet nem adunk (1792×1024 → 2K)."""
    w, h = _wh(size)
    return "1K" if w * h <= 1_050_000 else "2K"


def gen_price(size: str = "1024x1024") -> int:
    return credits.list_price("kie", image_cost(GEN_MODEL, gen_tier(size)))


def picture_price() -> int:
    """Egy keretbe (dia, videójelenet) kért kép ára — ezek nem négyzetesek, tehát 2K-sok: ennyit mutatunk és ellenőrzünk előre."""
    return gen_price("1792x1024")


def edit_price() -> int:
    return credits.list_price("kie", image_cost(EDIT_MODEL, "pro"))


def configured() -> bool:
    from app import providers
    return providers.configured("kie")


def _kie():
    from app.providers.kie import KieProvider
    if not configured():
        raise RuntimeError("AI pictures aren't set up on this server (KIE_API_KEY).")
    return KieProvider()


class Bill:
    """Egy fizetős képművelet kie-feladatai: a tényleges költség (kie-kredit) és a feladat-azonosítók a könyvhöz."""

    def __init__(self) -> None:
        self.cost, self.tasks = 0.0, []

    def add(self, st: TaskState) -> None:
        self.cost += float(st.cost or 0)
        self.tasks.append(st.task_id)


@asynccontextmanager
async def paid(operation: str, price: int, ref: str):
    """A kép ára előre foglalva (fedezet nélkül InsufficientCredits — semmi nem indul), sikerre levonva a tényleges
    kie-költséggel a könyvben, hibára visszaadva. Vendég: nincs fiók → InsufficientCredits (mint eddig)."""
    uid = tenancy.current_user_id()
    if tenancy.is_guest(uid):
        raise db.InsufficientCredits(needed=price, available=0)
    op, bill = f"img-{uuid.uuid4().hex[:16]}", Bill()
    on = db.enabled()
    held = (await credits.hold(uid, price, op, operation=operation, provider="kie", meta={"ref": ref[:80]}))["held"] if on else 0
    try:
        yield bill
    except BaseException:
        if on:
            await asyncio.shield(credits.release(uid, op, held, operation=operation, provider="kie",
                                                 provider_task_id=",".join(bill.tasks) or None, provider_cost=bill.cost))
        raise
    if on:
        await credits.settle(uid, op, held, price, operation=operation, provider="kie",
                             provider_task_id=",".join(bill.tasks) or None, provider_cost=bill.cost,
                             meta={"ref": ref[:80], "tasks": len(bill.tasks)})
    if credits.list_price("kie", bill.cost) > price:          # a kie drágult: a listaár (IMAGE_PRICES) frissítendő
        await credits.notice("image_price_below_cost", f"kie charged {bill.cost:g} credits for {operation}, more than its "
                                                       f"list price covers ({price} credits) — update IMAGE_PRICES in "
                                                       "app/providers/kie.py.")


async def _run(model: str, inp: dict[str, Any], bill: Optional[Bill]) -> bytes:
    """Egy kie-feladat kivárva (a kép ~10–60 s) → a kép bájtjai. Hiba / időtúllépés → RuntimeError."""
    prov = _kie()
    tid = await prov.create(model, inp)
    deadline = time.monotonic() + TASK_TIMEOUT_S
    while True:
        await asyncio.sleep(POLL_S)
        try:
            st = await prov.status(tid)
        except ProviderError as e:
            if time.monotonic() > deadline:
                raise RuntimeError(f"The AI picture service did not answer: {e}") from None
            continue
        if st.state in (SUCCESS, FAIL):
            if bill is not None:
                bill.add(st)
            if st.state == FAIL or not st.urls:
                raise RuntimeError(f"The AI picture service failed: {st.error or 'no picture came back'}")
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
                r = await c.get(st.urls[0])
                r.raise_for_status()
                return r.content
        if time.monotonic() > deadline:
            raise RuntimeError(f"The AI picture service gave no result in {TASK_TIMEOUT_S} s (task {tid}).")


async def generate(prompt: str, size: str = "1024x1024", bill: Optional[Bill] = None) -> bytes:
    """FLUX.2 [pro] szöveg→kép: a méret aránya a legközelebbi támogatott arány, a szint a méretből (gen_tier)."""
    w, h = _wh(size)
    aspect = min(GEN_ASPECTS, key=lambda a: abs(GEN_ASPECTS[a] - w / h))
    return await _run(GEN_MODEL, {"prompt": prompt, "aspect": aspect, "resolution": gen_tier(size), "nsfw_checker": True},
                      bill)


def _png_for_model(src: Path) -> bytes:
    """≤ 1600 px PNG (a modell úgyis ~1 MP-en dolgozik; a nagy szken csak lassít)."""
    from PIL import Image
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((1600, 1600))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()


async def kontext(src: Path, prompt: str, bill: Optional[Bill] = None) -> bytes:
    """FLUX.1 Kontext [pro] szerkesztés: a kép a kie feltöltő hostjára (24 óráig él), az arány az eredetié marad."""
    from app.storage import temp_path
    tmp = temp_path(f"kontext_{uuid.uuid4().hex[:10]}.png")
    tmp.write_bytes(await asyncio.to_thread(_png_for_model, src))
    try:
        url = await _kie().upload(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    return await _run(EDIT_MODEL, {"prompt": prompt, "image": url, "kontext_model": "flux-kontext-pro", "format": "png"}, bill)
