"""Ingyenes stock VIDEÓ — valódi felvétel bejátszásnak (Kommandant 10-21: „tudunk ingyenes videobejátszásokat is —
valódi képpel?”), a Pexels-ről.

Licenc (pexels.com/license, ellenőrizve 2026-10-22): szabadon felhasználható és szerkeszthető, reklámban és közösségi
médiában is; feltüntetés nem kötelező. TILOS: felismerhető embert rossz színben vagy sértően bemutatni, módosítatlanul
eladni, más stock- vagy háttérkép-oldalon újraterjeszteni, ajánlásnak beállítani, védjegyként használni.
API-szabály (pexels.com/api/documentation): a találatoknál látszódjon a Pexels (link), a készítőt lehetőleg nevesítsük —
nálunk mindig: a jelöltek mellett ATTRIBUTION, a fájl adataiban és a videó stáblistájában „Video: Név / Pexels”.
Kulcs: PEXELS_API_KEY (env). Keret: 200 kérés / óra, 20 000 / hó → a keresés egy óráig gyorsítótárazva.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Optional

import httpx

log = logging.getLogger("aice.stock_video")

API = "https://api.pexels.com/videos"
ATTRIBUTION = "Videos provided by Pexels — https://www.pexels.com"
ORIENTATIONS = ("landscape", "portrait", "square")
_TTL_S = 3600
_cache: dict[tuple, tuple[float, Any]] = {}


class StockVideoError(ValueError):
    """Érthető hiba (nincs kulcs, nincs találat, letöltési hiba)."""


def configured() -> bool:
    return bool(os.getenv("PEXELS_API_KEY"))


def _headers() -> dict[str, str]:
    key = os.getenv("PEXELS_API_KEY", "")
    if not key:
        raise StockVideoError("Stock video isn't set up on this server (PEXELS_API_KEY).")
    return {"Authorization": key}


async def _get(path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    ck = (path, tuple(sorted((params or {}).items())))
    hit = _cache.get(ck)
    if hit and time.monotonic() - hit[0] < _TTL_S:
        return hit[1]
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{API}/{path}", params=params, headers=_headers())
    if r.status_code == 429:
        raise StockVideoError("The stock video search is busy (Pexels rate limit) — try again in a few minutes.")
    if r.status_code == 404:
        raise StockVideoError("No such stock video on Pexels.")
    r.raise_for_status()
    data = r.json()
    _cache[ck] = (time.monotonic(), data)
    if len(_cache) > 500:                                   # a régieket ki
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
            _cache.pop(k, None)
    return data


def _title(v: dict[str, Any]) -> str:
    """A Pexels-oldal címéből (…/video/<szavak>-<id>/) olvasható cím."""
    m = re.search(r"/video/(.+?)-\d+/?$", v.get("url") or "")
    return (m.group(1).replace("-", " ").strip().capitalize() if m else "") or f"Pexels video {v.get('id')}"


def candidate(v: dict[str, Any]) -> dict[str, Any]:
    user = v.get("user") or {}
    return {"stock_video": f"pexels:{v['id']}", "title": _title(v), "thumb_url": v.get("image"), "page_url": v.get("url"),
            "duration": v.get("duration"), "width": v.get("width"), "height": v.get("height"), "source": "Pexels",
            "author": user.get("name"), "author_url": user.get("url"),
            "credit": f"Video: {user.get('name') or 'unknown'} / Pexels", "license": "Pexels License (pexels.com/license)"}


async def search(query: str, n: int = 12, orientation: Optional[str] = None, min_seconds: float = 0) -> list[dict[str, Any]]:
    """Videó-jelöltek szavakra (a Pexels saját keresője), a legalább min_seconds hosszúak."""
    q = " ".join(str(query or "").split())[:100]
    if not q:
        raise StockVideoError("Give words to search stock video for, e.g. \"warship at sea\".")
    params: dict[str, Any] = {"query": q, "per_page": max(1, min(40, int(n) + 6))}
    if orientation in ORIENTATIONS:
        params["orientation"] = orientation
    vids = (await _get("search", params)).get("videos") or []
    out = [candidate(v) for v in vids if (v.get("duration") or 0) >= min_seconds and v.get("video_files")]
    return out[:max(1, min(40, int(n)))]


def _pick_file(files: list[dict[str, Any]], short: int) -> Optional[dict[str, Any]]:
    """A legkisebb mp4, amelynek a rövidebb oldala legalább `short` px; ha nincs ilyen, a legnagyobb."""
    mp4 = [f for f in files if (f.get("file_type") or "").endswith("mp4") and f.get("width") and f.get("height") and f.get("link")]
    if not mp4:
        return None
    big = sorted((f for f in mp4 if min(f["width"], f["height"]) >= short), key=lambda f: f["width"] * f["height"])
    return big[0] if big else max(mp4, key=lambda f: f["width"] * f["height"])


def _ref_id(ref: Any) -> Optional[str]:
    s = ref.get("stock_video") if isinstance(ref, dict) else ref
    s = str(s or "")
    m = re.match(r"^(?:pexels:)?(\d{3,12})$", s) or re.search(r"pexels\.com/(?:[\w-]+/)*video(?:-files)?/(?:[\w-]*-)?(\d{3,12})", s)
    return m.group(1) if m else None


async def fetch(ref: Any, *, short: int = 1080, orientation: Optional[str] = None) -> tuple[str, str]:
    """Egy stock videó a felhasználó munkaterületére → (file_id, kredit). ref: "pexels:<id>" | <id> | Pexels-link |
    {"stock_video": "szavak"} (az első megfelelő találat az irány szerint). A felbontás: a rövidebb oldal ≥ short, ha van."""
    vid = _ref_id(ref)
    if vid is None:
        words = ref.get("stock_video") if isinstance(ref, dict) else ref
        found = await search(str(words or ""), 6, orientation, min_seconds=3)
        if not found:
            raise StockVideoError(f"No stock video found for “{words}” — try other words.")
        vid = found[0]["stock_video"].split(":", 1)[1]
    v = await _get(f"videos/{vid}")
    f = _pick_file(v.get("video_files") or [], short)
    if not f:
        raise StockVideoError(f"Pexels video {vid} has no downloadable mp4.")
    c = candidate(v)
    from app.storage import input_path, new_file_id, write_meta
    fid = new_file_id("mp4")
    dst = input_path(fid)
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as cl:
        async with cl.stream("GET", f["link"]) as r:
            r.raise_for_status()
            with open(dst, "wb") as fh:
                async for chunk in r.aiter_bytes(1 << 20):
                    fh.write(chunk)
    write_meta(fid, original_filename=f"{c['title'][:60]}.mp4", label=f"{c['title'][:60]} (Pexels).mp4", operation="stock video",
               extra={"credit": c["credit"], "license": c["license"], "source_url": c["page_url"], "stock_video": c["stock_video"],
                      "width": f["width"], "height": f["height"]})
    log.info("stock videó: pexels %s → %s (%dx%d)", vid, fid, f["width"], f["height"])
    return fid, c["credit"]
