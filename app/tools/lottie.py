"""Lottie-animációk keresése a LottieFiles nyilvános könyvtárában (ingyenes animációk, Lottie Simple License:
kereskedelmi célra is szabad, forrásmegjelölés nem kötelező — mi a záróképen mégis feltüntetjük az alkotót).

    search(query, n)   → jelöltek: név, JSON-link, előnézeti kép, GIF, alkotó, oldal
    fetch(url)         → a Lottie JSON ellenőrizve (fr + layers, méretkorlát, külső képfájl nélkül)
    pick(query, context, aspect, n) → a jelöltek sorrendje: átlátszó háttér, a doboz arányához illő alak, a látómodell
                       rangsora (az előnézetekből); n = a sorrend hányadik eleme (1 = a legjobb) — a választott neve,
                       előnézete és a többi jelölt vele jön (10-19: a „confetti” lila háromszögfoltként ült a szövegen)
"""
from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Optional

import httpx

log = logging.getLogger("aice.lottie")

_API = "https://graphql.lottiefiles.com/2022-08"
_Q = """query($q: String!, $n: Int!) { searchPublicAnimations(query: $q, first: $n) { edges { node {
  id name jsonUrl imageUrl gifUrl url bgColor frameRate createdBy { firstName lastName username } } } } }"""
MAX_BYTES = 3_000_000
_CACHE: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}


def _author(c: dict[str, Any]) -> str:
    who = c.get("createdBy") or {}
    name = " ".join(x for x in (who.get("firstName"), who.get("lastName")) if x).strip()
    return name or (who.get("username") or "").strip("/") or "unknown"


async def search(query: str, n: int = 8) -> list[dict[str, Any]]:
    q = " ".join((query or "").split())[:80]
    if not q:
        raise ValueError("lottie search: say what the animation should show, e.g. \"bicycle\"")
    key = (q.lower(), n)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(_API, json={"query": _Q, "variables": {"q": q, "n": max(1, min(n, 20))}})
        r.raise_for_status()
        edges = (((r.json().get("data") or {}).get("searchPublicAnimations") or {}).get("edges")) or []
    out = []
    for e in edges:
        a = e.get("node") or {}
        if not a.get("jsonUrl"):
            continue
        author = _author(a)
        out.append({"name": a.get("name") or "animation", "json_url": a["jsonUrl"], "thumb_url": a.get("imageUrl"),
                    "gif_url": a.get("gifUrl"), "page": a.get("url"), "author": author, "background": a.get("bgColor"),
                    "credit": f"Animation “{a.get('name') or 'untitled'}” by {author} (LottieFiles)"})
    _CACHE[key] = (time.time(), out)
    return out


def check(data: Any) -> dict[str, Any]:
    """Betölthető Lottie? (fr + layers; képfájlra csak beágyazva — data: URI — hivatkozhat)."""
    if not isinstance(data, dict) or "layers" not in data or not data.get("fr"):
        raise ValueError("not a Lottie animation (JSON with fr and layers)")
    for a in data.get("assets") or []:
        if isinstance(a, dict) and a.get("p") and not str(a.get("p")).startswith("data:") and a.get("e") != 1:
            raise ValueError("it loads pictures from separate files")
    return data


async def fetch(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        async with c.stream("GET", url) as r:
            r.raise_for_status()
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > MAX_BYTES:
                    raise ValueError(f"the animation is larger than {MAX_BYTES // 1_000_000} MB")
    return check(json.loads(bytes(buf).decode("utf-8")))


def _static(v: Any) -> Any:
    return v.get("k") if isinstance(v, dict) and not v.get("a") else None


def opaque_background(data: dict[str, Any]) -> bool:
    """Teli háttere van-e: egy szilárd réteg (solid) vagy egy kitöltött téglalap, amely a vászon ~egészét fedi — az ilyen
    animáció dobozként takarja, ami alatta van."""
    W, H = float(data.get("w") or 0), float(data.get("h") or 0)
    if not W or not H:
        return False
    big = lambda w, h: float(w or 0) >= 0.9 * W and float(h or 0) >= 0.9 * H        # noqa: E731

    def filled_rect(items: list[Any]) -> bool:
        if not any(isinstance(it, dict) and it.get("ty") == "fl" for it in items):
            return any(isinstance(it, dict) and it.get("ty") == "gr" and filled_rect(it.get("it") or []) for it in items)
        for it in items:
            sz = _static(it.get("s")) if isinstance(it, dict) and it.get("ty") == "rc" else None
            if isinstance(sz, list) and len(sz) >= 2 and big(sz[0], sz[1]):
                return True
        return any(isinstance(it, dict) and it.get("ty") == "gr" and filled_rect(it.get("it") or []) for it in items)
    for ly in data.get("layers") or []:
        if not isinstance(ly, dict):
            continue
        if ly.get("ty") == 1 and big(ly.get("sw"), ly.get("sh")):
            return True
        if ly.get("ty") == 4 and filled_rect(ly.get("shapes") or []):
            return True
    return False


def _shape(data: dict[str, Any]) -> tuple[float, bool]:
    w, h = float(data.get("w") or 1), float(data.get("h") or 1)
    return w / max(h, 1.0), not opaque_background(data)


async def pick(query: str, context: str = "", aspect: Optional[float] = None, n: Optional[int] = None,
               tries: int = 5) -> tuple[dict[str, Any], dict[str, Any]]:
    """→ (a Lottie JSON, a választott jelölt + "pick": a sorszáma, "of", "transparent", "aspect", "alternatives").
    Sorrend: az átlátszó hátterű előre, aztán az, amelyiknek az alakja a doboz arányához (aspect = szélesség / magasság)
    illik, aztán a látómodell rangsora (az előnézetekből; nem szűr). n: a sorrend n-edik eleme (1-től) — a kliens így
    választhat másikat, ha az első nem tetszik."""
    from app.tools.images import rank as _rank
    cands = (await search(query, n=8))[:6]
    if not cands:
        raise ValueError(f"no animation found for “{query}”")
    prompt = (f'A scene of a short video: "{context or query}". It should show an animated illustration of: "{query}". '
              "Rank the numbered animation previews by how well they fit: a clear, simple, on-topic illustration first; "
              "cluttered, off-topic or text-heavy ones last. Return JSON only: {\"ranking\": [numbers, best first]}")
    order = await _rank(query, context, cands, prompt=prompt) if len(cands) > 1 else [0]
    order = list(dict.fromkeys(list(order) + list(range(len(cands)))))
    loaded: list[tuple[int, int, dict[str, Any], float, bool]] = []
    last: Exception = ValueError("nothing loaded")
    for pos, k in enumerate(order[:max(1, tries)]):
        try:
            data = await fetch(cands[k]["json_url"])
        except Exception as e:
            last = e
            log.info("lottie-jelölt kimarad (%s): %s", cands[k]["name"], e)
            continue
        asp, clear = _shape(data)
        loaded.append((pos, k, data, asp, clear))
    if not loaded:
        raise ValueError(f"no usable animation for “{query}”: {last}")
    def key(x: tuple[int, int, dict[str, Any], float, bool]) -> tuple[int, int, int]:
        miss = abs(math.log(x[3] / aspect)) if aspect else 0.0
        return (0 if x[4] else 1, 0 if miss < 0.2 else 1 if miss < 0.6 else 2, x[0])
    loaded.sort(key=key)
    i = max(1, min(int(n or 1), len(loaded))) - 1
    _, k, data, asp, clear = loaded[i]
    alts = [{"pick": j + 1, "name": cands[x[1]]["name"], "thumb_url": cands[x[1]].get("thumb_url"), "transparent": x[4]}
            for j, x in enumerate(loaded) if j != i]
    return data, {**cands[k], "pick": i + 1, "of": len(loaded), "transparent": clear,
                  "aspect": f"{int(data.get('w') or 0)}×{int(data.get('h') or 0)}", "alternatives": alts,
                  **({"pick_asked": int(n)} if n and int(n) != i + 1 else {})}
