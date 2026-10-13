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
import base64
import io
import json
import logging
import math
import re
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
        raise ValueError(f"Image too large: {url}")
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
        who = _re.sub(r"<[^>]+>", "", _html.unescape((meta.get("Artist") or {}).get("value", ""))).strip() or "unknown author"
        try:
            data = await _download(ii.get("thumburl") or ii["url"])
        except Exception as e:
            log.info("commons letöltés kihagyva (%s): %s", ii.get("url"), e)
            continue
        return data, f"Photo: {who[:80]} (Wikimedia Commons), {lic} — {ii.get('descriptionurl', ii['url'])}"
    raise ValueError(f"No Wikimedia Commons result for {query!r}")


async def _stock_progressive(query: str, aspect: Optional[float]) -> tuple[bytes, str]:
    """Túl részletes keresőkifejezésre (ügynökök szeretik) nincs találat — egyre rövidebb változattal
    újrapróbál, és csak a végén hibázik. Angolban a fő szó többnyire a VÉGÉN áll („shiny blonde girl” → girl),
    ezért előbb a záró szavak maradnak (a jelzők esnek ki), csak utána az elejük (pl. „Parliament building at night”)."""
    words = query.split()
    tries = ([query] + [" ".join(words[-n:]) for n in (3, 2) if len(words) > n]
             + [" ".join(words[:n]) for n in (4, 3, 2) if len(words) > n])
    last: Exception = ValueError(f"No stock photo for {query!r}")
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
        raise ValueError(f"No stock photo for {query!r}")
    for hit in results[:5]:
        try:
            data = await _download(_wikimedia_thumb(hit["url"], 1600))
        except Exception as e:  # néhány forrás letilt/lejárt — a következő
            log.info("stock letöltés kihagyva (%s): %s", hit.get("url"), e)
            continue
        lic = f"CC {hit.get('license', '').upper()} {hit.get('license_version') or ''}".strip()
        who = hit.get("creator") or "unknown author"
        return data, f"Photo: {who} ({hit.get('source')}), {lic} — {hit.get('foreign_landing_url') or hit['url']}"
    raise ValueError(f"None of the stock photos for {query!r} could be downloaded")


# ---------- jelöltek a képválasztóhoz (a felhasználó választ; tartalmi szűrés NINCS — Kommandant, 10-12) ----------

def _commons_credit(ii: dict[str, Any]) -> str:
    import html as _html
    import re as _re
    meta = ii.get("extmetadata") or {}
    lic = (meta.get("LicenseShortName") or {}).get("value", "free licence")
    who = _re.sub(r"<[^>]+>", "", _html.unescape((meta.get("Artist") or {}).get("value", ""))).strip() or "unknown author"
    return f"Photo: {who[:80]} (Wikimedia Commons), {lic} — {ii.get('descriptionurl', ii.get('url', ''))}"


async def _commons_candidates(query: str, n: int) -> list[dict[str, Any]]:
    r = await _get("https://commons.wikimedia.org/w/api.php", params={
        "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6, "gsrlimit": min(50, n * 2),
        "gsrsearch": f"{query} filetype:bitmap", "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1600})
    pages = sorted((r.json().get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
    out = []
    for pg in pages:
        ii = (pg.get("imageinfo") or [{}])[0]
        if not ii.get("url") or not str(ii.get("mime", "")).startswith("image/"):
            continue                                   # csak technikai: legyen letölthető kép
        full, orig = ii.get("thumburl") or ii["url"], ii["url"].split("?")[0]
        # a Wikimedia csak szabványos bélyegkép-szélességeket ad (pl. 250/330/500; a 400 → HTTP 400)
        out.append({"thumb_url": _wikimedia_thumb(orig, 330) if (ii.get("width") or 0) > 330 else orig,
                    "url": full, "credit": _commons_credit(ii), "title": (pg.get("title") or "").removeprefix("File:"),
                    "width": ii.get("width"), "height": ii.get("height"), "source": "Wikimedia Commons"})
    return out[:n]


async def _openverse_candidates(query: str, n: int) -> list[dict[str, Any]]:
    r = await _get("https://api.openverse.org/v1/images/", params={"q": query, "license_type": "commercial",
                                                                  "page_size": min(40, max(n, 10)), "size": "large"})
    out = []
    for hit in r.json().get("results", []):
        if not hit.get("url"):
            continue
        lic = f"CC {hit.get('license', '').upper()} {hit.get('license_version') or ''}".strip()
        out.append({"thumb_url": hit.get("thumbnail") or hit["url"], "url": _wikimedia_thumb(hit["url"].split("?")[0], 1600),
                    "credit": f"Photo: {hit.get('creator') or 'unknown author'} ({hit.get('source')}), {lic} — "
                              f"{hit.get('foreign_landing_url') or hit['url']}",
                    "title": hit.get("title") or "", "width": hit.get("width"), "height": hit.get("height"),
                    "source": hit.get("source") or "Openverse"})
    return out[:n]


async def search(query: str, n: int = 12) -> list[dict[str, Any]]:
    """Stock-jelöltek a választóhoz, letöltés nélkül: [{thumb_url, url, credit, title, width, height, source}].
    Commons + Openverse; kevés találatnál a rövidített keresés (a fő szó a végén, ld. _stock_progressive) tölti fel."""
    query = " ".join(query.split())[:120]
    if not query:
        return []
    words = query.split()
    tries = list(dict.fromkeys([query] + [" ".join(words[-k:]) for k in (3, 2) if len(words) > k]
                               + [" ".join(words[:k]) for k in (4, 3, 2) if len(words) > k]))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for q in tries:
        for fn in (_commons_candidates, _openverse_candidates):
            try:
                found = await fn(q, n)
            except Exception as e:
                log.info("képjelöltek (%s, %s): %s", fn.__name__, q, e)
                continue
            for c in found:
                if c["url"] not in seen:
                    seen.add(c["url"]); out.append(c)
            if len(out) >= n:
                return out[:n]
        if len(out) >= max(4, n // 2):
            break
    return out[:n]


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
    raise ValueError(f"Unknown image source: {spec!r} (file_id | url | generate | stock)")


async def resolve_many(items: list[tuple[ImageSpec, Optional[float]]], concurrency: int = 4) -> list[ResolvedImage]:
    """Sok kép párhuzamosan (FLUX ~10 s/kép) — a sorrend megmarad."""
    sem = asyncio.Semaphore(concurrency)

    async def one(spec, aspect):
        async with sem:
            return await resolve(spec, aspect=aspect)
    return await asyncio.gather(*(one(s, a) for s, a in items))


# ---------- a stock-jelöltek látómodell-rangsora (videó, pakli, Lottie) ----------
# A kereső a FÁJLNÉVBEN keres, és elvont témánál mellé nyúl (10-12: „oil barrels” → vasúti fordítókorong; 10-13, pakli:
# „data analysis transparency” → genomikai ábra, „magnifying glass documents” → Deutsche Post-bélyeg, „report dashboard”
# → „Lorem Ipsum … designed by freepik” mockup). A látómodell SORRENDET ad, semmit nem dob ki (a felhasználó bármelyik
# jelöltet választhatja a képválasztóban) — csak az automatikus alapválasztás lesz a legjobban illő.

PICK_N = 6


def judge_prompt(query: str, context: str, medium: str = "video") -> str:
    """Képenkénti, külön kérdések (10-13, mérve: egyetlen összesített sorrendben a modell a „Lorem Ipsum” mockupot és egy
    Zabbix-képernyőt tette előre; képenként kérdezve a szöveget és a mockupot jól jelöli) — a sorrendet mi számoljuk."""
    head = (f'A slide of a presentation, titled "{context}". Its picture should show: "{query}". ' if medium == "slide" else
            f'A scene of a short news video: "{context}". Its full-screen background picture should show: "{query}". ')
    return (head + 'For EACH numbered picture answer: "text": does it show readable words, numbers, labels, a stamp, a logo '
            'or a watermark? (true/false); "mockup": is it a software screenshot, a template, a mock-up, a diagram, a map or '
            'an old document? (true/false); "fit": 0-10, how well it is a clear, real photo of that subject. '
            'Return JSON only: {"pictures": [{"n": 1, "text": false, "mockup": false, "fit": 7}, ...]}')


def contact_sheet(thumbs: list[bytes]) -> str:
    """A jelöltek bélyegképei számozott rácsban (3×2) — egy képen, egy látómodell-hívásnak."""
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    cw, ch, cols = 360, 240, 3
    rows = math.ceil(len(thumbs) / cols)
    sheet = Image.new("RGB", (cols * cw, rows * ch), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 34)
    except Exception:
        font = ImageFont.load_default()
    for k, data in enumerate(thumbs):
        x, y = (k % cols) * cw, (k // cols) * ch
        try:
            im = ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGB"), (cw - 6, ch - 6))
            sheet.paste(im, (x + 3, y + 3))
        except Exception:
            pass
        draw.rectangle([x + 3, y + 3, x + 50, y + 46], fill="black")
        draw.text((x + 14, y + 6), str(k + 1), fill="white", font=font)
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


async def rank(query: str, context: str, cands: list[dict[str, Any]], prompt: Optional[str] = None,
               medium: str = "video") -> list[int]:
    """A látómodell sorrendje a jelöltekre (0-tól), a legjobban illő elöl; hiba esetén a kereső sorrendje."""
    from app.config import settings
    from app.llm import vision as v
    thumbs = await asyncio.gather(*(_get(c["thumb_url"]) for c in cands), return_exceptions=True)
    ok = [(i, t.content) for i, t in enumerate(thumbs) if not isinstance(t, Exception) and t.status_code == 200]
    if len(ok) < 2:
        return list(range(len(cands)))
    judged = prompt is None                      # saját prompt (Lottie): a régi {"ranking": […]} alak
    prompt = prompt or judge_prompt(query, context or query, medium)
    msgs = [{"role": "user", "content": [{"type": "text", "text": prompt},
                                         {"type": "image_url", "image_url": {"url": contact_sheet([b for _, b in ok])}}]}]
    for model, tmo in ((settings.LLM_MODEL_VISION, 40), (settings.LLM_MODEL_VISION_FALLBACK, 60)):
        try:
            raw = await asyncio.wait_for(v._call(msgs, max_tokens=600 if judged else 200, model=model), tmo)
            m = re.search(r"\{.*\}", v._strip_json_fences(raw), re.S)
            js = json.loads(m.group(0))
            if judged:          # pontszám: illeszkedés, a szöveges kép és a mockup erősen hátrébb (semmi nem esik ki)
                scored = []
                for pic in js.get("pictures") or []:
                    n = int(pic.get("n") or 0)
                    if 1 <= n <= len(ok):
                        fit = float(pic.get("fit") or 0)
                        scored.append((fit - 4 * bool(pic.get("text")) - 3 * bool(pic.get("mockup")), fit, ok[n - 1][0]))
                order = [i for _, _, i in sorted(scored, key=lambda x: (-x[0], -x[1]))]
            else:
                order = [ok[int(n) - 1][0] for n in (js.get("ranking") or []) if 1 <= int(n) <= len(ok)]
            if order:
                return list(dict.fromkeys(order + [i for i, _ in ok]))
        except Exception as e:
            log.info("képrangsor (%s, %s): %s", model, query, e)
    return [i for i, _ in ok]


def _fits_frame(c: dict[str, Any], aspect: Optional[float]) -> bool:
    """Technikai feltétel (nem tartalmi): elég nagy, és fekvő keretbe nem álló kép."""
    w, h = c.get("width") or 0, c.get("height") or 0
    if w and w < 1000:
        return False
    return not (aspect and aspect > 1.18 and w and h and w / h < 1.1)


async def choose_stock(jobs: list[tuple], medium: str = "video") -> dict[Any, dict[str, Any]]:
    """Helyenként: stock-jelöltek → a látómodell rangsorolja → a legjobb, ami még nem szerepelt másik helyen.
    jobs = [(kulcs, keresőszó, kontextus (jelenet / dia címe)[, keret-arány])] → {kulcs: {"url", "credit"}};
    ami nem sikerült, kimarad (a hívó a régi úton keres)."""
    sem = asyncio.Semaphore(4)

    async def one(q: str, ctx: str, aspect: Optional[float]) -> tuple[list[dict[str, Any]], list[int]]:
        async with sem:
            try:
                # „gold bars and oil barrels”: a Commons a fájlnévben keres, az összetett kifejezésre rossz jelölteket ad →
                # a részek külön keresve, a jelöltek felváltva összefésülve (a látómodell választ közülük)
                parts = [x.strip() for x in re.split(r"\s+(?:and|&|with)\s+|,", q) if len(x.strip()) > 2]
                if len(parts) > 1:
                    pools = await asyncio.gather(*(search(x, n=PICK_N + 2) for x in parts[:3]))
                    seen, cands = set(), []
                    for row in zip(*[pl + [None] * (PICK_N + 2 - len(pl)) for pl in pools]):
                        for c in row:
                            if c and c["url"] not in seen:
                                seen.add(c["url"]); cands.append(c)
                else:
                    cands = await search(q, n=PICK_N + 4)
                cands = ([c for c in cands if _fits_frame(c, aspect)] or cands)[:PICK_N]
                return cands, (await rank(q, ctx, cands, medium=medium) if len(cands) > 1 else list(range(len(cands))))
            except Exception as e:
                log.info("képjelöltek (%s): %s", q, e)
                return [], []
    got = await asyncio.gather(*(one(j[1], j[2], j[3] if len(j) > 3 else None) for j in jobs))
    out, used = {}, set()
    for j, (cands, order) in zip(jobs, got):
        for k in order:
            if cands[k]["url"] not in used:
                used.add(cands[k]["url"])
                out[j[0]] = {"url": cands[k]["url"], "credit": cands[k]["credit"]}
                break
    return out


def _candidate_tile_label(k: int, c: dict[str, Any]) -> str:
    w, h = c.get("width"), c.get("height")
    return f"{k} · {(c.get('title') or '')[:34]}" + (f" · {w}×{h}" if w and h else "")


async def candidate_sheet(cands: list[dict[str, Any]]) -> Optional[bytes]:
    """A stock-jelöltek bélyegképei számozva, EGY képen — a szerver tölti le (a saját UA-jával), így a hívó AI
    a válaszban maga látja őket (10-13: a thumb_url-ek egyszerű klienssel nem töltődtek le, a Wikimedia UA-t kér)."""
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    if not cands:
        return None
    async def thumb(c: dict[str, Any]):
        return await _get(c["thumb_url"]) if c.get("thumb_url") else None
    got = await asyncio.gather(*(thumb(c) for c in cands), return_exceptions=True)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 15)
    except Exception:
        font = ImageFont.load_default()
    cols, tw, th = 4, 300, 200
    tiles = []
    for k, (c, r) in enumerate(zip(cands, got), 1):          # k = a lista sorszáma (1 = az első jelölt)
        tile = Image.new("RGB", (tw, th + 26), "white")
        if r is not None and not isinstance(r, Exception):
            try:
                tile.paste(ImageOps.contain(Image.open(io.BytesIO(r.content)).convert("RGB"), (tw - 6, th - 6)), (3, 3))
            except Exception:
                pass
        dr = ImageDraw.Draw(tile)
        dr.rectangle([3, 3, 34, 28], fill="black")
        dr.text((10, 6), str(k), fill="white", font=font)
        dr.text((5, th + 4), _candidate_tile_label(k, c)[:42], fill=(40, 40, 40), font=font)
        tiles.append(tile)
    if not tiles:
        return None
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (tw + 8) + 8, rows * (th + 34) + 8), (225, 225, 225))
    for k, t in enumerate(tiles):
        sheet.paste(t, (8 + (k % cols) * (tw + 8), 8 + (k // cols) * (th + 34)))
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=78)
    return buf.getvalue()
