"""Valódi térkép a videó saját grafikájához (chart type "graphic", shape "map") — kulcs és fizetős szolgáltatás nélkül.

    geocode(q)                    helynév → lat/lon + befoglaló doboz (OpenStreetMap Nominatim; ≤ 1 kérés/s, gyorsítótár)
    build(spec, points, box, W, H) → a térkép rétegei (csoportonként vektor-útvonalak, a képkocka arányában 0–1) és a
                                    vetítés (lat/lon → képkocka-arány) a tűknek, vonalaknak

Két részletesség (detail: auto | streets | outline):
  streets — város / kerület: utak (osztályonként), folyók, tavak az OpenStreetMapből (Overpass), ODbL: a képen és a
            záróképen „© OpenStreetMap contributors”
  outline — ország / régió: országhatárok a Natural Earth-ből (közkincs), a kért ország kiemelve
Web Mercator vetítés, a doboz arányára kitágítva; a vonalak a dobozra vágva és egyszerűsítve (Douglas–Peucker).
A letöltések lemez-gyorsítótárban (a temp mappában) — egy szerkesztés újraépítése nem kérdez újra.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger("aice.geo")

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_OVERPASS = ("https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter")
_NE_COUNTRIES = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson"
OSM_CREDIT = "Map data © OpenStreetMap contributors (ODbL)"
NE_CREDIT = "Map: Natural Earth"
_lock = asyncio.Lock()
_last = [0.0]


def _ua() -> str:
    from app.config import settings
    return f"AIConversionEditor-Studio/1.0 (+{settings.PUBLIC_BASE_URL.rstrip('/')})"


def _cache_dir() -> Path:
    from app.storage import temp_path
    d = temp_path("").parent / "geo-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cached(kind: str, key: str, max_age: float) -> Optional[Any]:
    p = _cache_dir() / f"{kind}-{hashlib.sha1(key.encode()).hexdigest()[:20]}.json"
    try:
        if time.time() - p.stat().st_mtime < max_age:
            return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        pass
    return None


def _store(kind: str, key: str, data: Any) -> None:
    p = _cache_dir() / f"{kind}-{hashlib.sha1(key.encode()).hexdigest()[:20]}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    tmp.replace(p)


# ---------- helymeghatározás ----------

async def geocode(q: str) -> dict[str, Any]:
    q = " ".join((q or "").split())[:160]
    if not q:
        raise ValueError("a place name is empty")
    hit = _cached("geocode", q.lower(), 90 * 86400)
    if hit:
        return hit
    async with _lock:                      # Nominatim: legfeljebb 1 kérés / másodperc
        wait = 1.1 - (time.time() - _last[0])
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _ua()}) as c:
                r = await c.get(_NOMINATIM, params={"q": q, "format": "jsonv2", "limit": 1, "addressdetails": 1})
                r.raise_for_status()
                rows = r.json()
        finally:
            _last[0] = time.time()
    if not rows:
        raise ValueError(f"place not found: “{q}”")
    a = rows[0]
    s, n, w, e = (float(x) for x in a["boundingbox"])
    out = {"query": q, "name": a.get("name") or q, "lat": float(a["lat"]), "lon": float(a["lon"]),
           "bbox": [s, w, n, e], "rank": int(a.get("place_rank") or 30), "type": a.get("addresstype") or a.get("type"),
           "cc": ((a.get("address") or {}).get("country_code") or "").upper()}
    _store("geocode", q.lower(), out)
    return out


# ---------- vetítés ----------

def _merc(lat: float, lon: float) -> tuple[float, float]:
    lat = max(-85.0, min(85.0, lat))
    return math.radians(lon), math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


class Projection:
    """Mercator → a doboz pixelei (a doboz bal felső sarka box[0], box[1]); y lefelé nő."""

    def __init__(self, bbox: list[float], box: tuple[float, float, float, float], W: int, H: int):
        s, w, n, e = bbox
        x0, y0 = _merc(s, w)
        x1, y1 = _merc(n, e)
        bx, by, bw, bh = box
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        sx, sy = max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)
        self.k = min(bw / sx, bh / sy)                       # a teljes bbox beférjen, a maradék irányban tágul
        self.cx, self.cy, self.box, self.W, self.H = cx, cy, box, W, H

    def px(self, lat: float, lon: float) -> tuple[float, float]:
        x, y = _merc(lat, lon)
        bx, by, bw, bh = self.box
        return bx + bw / 2 + (x - self.cx) * self.k, by + bh / 2 - (y - self.cy) * self.k

    def px_merc(self, x: float, y: float) -> tuple[float, float]:
        bx, by, bw, bh = self.box
        return bx + bw / 2 + (x - self.cx) * self.k, by + bh / 2 - (y - self.cy) * self.k

    def frac(self, lat: float, lon: float) -> list[float]:
        x, y = self.px(lat, lon)
        return [round(x / self.W, 5), round(y / self.H, 5)]

    def span(self, box: Optional[tuple[float, float, float, float]] = None) -> list[float]:
        """A dobozban (alapból a vetítés dobozában) látszó terület [s, w, n, e]."""
        bx, by, bw, bh = box or self.box
        def inv(px: float, py: float) -> tuple[float, float]:
            x = self.cx + (px - bx - bw / 2) / self.k
            y = self.cy - (py - by - bh / 2) / self.k
            return math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2), math.degrees(x)
        n, w = inv(bx, by)
        s, e = inv(bx + bw, by + bh)
        return [s, w, n, e]


# ---------- geometria: vágás, egyszerűsítés ----------

def _clip_line(pts: list[tuple[float, float]], r: tuple[float, float, float, float]) -> list[list[tuple[float, float]]]:
    """Töröttvonal a téglalapra vágva (Liang–Barsky szakaszonként) → darabok."""
    x0, y0, x1, y1 = r
    out, cur = [], []
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        dx, dy = bx - ax, by - ay
        t0, t1, ok = 0.0, 1.0, True
        for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
            if p == 0:
                if q < 0:
                    ok = False
                    break
            else:
                t = q / p
                if p < 0:
                    t0 = max(t0, t)
                else:
                    t1 = min(t1, t)
        if not ok or t0 > t1:
            if len(cur) > 1:
                out.append(cur)
            cur = []
            continue
        a = (ax + t0 * dx, ay + t0 * dy)
        b = (ax + t1 * dx, ay + t1 * dy)
        if not cur or abs(cur[-1][0] - a[0]) > 1e-6 or abs(cur[-1][1] - a[1]) > 1e-6:
            if len(cur) > 1:
                out.append(cur)
            cur = [a]
        cur.append(b)
        if t1 < 1:
            out.append(cur)
            cur = []
    if len(cur) > 1:
        out.append(cur)
    return out


def _clip_poly(pts: list[tuple[float, float]], r: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    """Sokszög a téglalapra vágva (Sutherland–Hodgman)."""
    x0, y0, x1, y1 = r
    edges = ((lambda p: p[0] >= x0, lambda a, b: (x0, a[1] + (b[1] - a[1]) * (x0 - a[0]) / (b[0] - a[0]))),
             (lambda p: p[0] <= x1, lambda a, b: (x1, a[1] + (b[1] - a[1]) * (x1 - a[0]) / (b[0] - a[0]))),
             (lambda p: p[1] >= y0, lambda a, b: (a[0] + (b[0] - a[0]) * (y0 - a[1]) / (b[1] - a[1]), y0)),
             (lambda p: p[1] <= y1, lambda a, b: (a[0] + (b[0] - a[0]) * (y1 - a[1]) / (b[1] - a[1]), y1)))
    out = list(pts)
    for inside, cut in edges:
        if not out:
            break
        src, out = out, []
        for i, p in enumerate(src):
            prev = src[i - 1]
            if inside(p):
                if not inside(prev):
                    out.append(cut(prev, p))
                out.append(p)
            elif inside(prev):
                out.append(cut(prev, p))
    return out


def _rdp(pts: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        (ax, ay), (bx, by) = pts[i], pts[j]
        dx, dy = bx - ax, by - ay
        nrm = math.hypot(dx, dy)
        best, k = -1.0, -1
        for m in range(i + 1, j):           # egybeeső végpontoknál (zárt gyűrű) a ponttól mért távolság
            d = (abs(dy * (pts[m][0] - ax) - dx * (pts[m][1] - ay)) / nrm) if nrm > 1e-9 else math.hypot(pts[m][0] - ax, pts[m][1] - ay)
            if d > best:
                best, k = d, m
        if best > eps and k > 0:
            keep[k] = True
            stack += [(i, k), (k, j)]
    return [p for p, f in zip(pts, keep) if f]


def _length(pts: list[tuple[float, float]]) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))


# ---------- adatforrások ----------

_OFM = "https://tiles.openfreemap.org/planet"
OFM_CREDIT = "Map: OpenFreeMap © OpenMapTiles, data © OpenStreetMap contributors (ODbL)"


def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if c < 0x80:
            return r, i
        s += 7


def _fields(b: bytes):
    """protobuf: (mezőszám, drótípus, érték) — 2-es típusnál a bájtok."""
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 2:
            ln, i = _varint(b, i)
            v, i = b[i:i + ln], i + ln
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError("bad protobuf wire type")
        yield f, wt, v


def _packed(b: bytes) -> list[int]:
    out, i = [], 0
    while i < len(b):
        v, i = _varint(b, i)
        out.append(v)
    return out


def _mvt(data: bytes, want: dict[str, set[str] | None]) -> list[tuple[str, str, int, list[list[tuple[float, float]]]]]:
    """Mapbox Vector Tile → [(réteg, osztály, típus 2=vonal 3=sokszög, gyűrűk/vonalak csempe-koordinátában 0–1)]."""
    import struct
    out = []
    for f, _, layer in _fields(data):
        if f != 3:
            continue
        name, feats, keys, vals, ext = "", [], [], [], 4096
        for lf, _, lv in _fields(layer):
            if lf == 1:
                name = lv.decode("utf-8", "replace")
            elif lf == 2:
                feats.append(lv)
            elif lf == 3:
                keys.append(lv.decode("utf-8", "replace"))
            elif lf == 4:
                v = None
                for vf, vwt, vv in _fields(lv):
                    v = vv.decode("utf-8", "replace") if vf == 1 else struct.unpack("<f", vv)[0] if vf == 2 else \
                        struct.unpack("<d", vv)[0] if vf == 3 else vv
                vals.append(v)
            elif lf == 5:
                ext = lv
        if name not in want:
            continue
        classes = want[name]
        for ft in feats:
            tags, typ, geom = [], 0, []
            for ff, _, fv in _fields(ft):
                if ff == 2:
                    tags = _packed(fv)
                elif ff == 3:
                    typ = fv
                elif ff == 4:
                    geom = _packed(fv)
            if typ not in (2, 3):
                continue
            props = {keys[tags[k]]: vals[tags[k + 1]] for k in range(0, len(tags) - 1, 2)
                     if tags[k] < len(keys) and tags[k + 1] < len(vals)}
            cls = str(props.get("class") or "")
            if classes is not None and cls not in classes:
                continue
            if props.get("brunnel") == "tunnel":
                continue
            parts, cur, x, y, i = [], [], 0, 0, 0
            while i < len(geom):
                cmd, cnt = geom[i] & 7, geom[i] >> 3
                i += 1
                if cmd == 7:
                    if cur:
                        parts.append(cur)
                    cur = []
                    continue
                for _ in range(cnt):
                    dx, dy = geom[i], geom[i + 1]
                    i += 2
                    x += (dx >> 1) ^ -(dx & 1)
                    y += (dy >> 1) ^ -(dy & 1)
                    if cmd == 1:
                        if len(cur) > 1:
                            parts.append(cur)
                        cur = [(x / ext, y / ext)]
                    else:
                        cur.append((x / ext, y / ext))
            if len(cur) > 1:
                parts.append(cur)
            out.append((name, cls, typ, parts))
    return out


async def _tiles(view: list[float]) -> list[tuple[str, str, int, list[list[tuple[float, float]]]]]:
    """A nézet vektorcsempéi (OpenFreeMap) → elemek MERCATOR-koordinátában (x: rad hosszúság, y: ln tan)."""
    import gzip
    s, w, n, e = view
    x0, y1 = _merc(s, w)
    x1, y0 = _merc(n, e)
    span = max(x1 - x0, y0 - y1)
    z = max(5, min(14, int(math.floor(math.log2(2 * math.pi / max(span / 3.0, 1e-9))))))
    def tile_of(x: float, y: float) -> tuple[int, int]:
        N = 2 ** z
        return int((x + math.pi) / (2 * math.pi) * N), int((math.pi - y) / (2 * math.pi) * N)
    while True:
        (tx0, ty0), (tx1, ty1) = tile_of(x0, y0), tile_of(x1, y1)
        if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) <= 20 or z <= 5:
            break
        z -= 1
    tj = _cached("ofm", "tilejson", 86400)
    if tj is None:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _ua()}) as c:
            r = await c.get(_OFM)
            r.raise_for_status()
            tj = {"tiles": r.json()["tiles"]}
        _store("ofm", "tilejson", tj)
    tmpl = tj["tiles"][0]
    want: dict[str, set[str] | None] = {
        "transportation": {"motorway", "trunk", "primary", "secondary", "tertiary", "minor", "service"} if z >= 14 else
                          {"motorway", "trunk", "primary", "secondary", "tertiary", "minor"} if z >= 12 else
                          {"motorway", "trunk", "primary", "secondary"},
        "water": None, "waterway": {"river", "canal"} if z < 13 else {"river", "canal", "stream"}, "park": None}
    N = 2 ** z
    out = []
    sem = asyncio.Semaphore(6)

    async def one(tx: int, ty: int) -> None:
        url = tmpl.replace("{z}", str(z)).replace("{x}", str(tx)).replace("{y}", str(ty))
        key = url
        p = _cache_dir() / f"tile-{hashlib.sha1(key.encode()).hexdigest()[:20]}.pbf"
        try:
            data = p.read_bytes() if p.exists() and time.time() - p.stat().st_mtime < 30 * 86400 else None
        except OSError:
            data = None
        if data is None:
            async with sem:
                async with httpx.AsyncClient(timeout=30, headers={"User-Agent": _ua()}) as c:
                    r = await c.get(url)
                    if r.status_code == 204 or r.status_code == 404:
                        data = b""
                    else:
                        r.raise_for_status()
                        data = r.content
            p.write_bytes(data)
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        for name, cls, typ, parts in _mvt(data, want):
            merc = [[((tx + u) / N * 2 * math.pi - math.pi, math.pi - (ty + v) / N * 2 * math.pi) for u, v in part] for part in parts]
            out.append((name, cls, typ, merc))
    await asyncio.gather(*(one(tx, ty) for tx in range(tx0, tx1 + 1) for ty in range(ty0, ty1 + 1)))
    return out


async def _overpass(bbox: list[float], classes: tuple[str, ...]) -> list[dict[str, Any]]:
    s, w, n, e = (round(v, 4) for v in bbox)
    hw = "|".join(classes)
    q = (f'[out:json][timeout:40];(way["highway"~"^({hw})$"]({s},{w},{n},{e});'
         f'way["waterway"~"^(river|canal)$"]({s},{w},{n},{e});way["natural"="water"]({s},{w},{n},{e});'
         f'way["leisure"="park"]({s},{w},{n},{e}););out geom;')
    hit = _cached("overpass", q, 30 * 86400)
    if hit is not None:
        return hit
    last: Exception = RuntimeError("no Overpass server answered")
    for url in _OVERPASS:
        try:
            async with httpx.AsyncClient(timeout=60, headers={"User-Agent": _ua()}) as c:
                r = await c.post(url, data={"data": q})
                r.raise_for_status()
                els = [{"t": x.get("tags") or {}, "g": [(p["lat"], p["lon"]) for p in x.get("geometry") or []]}
                       for x in r.json().get("elements") or [] if x.get("geometry")]
            _store("overpass", q, els)
            return els
        except Exception as ex:
            last = ex
            log.info("overpass (%s): %s", url, ex)
    raise RuntimeError(f"street map data could not be loaded: {last}")


async def _countries() -> list[dict[str, Any]]:
    hit = _cached("ne", "countries50", 365 * 86400)
    if hit is not None:
        return hit
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.get(_NE_COUNTRIES)
        r.raise_for_status()
        fc = r.json()
    out = []
    for f in fc.get("features") or []:
        g, pr = f.get("geometry") or {}, f.get("properties") or {}
        polys = [g["coordinates"]] if g.get("type") == "Polygon" else g.get("coordinates") or []
        rings = [[(lat, lon) for lon, lat in poly[0]] for poly in polys if poly]   # csak a külső gyűrű
        if rings:
            out.append({"name": pr.get("NAME") or pr.get("ADMIN"), "iso": (pr.get("ISO_A2_EH") or pr.get("ISO_A2") or "").upper(),
                        "rings": rings})
    _store("ne", "countries50", out)
    return out


# ---------- a térkép ----------

_DARK = {"minor": [0.24, 0.27, 0.33], "major": [0.46, 0.5, 0.58], "water": [0.16, 0.34, 0.52], "park": [0.12, 0.2, 0.15],
         "land": [0.11, 0.135, 0.18], "border": [0.32, 0.36, 0.43], "focus": [0.17, 0.25, 0.34]}


async def build(spec: dict[str, Any], points: list[tuple[float, float]], box: tuple[float, float, float, float],
                W: int, H: int, fit: Optional[tuple[float, float, float, float]] = None) -> dict[str, Any]:
    """spec: {"place"?, "bbox"? [s, w, n, e], "detail"?}; points: a jelenet többi földrajzi pontja (lat, lon) — a térkép
    ezeket is mutassa. → {"groups": [...], "proj": Projection, "credit", "mode", "place"}"""
    place = await geocode(spec["place"]) if spec.get("place") else None
    if spec.get("bbox"):
        bb = [float(v) for v in spec["bbox"]]
    elif place:
        bb = list(place["bbox"])
    elif points:
        bb = [min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)]
    else:
        raise ValueError("a map needs a place, a bbox or placed pins")
    for la, lo in points:                       # minden tű látsszon
        bb = [min(bb[0], la), min(bb[1], lo), max(bb[2], la), max(bb[3], lo)]
    pad = 0.12 if points else 0.04
    dla, dlo = max(bb[2] - bb[0], 0.004), max(bb[3] - bb[1], 0.006)
    bb = [bb[0] - dla * pad, bb[1] - dlo * pad, bb[2] + dla * pad, bb[3] + dlo * pad]
    proj = Projection(bb, fit or box, W, H)      # a hely és a tűk a fit-dobozba (cím és felirat közé) illeszkednek,
    view = proj.span(box)                        # a térkép viszont a teljes dobozt kitölti
    extent = max(view[2] - view[0], (view[3] - view[1]) * math.cos(math.radians((view[0] + view[2]) / 2)))
    detail = spec.get("detail") if spec.get("detail") in ("streets", "outline") else (
        "outline" if extent > 1.6 or (place and place["rank"] <= 8) else "streets")
    bx, by, bw, bh = box
    rect = (bx, by, bx + bw, by + bh)
    m = min(W, H) / 1080
    groups: list[dict[str, Any]] = []

    def to_frac(path: list[tuple[float, float]]) -> list[list[float]]:
        return [[round(x / W, 5), round(y / H, 5)] for x, y in path]

    if detail == "outline":
        focus = (place or {}).get("cc") if place and place["rank"] <= 4 else None
        other, hi = [], []
        for c in await _countries():
            for ring in c["rings"]:
                if not any(view[0] - 5 <= la <= view[2] + 5 and view[1] - 5 <= lo <= view[3] + 5 for la, lo in ring[::7]):
                    continue
                ring = ring[:-1] if len(ring) > 3 and ring[0] == ring[-1] else ring        # a zárást a réteg végzi
                poly = _clip_poly([proj.px(la, lo) for la, lo in ring], rect)
                poly = _rdp(poly, 0.9 * m)
                if len(poly) >= 3:
                    (hi if focus and c["iso"] == focus else other).append(to_frac(poly))
        groups.append({"name": "Countries", "paths": other, "closed": True, "fill": _DARK["land"], "stroke": _DARK["border"], "width": 2 * m})
        if hi:
            groups.append({"name": "Highlighted country", "paths": hi, "closed": True, "fill": _DARK["focus"], "stroke": "accent", "width": 4 * m})
        credit = NE_CREDIT
    else:
        try:
            groups = _street_groups_from_tiles(await _tiles(view), proj, rect, m, to_frac)
            credit = OFM_CREDIT
        except Exception as ex:
            log.info("vektorcsempék (%s): %s — Overpass", view, ex)
            groups = []
    if detail != "outline" and not groups:
        classes = ["motorway", "trunk", "primary"]
        if extent < 0.35:
            classes += ["secondary", "tertiary"]
        if extent < 0.07:
            classes += ["residential", "unclassified", "living_street", "pedestrian"]
        q = [round(view[0] - 0.002, 3), round(view[1] - 0.002, 3), round(view[2] + 0.002, 3), round(view[3] + 0.002, 3)]
        els = await _overpass(q, tuple(classes + [c + "_link" for c in ("motorway", "trunk", "primary")]))
        buckets: dict[str, list] = {"park": [], "lake": [], "river": [], "minor": [], "major": []}
        budget = 26000                             # összes csúcs: a motor rétegépítése ennyit bír kényelmesen
        for x in els:
            t, g = x["t"], x["g"]
            if len(g) < 2:
                continue
            pts = [proj.px(la, lo) for la, lo in g]
            hw = t.get("highway", "")
            if t.get("natural") == "water" or t.get("leisure") == "park":
                if len(pts) < 4 or pts[0] != pts[-1]:
                    continue
                poly = _rdp(_clip_poly(pts[:-1], rect), 0.8 * m)
                if len(poly) >= 3 and abs(_area(poly)) > 40 * m * m:
                    buckets["lake" if t.get("natural") == "water" else "park"].append(poly)
                continue
            key = "river" if t.get("waterway") else "major" if hw.split("_")[0] in ("motorway", "trunk", "primary", "secondary") else "minor"
            for part in _clip_line(pts, rect):
                part = _rdp(part, 0.7 * m)
                if len(part) >= 2 and _length(part) > 6 * m:
                    buckets[key].append(part)
        order = [("park", "Parks", True, _DARK["park"], None, 0), ("lake", "Water areas", True, _DARK["water"], None, 0),
                 ("river", "Rivers", False, None, _DARK["water"], 9), ("minor", "Streets", False, None, _DARK["minor"], 2.2),
                 ("major", "Main roads", False, None, _DARK["major"], 4.5)]
        used = 0
        for key, name, closed, fill, stroke, w in order:
            paths = []
            for p in sorted(buckets[key], key=_length, reverse=True):   # a hosszabbak előbb — a keret a kis darabokat vágja
                if used + len(p) > budget:
                    break
                used += len(p)
                paths.append(to_frac(p))
            if paths:
                groups.append({"name": name, "paths": paths, "closed": closed, "fill": fill, "stroke": stroke, "width": w * m})
        credit = OSM_CREDIT
    return {"groups": groups, "proj": proj, "credit": credit, "mode": detail, "place": (place or {}).get("name")}


def _street_groups_from_tiles(feats: list, proj: Projection, rect: tuple[float, float, float, float], m: float,
                              to_frac) -> list[dict[str, Any]]:
    buckets: dict[str, list] = {"park": [], "lake": [], "river": [], "minor": [], "major": []}
    major = {"motorway", "trunk", "primary", "secondary"}
    for name, cls, typ, parts in feats:
        for part in parts:
            pts = [proj.px_merc(x, y) for x, y in part]
            if typ == 3:
                if name not in ("water", "park"):
                    continue
                poly = _rdp(_clip_poly(pts, rect), 0.8 * m)
                if len(poly) >= 3 and abs(_area(poly)) > 30 * m * m:
                    buckets["lake" if name == "water" else "park"].append(poly)
            else:
                key = "river" if name == "waterway" else "major" if cls in major else "minor"
                for seg in _clip_line(pts, rect):
                    seg = _rdp(seg, 0.7 * m)
                    if len(seg) >= 2 and _length(seg) > 6 * m:
                        buckets[key].append(seg)
    if not any(buckets.values()):
        raise ValueError("no map features in view")
    order = [("park", "Parks", True, _DARK["park"], None, 0), ("lake", "Water areas", True, _DARK["water"], None, 0),
             ("river", "Rivers", False, None, _DARK["water"], 6), ("minor", "Streets", False, None, _DARK["minor"], 2.2),
             ("major", "Main roads", False, None, _DARK["major"], 4.5)]
    groups, used, budget = [], 0, 26000
    for key, gname, closed, fill, stroke, w in order:
        paths = []
        for p in sorted(buckets[key], key=lambda q: abs(_area(q)) if closed else _length(q), reverse=True):
            if used + len(p) > budget:
                break
            used += len(p)
            paths.append(to_frac(p))
        if paths:
            groups.append({"name": gname, "paths": paths, "closed": closed, "fill": fill, "stroke": stroke, "width": w * m})
    return groups


def _area(poly: list[tuple[float, float]]) -> float:
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(poly, poly[1:] + poly[:1])) / 2
