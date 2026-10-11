"""Képforrások egy helyen (PPTX-pakli, dizájnsablonok): file_id, URL, FLUX-generálás, stockfotó.

Egy képleírás (spec) lehet:
    "<file_id>"                          meglévő feltöltés/kimenet
    {"file_id": "..."}                   ugyanaz
    {"url": "https://..."}               letöltjük
    {"generate": "prompt", "size"?: …}   FLUX (generate_image) — a méret a célkeret arányából jön
    {"stock": "keresőszó"}               Openverse (CC, kereskedelmi célra is; kulcs nélkül), forrásmegjelöléssel

Kimenet: a célkeret arányára középre vágott, max. ~1920 px-es JPEG ideiglenes fájlban + forrásmegjelölés.
"""
from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

import httpx

from app.storage import temp_path
from app.storage.files import new_file_id
from app.tools.crafts import existing

log = logging.getLogger("aice.images")

ImageSpec = Union[str, dict[str, Any]]
# A Wikimedia kapcsolatot kér a User-Agentben (különben 429-cel fojt): https://meta.wikimedia.org/wiki/User-Agent_policy
_UA = "ai-conversioneditor/1.0 (+https://github.com/Tamas54/ai-conversioneditor; document builder)"
_MAX_DOWNLOAD = 30 * 1024 * 1024


@dataclass
class ResolvedImage:
    path: Path                     # vágott, tömörített JPEG (ideiglenes; a hívó törli)
    attribution: Optional[str]     # stocknál kötelező forrásmegjelölés
    source_file_id: Optional[str]  # generált/feltöltött kép file_id-je, ha van


def _size_for(aspect: Optional[float]) -> str:
    if aspect is None or 0.85 <= aspect <= 1.18:
        return "1024x1024"
    if aspect > 1.18:
        return "1792x1024" if aspect >= 1.6 else "1536x1152"
    return "1024x1792" if aspect <= 0.62 else "1152x1536"


async def _get(url: str, **kw) -> httpx.Response:
    """GET egy újrapróbálással: átmeneti DNS-/kapcsolati hiba ne döntse el a teljes paklit."""
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers={"User-Agent": _UA}) as c:
                r = await c.get(url, **kw)
                r.raise_for_status()
                return r
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
            if attempt == 2:
                raise
            await asyncio.sleep(1.5)
    raise RuntimeError("unreachable")


async def _download(url: str) -> bytes:
    r = await _get(url)
    if len(r.content) > _MAX_DOWNLOAD:
        raise ValueError(f"kép túl nagy: {url}")
    return r.content


def _wikimedia_thumb(url: str, width: int) -> str:
    """upload.wikimedia.org eredeti → gyorsítótárazott bélyegkép (az eredetiket a Wikimedia fojtja)."""
    pre = "https://upload.wikimedia.org/wikipedia/commons/"
    if not url.startswith(pre) or "/thumb/" in url:
        return url
    rest = url[len(pre):]                      # "a/ab/Fájl.jpg"
    name = rest.rsplit("/", 1)[-1]
    if not name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        return url
    return f"{pre}thumb/{rest}/{width}px-{name}"


async def _commons(query: str, aspect: Optional[float]) -> tuple[bytes, str]:
    """Wikimedia Commons keresés (kulcs nélkül): helyekre/tárgyakra jóval relevánsabb, mint az Openverse
    fotószűrője; 1600 px-es bélyegkép + licenc/szerző az extmetadatából."""
    import html as _html
    import re as _re
    r = await _get("https://commons.wikimedia.org/w/api.php", params={
        "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6, "gsrlimit": 15,
        "gsrsearch": f"{query} filetype:bitmap", "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1600})
    pages = sorted((r.json().get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
    for pg in pages:
        ii = (pg.get("imageinfo") or [{}])[0]
        w, h = ii.get("width") or 0, ii.get("height") or 1
        if ii.get("mime") != "image/jpeg" or w < 1200:
            continue
        if aspect and aspect > 1.18 and w / h < 1.1:      # fekvő keretbe ne tegyünk álló fotót
            continue
        meta = ii.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value", "szabad licenc")
        who = _re.sub(r"<[^>]+>", "", _html.unescape((meta.get("Artist") or {}).get("value", ""))).strip() or "ismeretlen szerző"
        try:
            data = await _download(ii.get("thumburl") or ii["url"])
        except Exception as e:
            log.info("commons letöltés kihagyva (%s): %s", ii.get("url"), e)
            continue
        return data, f"Fotó: {who[:80]} (Wikimedia Commons), {lic} — {ii.get('descriptionurl', ii['url'])}"
    raise ValueError(f"nincs Commons-találat: {query!r}")


async def _stock_progressive(query: str, aspect: Optional[float]) -> tuple[bytes, str]:
    """Túl részletes keresőkifejezésre (ügynökök szeretik) nincs találat — egyre rövidebb változattal
    újrapróbál (az utolsó szavak a legkevésbé lényegesek), és csak a végén hibázik."""
    words = query.split()
    tries = [query] + [" ".join(words[:n]) for n in (4, 3, 2) if len(words) > n]
    last: Exception = ValueError(f"nincs stockkép: {query!r}")
    for q in dict.fromkeys(tries):
        try:
            return await _stock(q, aspect)
        except ValueError as e:
            last = e
    raise last


async def _stock(query: str, aspect: Optional[float]) -> tuple[bytes, str]:
    try:
        return await _commons(query, aspect)
    except Exception as e:
        log.info("Commons nem adott képet (%s), Openverse jön: %s", query, e)
        return await _openverse(query, aspect)


async def _openverse(query: str, aspect: Optional[float]) -> tuple[bytes, str]:
    params = {"q": query, "license_type": "commercial", "page_size": 20, "size": "large", "category": "photograph"}
    if aspect and aspect > 1.18:
        params["aspect_ratio"] = "wide"
    elif aspect and aspect < 0.85:
        params["aspect_ratio"] = "tall"
    r = await _get("https://api.openverse.org/v1/images/", params=params)
    results = [x for x in r.json().get("results", []) if (x.get("width") or 0) >= 1000]
    if not results:
        raise ValueError(f"nincs stockkép erre: {query!r}")
    for hit in results[:5]:
        try:
            data = await _download(_wikimedia_thumb(hit["url"], 1600))
        except Exception as e:  # néhány forrás letilt/lejárt — a következő
            log.info("stock letöltés kihagyva (%s): %s", hit.get("url"), e)
            continue
        lic = f"CC {hit.get('license', '').upper()} {hit.get('license_version') or ''}".strip()
        who = hit.get("creator") or "ismeretlen szerző"
        return data, f"Fotó: {who} ({hit.get('source')}), {lic} — {hit.get('foreign_landing_url') or hit['url']}"
    raise ValueError(f"egyik stockkép sem tölthető le: {query!r}")


def _crop_to(data_or_path: Union[bytes, Path], aspect: Optional[float], max_px: int) -> Path:
    from PIL import Image, ImageOps
    src = Image.open(io.BytesIO(data_or_path) if isinstance(data_or_path, bytes) else data_or_path)
    im = ImageOps.exif_transpose(src).convert("RGB")
    if aspect:
        w, h = im.size
        if w / h > aspect:            # túl széles → oldalt vágunk
            nw = int(h * aspect); x = (w - nw) // 2; im = im.crop((x, 0, x + nw, h))
        else:                         # túl magas → fent/lent vágunk (kicsit feljebb: arcok, horizont)
            nh = int(w / aspect); y = int((h - nh) * 0.4); im = im.crop((0, y, w, y + nh))
    if max(im.size) > max_px:
        im.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    out = temp_path(new_file_id("jpg"))
    im.save(out, "JPEG", quality=86, optimize=True, progressive=True)
    return out


async def resolve(spec: ImageSpec, *, aspect: Optional[float] = None, max_px: int = 1920) -> ResolvedImage:
    if isinstance(spec, str):
        spec = {"file_id": spec}
    if spec.get("file_id"):
        p = existing(spec["file_id"])
        return ResolvedImage(await asyncio.to_thread(_crop_to, p, aspect, max_px), spec.get("credit"), spec["file_id"])
    if spec.get("url"):
        data = await _download(spec["url"])
        return ResolvedImage(await asyncio.to_thread(_crop_to, data, aspect, max_px), spec.get("credit"), None)
    if spec.get("generate"):
        from app.tools import vision_ops
        g = await vision_ops.generate_image(spec["generate"], size=spec.get("size") or _size_for(aspect))
        p = existing(g["image_file_id"])
        return ResolvedImage(await asyncio.to_thread(_crop_to, p, aspect, max_px), None, g["image_file_id"])
    if spec.get("stock"):
        data, credit = await _stock_progressive(spec["stock"], aspect)
        return ResolvedImage(await asyncio.to_thread(_crop_to, data, aspect, max_px), credit, None)
    raise ValueError(f"ismeretlen képforrás: {spec!r} (file_id | url | generate | stock)")


async def resolve_many(items: list[tuple[ImageSpec, Optional[float]]], concurrency: int = 4) -> list[ResolvedImage]:
    """Sok kép párhuzamosan (FLUX ~10 s/kép) — a sorrend megmarad."""
    sem = asyncio.Semaphore(concurrency)

    async def one(spec, aspect):
        async with sem:
            return await resolve(spec, aspect=aspect)
    return await asyncio.gather(*(one(s, a) for s, a in items))
