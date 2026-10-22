"""A végleges render becsült ideje (10-23, Kommandant: „a tökéletes eredmény sokáig tarthat, de ki kell írni, kb.
mennyi idő” — elmegy kávézni, boltba, visszajön). Szakaszonként mért átvitelből: másodperc / normalizált másodperc,
ahol a normalizálás a képpont-sebesség (W·H·fps a 1080p30-hoz képest) — egy 4K-s videó ugyanannyi másodperce ~4×
annyi munka.

Alapértékek (10-22/23, i7-11850H, 16 szál, ffmpeg 4.4, 4 időszelet, a 30:21-es HDR iPhone-felvételen mérve):
  munkamásolat   HDR: 360 s a 30 percre (5 perces szeleten 61 s / 300 s)   SDR: becsült — az első mérés kalibrál
  idővonal       120 s 26 rétegre + 195 feliratra (~0,5 s / elem)
  grafika        117 s 124,5 s grafikára (23 darab, 4 egyszerre)
  végső menet    az eredetiből HDR-leképezéssel, rétegekkel, felirattal: 1655 s / 1821 s; SDR-ből 167 s / 300 s
  a régi (teljes) út: a motor kockánként rajzol (2:01 → 325 s köztes fájl) + kódolás
Minden kész munka beolvad (Postgres render_rates, mozgó átlag): a becslés a gép valós sebességéhez igazodik. A kiugró
mérés (a becslés negyede alatt / négyszerese fölött — gyorsítótárból jött munkamásolat, túlterhelt gép) nem számít.

    eta = await render_eta.final(spec)          # {"seconds", "parts": {...}}
    render_eta.text(eta["seconds"])             # "about 35 min"
"""
from __future__ import annotations

import datetime as _dt
import logging
import time
from typing import Any, Optional

log = logging.getLogger("aice.render_eta")

REF = 1920 * 1080 * 30.0
DEFAULTS: dict[tuple[str, str], float] = {
    ("proxy", "hdr"): 360 / 1816, ("proxy", "sdr"): 0.12,
    ("graphics", "any"): 117 / 124.5,
    ("final", "hdr"): 1655 / 1821, ("final", "sdr"): 167 / 300,
    ("full", "any"): 3.2,
}
ITEM_S = 0.5                 # az idővonal építése elemenként (réteg, felirat)
OVERHEAD_S = 15.0            # a projekt felmérése, a változatok, a hang keverése, az összefűzés
PERSISTENT_S, PROBE_S = 30.0, 13.0
_CACHE: dict[str, Any] = {"at": 0.0, "rates": None}


async def rates() -> dict[tuple[str, str], float]:
    """Az alapértékek a Postgresben mért átvitellel felülírva (percenként frissítve)."""
    if _CACHE["rates"] is not None and time.time() - _CACHE["at"] < 60:
        return _CACHE["rates"]
    r = dict(DEFAULTS)
    try:
        from app import db
        r.update(await db.render_rates())
    except Exception as e:                                       # a becslés sose állítja meg a munkát
        log.warning("render_rates nem olvasható: %s", e)
    _CACHE.update(at=time.time(), rates=r)
    return r


def norm(W: float, H: float, fps: float) -> float:
    return max(0.05, float(W) * float(H) * max(1.0, float(fps or 30)) / REF)


def graphics_seconds(spec: dict[str, Any], dur: float) -> float:
    """Ahány másodpercnyi grafikát a motor rajzol (a gyors út szakaszai): a rövid rétegek egyesített ideje, a végig
    látható rétegek mintája, a zárókártya, a kamera mozgásai."""
    story, base, opts = spec.get("story") or {}, spec.get("base") or {}, spec.get("opts") or {}
    spans, persistent = [], False
    for ly in story.get("layers") or []:
        a, d = float(ly.get("at") or 0), float(ly.get("dur") or 0)
        if d >= PERSISTENT_S:
            persistent = True
        elif d > 0:
            spans.append((a, a + d))
    card = base.get("end_card") or {}
    if card:
        spans.append((max(0.0, dur - float(card.get("dur") or 4)), dur))
    cam = opts.get("camera")
    for mv in cam if isinstance(cam, list) else []:
        a = float(mv.get("at") or 0)
        spans.append((a, a + max(0.3, float(mv.get("dur") or 0.6))))
    spans.sort()
    total, cur = 0.0, None
    for a, b in spans:
        if cur and a <= cur[1] + 0.5:
            cur = (cur[0], max(cur[1], b))
            continue
        if cur:
            total += cur[1] - cur[0]
        cur = (a, b)
    if cur:
        total += cur[1] - cur[0]
    return min(dur, total + (PROBE_S if persistent else 0.0))


def key_of(spec: dict[str, Any]) -> str:
    """A végső menet HDR-leképezéssel fut-e (az eredeti HDR, és az eredetiből olvas)."""
    base = spec.get("base") or {}
    return "hdr" if base.get("hdr") and base.get("proxy") else "sdr"


async def final(spec: dict[str, Any], *, fast: bool = True) -> dict[str, Any]:
    """A végleges render becsült ideje a projekt specjéből. fast=False: a régi (teljes) út."""
    base = spec.get("base") or {}
    W, H = float(base.get("width") or 1920), float(base.get("height") or 1080)
    fps, dur = float(base.get("fps") or 30), float(base.get("duration") or 0)
    r, n = await rates(), norm(W, H, fps)
    if not fast:
        secs = OVERHEAD_S + dur * r[("full", "any")] * n
        return {"seconds": round(secs), "parts": {"full": round(secs)}}
    g = graphics_seconds(spec, dur) * r[("graphics", "any")] * n
    f = dur * r[("final", key_of(spec))] * n
    return {"seconds": round(OVERHEAD_S + g + f), "parts": {"graphics": round(g), "final": round(f), "overhead": OVERHEAD_S}}


async def proxy(m: dict[str, Any], length: float) -> float:
    """A munkamásolat becsült ideje (m: mediaprobe.info; length: a vágás utáni hossz)."""
    r = await rates()
    W, H = m.get("display_width") or m.get("width") or 1920, m.get("display_height") or m.get("height") or 1080
    return float(length) * r[("proxy", "hdr" if m.get("hdr") else "sdr")] * norm(W, H, m.get("fps") or 30)


async def observe(stage: str, key: str, seconds: float, units: float) -> None:
    """Egy kész munka mért ideje → a kalibráció (s / normalizált s). Kiugró mérés nem számít."""
    if units <= 0.5 or seconds <= 0:
        return
    try:
        rate = seconds / units
        now = (await rates()).get((stage, key))
        if now and not now / 4 <= rate <= now * 4:
            log.info("render_eta: kiugró mérés kihagyva (%s/%s %.3f, a becslés %.3f)", stage, key, rate, now)
            return
        from app import db
        await db.render_rate_observe(stage, key, rate)
        _CACHE["rates"] = None
    except Exception as e:
        log.warning("render_eta: a mérés nem menthető: %s", e)


def text(seconds: float) -> str:
    s = max(0.0, float(seconds))
    if s < 90:
        return f"about {max(5, int(round(s / 5.0)) * 5)} s"
    m = s / 60
    return f"about {int(round(m))} min" if m < 90 else f"about {m / 60:.1f} h"


def ready_at(seconds: float, start: Optional[float] = None) -> str:
    """Mikorra készül el (ISO 8601, a szerver időzónájával)."""
    t = (start or time.time()) + max(0.0, float(seconds))
    return _dt.datetime.fromtimestamp(t).astimezone().isoformat(timespec="minutes")
