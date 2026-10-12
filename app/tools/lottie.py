"""Lottie-animációk keresése a LottieFiles nyilvános könyvtárában (ingyenes animációk, Lottie Simple License:
kereskedelmi célra is szabad, forrásmegjelölés nem kötelező — mi a záróképen mégis feltüntetjük az alkotót).

    search(query, n)   → jelöltek: név, JSON-link, előnézeti kép, GIF, alkotó, oldal
    fetch(url)         → a Lottie JSON ellenőrizve (fr + layers, méretkorlát, külső képfájl nélkül)
    pick(query, context) → a látómodell választ a jelöltek előnézeteiből (videó: _rank), az első betölthető nyer
"""
from __future__ import annotations

import json
import logging
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


async def pick(query: str, context: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    """→ (a Lottie JSON, a választott jelölt). A látómodell az előnézetekből rangsorol (nem szűr), az első betölthető nyer."""
    from app.tools.video import _rank
    cands = (await search(query, n=8))[:6]
    if not cands:
        raise ValueError(f"no animation found for “{query}”")
    prompt = (f'A scene of a short video: "{context or query}". It should show an animated illustration of: "{query}". '
              "Rank the numbered animation previews by how well they fit: a clear, simple, on-topic illustration first; "
              "cluttered, off-topic or text-heavy ones last. Return JSON only: {\"ranking\": [numbers, best first]}")
    order = await _rank(query, context, cands, prompt=prompt) if len(cands) > 1 else [0]
    last: Exception = ValueError("nothing loaded")
    for k in order:
        try:
            return await fetch(cands[k]["json_url"]), cands[k]
        except Exception as e:
            last = e
            log.info("lottie-jelölt kimarad (%s): %s", cands[k]["name"], e)
    raise ValueError(f"no usable animation for “{query}”: {last}")
