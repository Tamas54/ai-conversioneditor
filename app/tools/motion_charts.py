"""Újabb mozgó grafikonok a videóhoz (10-17): vízesés, előtte → utána, pont-/buborékdiagram, sáv-előrejelzés, több
vonal (forgatókönyvek), rangsor-verseny. A ChatGPT-értékelés hiánylistájáról (változás-nézet, bizonytalansági sáv,
forgatókönyv-összevetés, pontdiagram) és a mozgógrafika klasszikusaiból (vízesés, „bar race”). Sankey: mennyiségek
útja szakaszokon át (forrás → program → projekt) — a folyamok a forrásból, a csomópontok összege kiszámolt.

Ugyanaz a szabály, mint a többi ábránál (video._chart_from_plan): minden szám és dátum a FORRÁSBÓL — ami nincs benne,
attól az ábra kimarad, és a check megmondja, miért. A kiszámolt érték (a vízesés vége) kiszámolt: ha a forrás mást
mond, az a check-ben van. A rajz a meglévő elemkészlet (rect / text / path), a builder ugyanúgy rakja le; a versenyhez
a kulcskockás mozgás (keys: [[t, x, y, szélesség?, átlátszóság?]]) új a builderben.
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable, Optional

TYPES = ("waterfall", "slope", "scatter", "band", "lines", "race", "sankey")
ALIASES = {"bridge": "waterfall", "before_after": "slope", "change": "slope", "bubble": "scatter", "forecast": "band",
           "range": "band", "uncertainty": "band", "scenarios": "lines", "multiline": "lines", "bar_race": "race",
           "racing_bars": "race", "ranking": "race", "alluvial": "sankey", "flows": "sankey", "money_flow": "sankey"}
SANKEY_NODES, SANKEY_FLOWS = 14, 24     # ennél több csomópont / szalag a képernyőn olvashatatlan
RED = [0.93, 0.36, 0.33]
SOFT = [0.86, 0.88, 0.92]
LIGHT = [0.93, 0.94, 0.96]


# ---------- a terv ellenőrzése: a számok a forrásból ----------

def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("−", "-").replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


class _Src:
    def __init__(self, source: str):
        from app.tools.video import _text_numbers
        self.nums = _text_numbers(source)
        self.text = re.sub(r"\s+", " ", source or "").lower()

    def has(self, v: Optional[float]) -> bool:
        return v is not None and (round(v, 6) in self.nums or round(abs(v), 6) in self.nums)

    def has_label(self, lab: str) -> bool:
        """Időszak / dátum: ha szám van benne (2024, Q3 2025), szó szerint a forrásban legyen; a szavas („előtte”) szabad."""
        return not re.search(r"\d", lab) or re.sub(r"\s+", " ", lab).lower() in self.text


def parse(typ: str, c: dict[str, Any], source: str) -> tuple[Optional[dict[str, Any]], Any]:
    from app.tools.video import _clean, _lang_of
    S = _Src(source)
    title = _clean(c.get("title"), 60, "Chart title")
    unit = _clean(c.get("unit"), 14, "Chart unit")
    out: dict[str, Any] = {"type": typ, "title": title, "unit": unit}
    left_out = f"⚠ The {typ} “{title}” was left out: not all its numbers are in the source."

    def period_labels(raw: Any, lo: int, hi: int) -> Optional[list[str]]:
        labs = [_clean(x, 14, "Period") for x in (raw or []) if str(x).strip()][:hi]
        return labs if len(labs) >= lo else None

    if typ == "waterfall":
        items = []
        for it in [x for x in c.get("items") or [] if isinstance(x, dict)][:9]:
            v = _num(it.get("value"))
            if not S.has(v):
                return None, left_out
            items.append({"label": _clean(it.get("label"), 22, "Chart label"), "value": v})
        if len(items) < 3:
            return None, f"⚠ The waterfall “{title}” was left out: it needs a start and at least two changes (items)."
        end = round(items[0]["value"] + sum(i["value"] for i in items[1:]), 6)
        out.update(items=items, total=end, total_label=_clean(c.get("total_label"), 22, "Chart label")
                   or ("Végül" if _lang_of(source) == "hu" else "End"))
        given = _num(c.get("total"))
        if given is not None and abs(given - end) > max(1e-6, abs(end) * 0.005):
            return out, [f"⚠ Waterfall “{title}”: the total {given:g} is not the start plus the changes ({end:g}) — the "
                         f"chart ends at {end:g}; check the numbers."]
        return out, (f"Waterfall “{title}”: start and {len(items) - 1} changes are in the source; the end ({end:g}) is "
                     f"computed" + (" and matches the source." if given is not None else "."))
    if typ == "slope":
        labs = period_labels(c.get("labels"), 2, 2) or ["", ""]
        if not all(S.has_label(x) for x in labs):
            return None, f"⚠ The before/after chart “{title}” was left out: “{labs[0]}” / “{labs[1]}” is not in the source."
        items = []
        for it in [x for x in c.get("items") or [] if isinstance(x, dict)][:8]:
            a, b = _num(it.get("from")), _num(it.get("to"))
            if not (S.has(a) and S.has(b)):
                return None, left_out
            items.append({"label": _clean(it.get("label"), 22, "Chart label"), "from": a, "to": b})
        if len(items) < 1:
            return None, None
        out.update(labels=labs, items=items)
        return out, f"Before/after “{title}”: {len(items)} pair(s), every value is in the source."
    if typ == "scatter":
        items = []
        for it in [x for x in c.get("items") or [] if isinstance(x, dict)][:24]:
            x, y, s = _num(it.get("x")), _num(it.get("y")), _num(it.get("size"))
            if not (S.has(x) and S.has(y)) or (it.get("size") not in (None, "") and not S.has(s)):
                return None, left_out
            items.append({"label": _clean(it.get("label"), 22, "Chart label"), "x": x, "y": y,
                          **({"size": s} if s is not None and s > 0 else {})})
        if len(items) < 3:
            return None, None
        out.update(items=items, x_title=_clean(c.get("x_title"), 32, "Axis title"), y_title=_clean(c.get("y_title"), 32, "Axis title"))
        notes = [f"Scatter “{title}”: {len(items)} points, every value is in the source."]
        if not (out["x_title"] and out["y_title"]):
            notes.append(f"⚠ Scatter “{title}”: give x_title and y_title (what each axis measures, with the unit) — "
                         "without them nobody can read it.")
        return out, notes
    if typ == "band":
        items, ranged = [], 0
        for it in [x for x in c.get("items") or [] if isinstance(x, dict)][:16]:
            lab = _clean(it.get("label"), 14, "Period")
            v, lo, hi = _num(it.get("value")), _num(it.get("low")), _num(it.get("high"))
            if not S.has_label(lab):
                return None, f"⚠ The forecast band “{title}” was left out: “{lab}” is not in the source."
            if not S.has(v) or (lo is not None and not S.has(lo)) or (hi is not None and not S.has(hi)):
                return None, left_out
            row = {"label": lab, "value": v}
            if lo is not None and hi is not None and lo <= v <= hi:
                row.update(low=lo, high=hi)
                ranged += 1
            items.append(row)
        if len(items) < 3 or not ranged:
            return None, f"⚠ The forecast band “{title}” was left out: it needs 3+ points and a low–high range on some."
        out["items"] = items
        ff = str(c.get("forecast_from") or "").strip()
        if ff and ff in [i["label"] for i in items]:
            out["forecast_from"] = ff
        return out, f"Forecast band “{title}”: {len(items)} points, {ranged} with a range, every value is in the source."
    if typ in ("lines", "race"):
        labs = period_labels(c.get("labels"), 2 if typ == "lines" else 3, 16 if typ == "lines" else 12)
        if not labs:
            return None, f"⚠ The {typ} chart “{title}” was left out: give labels (the periods, in order)."
        bad = [x for x in labs if not S.has_label(x)]
        if bad:
            return None, f"⚠ The {typ} chart “{title}” was left out: “{bad[0]}” is not in the source."
        rows = [x for x in (c.get("series") or c.get("items") or []) if isinstance(x, dict)]
        series = []
        for r in rows[:4 if typ == "lines" else 10]:
            vals = [_num(v) for v in (r.get("values") or [])][:len(labs)]
            if len(vals) != len(labs) or not all(S.has(v) for v in vals):
                return None, left_out
            series.append({"label": _clean(r.get("name") or r.get("label"), 22, "Chart label"), "values": vals})
        if len(series) < (2 if typ == "lines" else 3):
            return None, None
        out.update(labels=labs, items=series)
        return out, f"{'Lines' if typ == 'lines' else 'Ranking race'} “{title}”: {len(series)} × {len(labs)} values, all in the source."
    if typ == "sankey":
        raw = [x for x in (c.get("flows") or c.get("items") or []) if isinstance(x, dict)]
        if len(raw) > SANKEY_FLOWS:
            return None, f"⚠ The sankey “{title}” was left out: {len(raw)} flows — {SANKEY_FLOWS} at most; merge the small ones."
        flows: list[dict[str, Any]] = []
        canon: dict[str, str] = {}

        def node(x: Any) -> str:     # ugyanaz a név kis-nagybetűtől függetlenül ugyanaz a csomópont (az első írásmód marad)
            s = _clean(x, 22, "Chart label")
            return canon.setdefault(s.casefold(), s) if s else ""
        for fl in raw:
            v = _num(fl.get("value"))
            if not S.has(v):
                return None, left_out
            a, b = node(fl.get("from")), node(fl.get("to"))
            if not (a and b):
                return None, f"⚠ The sankey “{title}” was left out: every flow needs a “from” and a “to” (the two nodes it joins)."
            if a == b:
                return None, f"⚠ The sankey “{title}” was left out: “{a}” flows into itself — a flow joins two different nodes."
            if v <= 0:
                return None, (f"⚠ The sankey “{title}” was left out: the flow “{a}” → “{b}” is {v:g} — flows are positive "
                              "amounts (turn it round instead).")
            flows.append({"from": a, "to": b, "value": v})
        if len(flows) < 2:
            return None, f"⚠ The sankey “{title}” was left out: it needs at least two flows ({{\"from\", \"to\", \"value\"}})."
        hint: list[dict[str, str]] = []     # a megadott sorrend (és szín) — csak a folyamokban szereplő csomópontokra
        for n in c.get("nodes") or []:
            nm = canon.get(_clean(n.get("name") if isinstance(n, dict) else n, 22).casefold())
            if nm and nm not in [h["name"] for h in hint]:
                hx = str(n.get("color") or "").strip().lstrip("#") if isinstance(n, dict) else ""
                hint.append({"name": nm, **({"color": "#" + hx.upper()} if re.fullmatch(r"[0-9a-fA-F]{6}", hx) else {})})
        g = _sankey_graph(flows, [h["name"] for h in hint])
        if len(g["names"]) > SANKEY_NODES:
            return None, (f"⚠ The sankey “{title}” was left out: {len(g['names'])} nodes — {SANKEY_NODES} at most; merge the "
                          "small ones.")
        if g["cycle"]:
            return None, (f"⚠ The sankey “{title}” was left out: the flows go round in a circle ({' → '.join(g['cycle'])}) — "
                          "a sankey flows one way only.")
        out["flows"] = flows
        if hint:
            out["nodes"] = hint
        return out, (f"Sankey “{title}”: {len(flows)} flows between {len(g['names'])} nodes in {max(g['depth'].values()) + 1} "
                     "stages, every flow is in the source; the node totals are computed.")
    return None, None


def numbers(ch: dict[str, Any]) -> list[str]:
    """A képernyőn megjelenő számok és címkék szövegként (a lefedettség-méréshez)."""
    if ch.get("type") == "sankey":        # a csomópontok neve és összege látszik (a szalagoknak nincs felirata)
        g = _sankey_graph(ch.get("flows") or [], [n["name"] for n in ch.get("nodes") or []])
        return g["names"] + [str(g["total"][n]) for n in g["names"]]
    out = [str(x) for x in ch.get("labels") or []] + [str(ch.get(k) or "") for k in ("x_title", "y_title", "total_label")]
    for it in ch.get("items") or []:
        for k, v in it.items():
            out += [str(x) for x in v] if isinstance(v, list) else [str(v)]
    return out


# ---------- feliratok helye: ne takarjanak pontot és egymást (a pakli pontdiagramja is ezt használja) ----------

def label_sides(P: list[tuple[float, float]], names: list[str], bounds: tuple[float, float, float, float],
                char_w: float, line_h: float, dot: float = 5.0) -> list[str]:
    """Pontonként: right | left | above | below — ahol a felirat a legkevesebb pontot és korábbi feliratot fedi, és nem
    lóg ki a bounds (x0, y0, x1, y1) dobozból."""
    dots = [(px - dot, py - dot, px + dot, py + dot) for px, py in P]
    placed: list[tuple[float, float, float, float]] = []
    out = []
    for k, ((px, py), name) in enumerate(zip(P, names)):
        if not name:
            out.append("right")
            continue
        w, h, g = len(name) * char_w + 4, line_h, dot + 2
        boxes = {"right": (px + g, py - h / 2, px + g + w, py + h / 2), "left": (px - g - w, py - h / 2, px - g, py + h / 2),
                 "above": (px - w / 2, py - g - h, px + w / 2, py - g), "below": (px - w / 2, py + g, px + w / 2, py + g + h)}
        others = [d for j, d in enumerate(dots) if j != k]

        def cost(side: str) -> tuple:
            b = boxes[side]
            n = sum(1 for o in others + placed if b[0] < o[2] and o[0] < b[2] and b[1] < o[3] and o[1] < b[3])
            off = b[0] < bounds[0] or b[2] > bounds[2] or b[1] < bounds[1] or b[3] > bounds[3]
            return (n, off, ["right", "left", "above", "below"].index(side))
        side = min(boxes, key=cost)
        placed.append(boxes[side])
        out.append(side)
    return out


def _nice(lo: float, hi: float, n: int = 4) -> tuple[float, float, float]:
    """Kerek skála: (alsó, felső, lépés) — a 0-t tartalmazza, ha az adatok egy előjelűek."""
    lo, hi = min(lo, 0.0) if lo >= 0 else lo, max(hi, 0.0) if hi <= 0 else hi
    if hi == lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return math.floor(lo / step) * step, math.ceil(hi / step) * step, step


def _spread(ys: list[float], gap: float, lo: float, hi: float) -> list[float]:
    """Függőleges feliratok szétnyomva (legalább gap távolság), a lo–hi sávban, a sorrend marad."""
    idx = sorted(range(len(ys)), key=lambda i: ys[i])
    out = list(ys)
    for a, b in zip(idx, idx[1:]):
        out[b] = max(out[b], out[a] + gap)
    over = out[idx[-1]] - hi if idx else 0
    if over > 0:
        for i in idx:
            out[i] -= over
        for a, b in zip(reversed(idx[1:]), reversed(idx[:-1])):
            out[b] = min(out[b], out[a] - gap)
    return [max(lo, y) for y in out]


def _spread_sized(cs: list[float], sz: list[float], gap: float, lo: float, hi: float) -> list[float]:
    """Mint a _spread, de különböző méretű dobozokra: középpontok, a dobozok közt legalább gap, a lo–hi sávban."""
    idx = sorted(range(len(cs)), key=lambda i: cs[i])
    top = {i: cs[i] - sz[i] / 2 for i in idx}
    y = lo
    for i in idx:
        top[i] = max(top[i], y)
        y = top[i] + sz[i] + gap
    y = hi
    for i in reversed(idx):
        top[i] = min(top[i], y - sz[i])
        y = top[i] - gap
    return [top[i] + sz[i] / 2 for i in range(len(cs))]


# ---------- sankey: folyamok szakaszok között (források → programok → projektek) ----------

def _sankey_graph(flows: list[dict[str, Any]], hint: Any = ()) -> dict[str, Any]:
    """Csomópontok (a megadott sorrend elöl, aztán ahogy a folyamokban jönnek), összegük (a befolyó és a kifolyó közül a
    nagyobb — a forrásnak csak kifolyója, a célnak csak befolyója van), oszlopuk (a leghosszabb út a forrásoktól) és a
    kör, ha van (akkor nincs oszlopsorrend: a szalagok körbeérnének)."""
    used = {x for fl in flows for x in (fl["from"], fl["to"])}
    names = [n for n in dict.fromkeys(list(hint) + [x for fl in flows for x in (fl["from"], fl["to"])]) if n in used]
    tin, tout = dict.fromkeys(names, 0.0), dict.fromkeys(names, 0.0)
    for fl in flows:
        tout[fl["from"]] += fl["value"]
        tin[fl["to"]] += fl["value"]
    indeg = dict.fromkeys(names, 0)
    for fl in flows:
        indeg[fl["to"]] += 1
    depth = dict.fromkeys(names, 0)
    queue, done = [n for n in names if not indeg[n]], 0
    while queue:
        u = queue.pop(0)
        done += 1
        for fl in flows:
            if fl["from"] == u:
                v = fl["to"]
                depth[v] = max(depth[v], depth[u] + 1)
                indeg[v] -= 1
                if not indeg[v]:
                    queue.append(v)
    cycle = None
    if done < len(names):     # a maradékban kör van: visszafelé lépkedve (minden maradónak van maradó elődje) körbeérünk
        rest = {n for n in names if indeg[n] > 0}
        cur, seen, walk = next(n for n in names if n in rest), {}, []
        while cur not in seen:
            seen[cur] = len(walk)
            walk.append(cur)
            cur = next(fl["from"] for fl in flows if fl["to"] == cur and fl["from"] in rest)
        cycle = walk[seen[cur]:][::-1]
        k = min(range(len(cycle)), key=lambda i: names.index(cycle[i]))     # a felsorolásban legelső csomópontnál kezdve
        cycle = cycle[k:] + cycle[:k]
        cycle.append(cycle[0])
    return {"names": names, "total": {n: round(max(tin[n], tout[n]), 6) for n in names}, "depth": depth, "cycle": cycle}


def _sankey_order(g: dict[str, Any], flows: list[dict[str, Any]]) -> tuple[list[list[Any]], list[list[tuple]], dict, dict]:
    """Oszlopok és sorrend. Az oszlopot átugró folyam a köztes oszlopokban „átvezetőt” kap (saját helyet a csomópontok
    között), így a szalag nem fut át dobozon. Sorrend: súlyozott baricentrum-söprések előre-hátra, a legkevesebb
    (értékkel súlyozott) keresztezésű marad — a kiinduló a megadott / megjelenési sorrend.
    → (oszlopok, folyamonként a szakaszok [(a, b, érték)], érték, oszlop) — az átvezető kulcsa ("~", folyam, oszlop)."""
    depth, names = g["depth"], g["names"]
    C = max(depth.values()) + 1
    val: dict[Any, float] = dict(g["total"])
    col: dict[Any, int] = dict(depth)
    rank: dict[Any, float] = {n: float(i) for i, n in enumerate(names)}
    segs = []
    for i, fl in enumerate(flows):
        a, b = fl["from"], fl["to"]
        chain = [a] + [("~", i, c) for c in range(depth[a] + 1, depth[b])] + [b]
        for k in chain[1:-1]:
            val[k], col[k], rank[k] = fl["value"], k[2], rank[a] + 0.5 + i / 1000
        segs.append([(p, q, fl["value"]) for p, q in zip(chain, chain[1:])])
    edges = [e for s in segs for e in s]
    cols = [sorted([k for k in col if col[k] == c], key=lambda k: rank[k]) for c in range(C)]

    def where(cs: list[list[Any]]) -> dict[Any, float]:     # az oszlopon belüli hely 0–1 között, értékkel súlyozva
        pos = {}
        for cc in cs:
            tot, run = sum(val[k] for k in cc) or 1.0, 0.0
            for k in cc:
                pos[k] = (run + val[k] / 2) / tot
                run += val[k]
        return pos

    def crossings(cs: list[list[Any]]) -> float:
        pos = {k: i for cc in cs for i, k in enumerate(cc)}
        n = 0.0
        for i, (a1, b1, w1) in enumerate(edges):
            for a2, b2, w2 in edges[i + 1:]:
                if col[a1] == col[a2] and a1 != a2 and b1 != b2 and (pos[a1] - pos[a2]) * (pos[b1] - pos[b2]) < 0:
                    n += w1 * w2
        return n

    best, low = [list(cc) for cc in cols], crossings(cols)
    for it in range(8):
        fwd = it % 2 == 0
        for c in (range(1, C) if fwd else range(C - 2, -1, -1)):
            pos = where(cols)
            bary = {}
            for k in cols[c]:
                nb = [(pos[a], w) for a, b, w in edges if b == k] if fwd else [(pos[b], w) for a, b, w in edges if a == k]
                bary[k] = sum(p * w for p, w in nb) / sum(w for _, w in nb) if nb else pos[k]
            cols[c].sort(key=lambda k: (round(bary[k], 9), pos[k]))
        n = crossings(cols)
        if n < low - 1e-9:
            best, low = [list(cc) for cc in cols], n
    return best, segs, val, col


def _sankey_plan(ch: dict[str, Any], W: int, H: int, down: bool, f: float, fv: Callable[[float], str],
                 final: bool = True) -> dict[str, Any]:
    """A sankey geometriája egy irányban (down: fentről lefelé folyik, különben balról jobbra) f betűméret-szorzóval.
    Absztrakt tengelyeken számol — d: a folyás iránya, b: rá merőleges —, a képernyőre csak a végén fordít.
    final=False: csak a „fit” (mennyit kellene még kicsinyíteni, hogy minden felirat elférjen), a méréshez."""
    from app.tools.video import _wrap
    m, vertical = min(W, H), H > W * 1.2
    g = _sankey_graph(ch["flows"], [n["name"] for n in ch.get("nodes") or []])
    cols, segs, val, col = _sankey_order(g, ch["flows"])
    C, real = len(cols), g["names"]
    # a rajzterület: fent a cím, lent a felirat-sáv marad szabadon
    ax0, ax1 = W * (0.05 if vertical else 0.06), W * (0.95 if vertical else 0.94)
    ay0, ay1 = H * (0.17 if vertical else 0.19), H * (0.80 if vertical else 0.79)
    (d0, d1), (b0, b1) = ((ay0, ay1), (ax0, ax1)) if down else ((ax0, ax1), (ay0, ay1))
    nz, vz = m * (0.033 if vertical else 0.031) * f, m * 0.034 * f
    pad, lpad, nw = m * 0.012, m * 0.008, max(10.0, m * 0.018)
    inset = m * 0.025                   # a csomópontok a címkesávon belül: a szélső címke is elfér mellettük
    nb0, nb1 = b0 + inset, b1 - inset
    B = nb1 - nb0
    gap = min(m * 0.05, max(m * 0.014, B * 0.3 / max(max(len(cc) for cc in cols) - 1, 1)))
    k = min((B - (len(cc) - 1) * gap) / (sum(val[x] for x in cc) or 1) for cc in cols)
    k = min(k, B * 0.5 / max(val.values()))     # kevés csomópontnál se legyen egyetlen tömb: a legnagyobb a sáv fele
    size = {x: max(val[x] * k, max(2.0, m * 0.003)) for x in val}

    # 1) a csomópontok helye a folyásra merőlegesen: oszloponként középre, aztán a szomszédok felé húzva (egyenesebb
    #    szalagok) — ütközéskor legalább gap köz, a sorrend (a keresztezés-minimum) marad
    top: dict[Any, float] = {}
    for cc in cols:
        y = nb0 + (B - sum(size[x] for x in cc) - (len(cc) - 1) * gap) / 2
        for x in cc:
            top[x] = y
            y += size[x] + gap
    edges = [(i, j, a, b, w) for i, s in enumerate(segs) for j, (a, b, w) in enumerate(s)]
    ins = {x: [(e[2], e[4]) for e in edges if e[3] == x] for x in val}
    outs = {x: [(e[3], e[4]) for e in edges if e[2] == x] for x in val}
    mid = lambda x: top[x] + size[x] / 2    # noqa: E731

    def settle(cc: list[Any]) -> None:
        y = nb0
        for x in cc:
            top[x] = max(top[x], y)
            y = top[x] + size[x] + gap
        y = nb1
        for x in reversed(cc):
            top[x] = min(top[x], y - size[x])
            y = top[x] - gap
    for it in range(6):
        alpha = 0.5 * 0.8 ** it
        for c, nbr in [(c, ins) for c in range(1, C)] + [(c, outs) for c in range(C - 2, -1, -1)]:
            for x in cols[c]:
                if nbr[x]:
                    t = sum(mid(y) * w for y, w in nbr[x]) / sum(w for _, w in nbr[x])
                    top[x] += (t - mid(x)) * alpha
            settle(cols[c])

    # 2) címkék: név (ha a cap-be nem fér, két kiegyensúlyozott sorban) + alatta az összeg
    def label(n: str, cap: float) -> tuple[float, float, int]:
        pw = lambda w: w * 1.03 + 4     # noqa: E731 — a doboz kis ráhagyással (a builder betűi ne törjenek)
        w1 = _wrap(n, nz, 1e9)[2]
        wn, lines, ws = w1, 1, n.split()
        if pw(w1) > cap and len(ws) > 1:
            wn, lines = min(max(_wrap(" ".join(ws[:i]), nz, 1e9)[2], _wrap(" ".join(ws[i:]), nz, 1e9)[2])
                            for i in range(1, len(ws))), 2
        return pw(max(wn, _wrap(fv(g["total"][n]), vz, 1e9)[2])), (lines * nz + vz) * 1.22, lines

    # balról jobbra: a szélső oszlopok címkéje legfeljebb ekkora sávot foglal (kevés oszlopnál több is jut neki)
    capo = max(0.24 * (d1 - d0), ((d1 - d0) - (C - 1) * m * 0.35 - C * nw) / 2)
    L = {n: label(n, max(size[n] + gap, m * 0.2) if down else capo) for n in real}
    ld = lambda n: L[n][1] if down else L[n][0]    # noqa: E731 — a címke kiterjedése a folyás irányában
    lb = lambda n: L[n][0] if down else L[n][1]    # noqa: E731 — és rá merőlegesen
    first, last = [n for n in cols[0] if n in L], [n for n in cols[-1] if n in L]
    inner = [n for n in real if 0 < col[n] < C - 1]
    room_b = (b1 - b0)
    # fentről lefelé egy sor címkéi egymás mellett vannak: ha elférnek, egysoros nevek (kevesebb helyet visznek el a
    # folyás irányában), ha nem, tördelve; a szélső sorban végül két lépcsőben (minden második a távolabbi lépcsőbe)
    # — így nem kell kisebb betű
    tier = dict.fromkeys(real, 0)

    def spread(grp: list[str]) -> dict[str, float]:
        return dict(zip(grp, _spread_sized([mid(n) for n in grp], [lb(n) for n in grp], lpad, b0, b1)))

    def leaders_clear(grp: list[str]) -> bool:
        """A távolabbi lépcső vezetővonala (a csomóponttól a címkéig) ne fusson át a közelebbi lépcső címkéjén."""
        c0, c1 = spread([n for n in grp if not tier[n]]), spread([n for n in grp if tier[n]])
        return not any(min(mid(n), c1[n]) - pad < c0[o] + lb(o) / 2 and c0[o] - lb(o) / 2 < max(mid(n), c1[n]) + pad
                       for n in c1 for o in c0)
    if down:
        for grp in [first, last] + [[n for n in cols[c] if n in L] for c in range(1, C - 1)]:
            srt = sorted(grp, key=mid)
            for wrap, tiers in ((False, 1), (True, 1)) + (((False, 2), (True, 2)) if grp in (first, last) else ()):
                for n in grp:
                    L[n] = label(n, max(size[n] + gap, m * 0.2) if wrap else 1e9)
                    tier[n] = srt.index(n) % tiers
                if all(sum(lb(n) + lpad for n in grp if tier[n] == t) - lpad <= room_b for t in range(tiers)) \
                        and (tiers == 1 or wrap or leaders_clear(grp)):
                    break
    near = {id(grp): max([ld(n) for n in grp if not tier[n]] or [0]) for grp in (first, last)}
    off = {n: (near[id(first)] if n in first else near[id(last)]) + lpad if tier[n] else 0.0 for n in real}
    mb = max(ld(n) + off[n] for n in first) + pad
    ma = max(ld(n) + off[n] for n in last) + pad
    span = (d1 - d0) - mb - ma - nw
    lead = max(0.0, span - (C - 1) * m * 0.6) / 2      # két-három oszlopnál se nyúljon végtelenre: középre, ráhagyással
    span -= 2 * lead
    S_in = span / (C - 1) - nw          # két oszlop közti szabad hossz (ebben futnak a szalagok)
    if not down:
        for n in inner:
            L[n] = label(n, min(capo, S_in - 2 * pad))
    # mennyire férnek el a feliratok f-nél? (1 = elférnek; kisebb = ennyivel kisebb betű kell)
    fit = 1.0
    if not down:
        for n in first + last:
            fit = min(fit, capo / L[n][0])
    for n in inner:
        fit = min(fit, (S_in - 2 * pad) / ld(n))
    minsp = m * 0.12                    # a szalagnak hossz kell, hogy látsszon az íve
    fit = min(fit, ((d1 - d0) - nw - (C - 1) * (minsp + nw)) / (mb + ma))
    groups_b = [[n for n in grp if tier[n] == t] for grp in (first, last) for t in (0, 1)]
    for grp in groups_b + [[n for n in cols[c] if n in L] for c in range(1, C - 1)]:
        if grp:
            fit = min(fit, (room_b - (len(grp) - 1) * lpad) / sum(lb(n) for n in grp))
    if not final:
        return {"fit": fit, "f": f, "down": down}

    # 3) szalagok: a csomóponton kifelé a cél, befelé a forrás helye szerint egymás alatt (így nem keresztezik egymást)
    dpos = [d0 + lead + mb + span * c / (C - 1) for c in range(C)]
    so, si = {}, {}
    for x in val:
        sc = size[x] / (val[x] or 1)
        for key, slot, other in ((2, so, 3), (3, si, 2)):
            y = top[x]
            for e in sorted([e for e in edges if e[key] == x], key=lambda e: (mid(e[other]), e[0])):
                slot[(e[0], e[1])] = (y, y + e[4] * sc)
                y += e[4] * sc

    def bez(da: float, ya: float, db: float, yb: float, t: float) -> tuple[float, float]:   # S-ív: vízszintes érintők
        u, dm = 1 - t, (da + db) / 2
        return u ** 3 * da + 3 * u * t * dm + t ** 3 * db, (u ** 3 + 3 * u * u * t) * ya + (3 * u * t * t + t ** 3) * yb
    ts = [j / 15 for j in range(16)]
    ribbons = []
    for i, s in enumerate(segs):
        up, lo = [], []
        for j, (a, b, w) in enumerate(s):
            da, db = dpos[col[a]] + nw, dpos[col[b]]
            (ya0, ya1), (yb0, yb1) = so[(i, j)], si[(i, j)]
            up += [bez(da, ya0, db, yb0, t) for t in ts]
            lo += [bez(da, ya1, db, yb1, t) for t in ts]
        ribbons.append(up + lo[::-1])

    # 4) a címke oldala: a szélső oszlopé kívül, a belsőé ott, ahol kevesebb szalagot és címkét takar
    side = {**{n: "before" for n in first}, **{n: "after" for n in last}}

    def lbox(n: str, s: str, bc: Optional[float] = None) -> tuple[float, float, float, float]:
        bc = mid(n) if bc is None else bc
        e0 = dpos[col[n]] - pad - off[n] - ld(n) if s == "before" else dpos[col[n]] + nw + pad + off[n]
        return e0, bc - lb(n) / 2, e0 + ld(n), bc + lb(n) / 2
    rbb = [(min(p[0] for p in r), min(p[1] for p in r), max(p[0] for p in r), max(p[1] for p in r)) for r in ribbons]

    def inside(px: float, py: float, poly: list[tuple[float, float]]) -> bool:
        hit = False
        for (xa, ya), (xb, yb) in zip(poly, poly[1:] + poly[:1]):
            if (ya > py) != (yb > py) and px < xa + (py - ya) * (xb - xa) / (yb - ya):
                hit = not hit
        return hit

    def cover(bx: tuple[float, float, float, float]) -> float:
        pts = [(bx[0] + (bx[2] - bx[0]) * (i + 0.5) / 5, bx[1] + (bx[3] - bx[1]) * (j + 0.5) / 3) for i in range(5) for j in range(3)]
        return sum(any(bb[0] <= px <= bb[2] and bb[1] <= py <= bb[3] and inside(px, py, r) for r, bb in zip(ribbons, rbb))
                   for px, py in pts) / len(pts)
    placed = [lbox(n, side[n]) for n in side]
    for c in range(1, C - 1):
        for n in [x for x in cols[c] if x in L]:
            def cost(s: str) -> tuple:
                bx = lbox(n, s)
                hits = sum(1 for o in placed if bx[0] < o[2] and o[0] < bx[2] and bx[1] < o[3] and o[1] < bx[3])
                return hits, round(cover(bx), 2), s != "after"
            side[n] = min(("after", "before"), key=cost)
            placed.append(lbox(n, side[n]))
    # 5) ütköző címkék szétnyomva: ami a folyás irányában átfedi egymást, egy csoport, együtt húzódik szét
    boxes = {n: lbox(n, side[n]) for n in real}
    parent = {n: n for n in real}

    def root(n: str) -> str:
        while parent[n] != n:
            n = parent[n]
        return n
    for i, a in enumerate(real):
        for b in real[i + 1:]:
            if boxes[a][0] < boxes[b][2] and boxes[b][0] < boxes[a][2]:
                parent[root(a)] = root(b)
    groups: dict[str, list[str]] = {}
    for n in real:
        groups.setdefault(root(n), []).append(n)
    for grp in groups.values():
        for n, c_ in zip(grp, _spread_sized([mid(n) for n in grp], [lb(n) for n in grp], lpad, b0, b1)):
            boxes[n] = lbox(n, side[n], c_)
    P = (lambda d, b: (b, d)) if down else (lambda d, b: (d, b))    # noqa: E731 — absztrakt → képernyő
    nodes = {}
    for n in real:
        (x0_, y0_), (x1_, y1_) = P(dpos[col[n]], top[n]), P(dpos[col[n]] + nw, top[n] + size[n])
        nodes[n] = {"pos": ((x0_ + x1_) / 2, (y0_ + y1_) / 2), "size": (x1_ - x0_, y1_ - y0_), "col": col[n]}
    labels = {}
    for n in real:
        e0, c0, e1, c1 = boxes[n]
        (x0_, y0_), (x1_, y1_) = P(e0, c0), P(e1, c1)
        labels[n] = {"box": (x0_, y0_, x1_, y1_), "lines": L[n][2], "inner": n in inner, "tier": tier[n], "side": side[n],
                     "align": "center" if down else ("right" if side[n] == "before" else "left")}
    return {"fit": fit, "f": f, "down": down, "cols": C, "nodes": nodes, "labels": labels, "nz": nz, "vz": vz,
            "ribbons": [{"from": fl["from"], "to": fl["to"], "col": col[fl["from"]], "pts": [P(d, b) for d, b in r]}
                        for fl, r in zip(ch["flows"], ribbons)]}


def _sankey_layout(ch: dict[str, Any], W: int, H: int, fv: Callable[[float], str]) -> dict[str, Any]:
    """Melyik irányban folyjon? Amelyikben nagyobb betűvel fér el minden felirat (mérve, mindkét irányban); ha
    egyformán, akkor a képkocka hosszabb oldala mentén (hosszabb, laposabb szalagok) — állóban jellemzően lefelé."""
    best = None
    for down in (H > W, H <= W):
        f = 1.0
        for _ in range(4):
            fit = _sankey_plan(ch, W, H, down, f, fv, final=False)["fit"]
            if fit >= 0.995 or f <= 0.6:
                break
            f = max(0.6, f * fit * 0.99)
        if best is None or f > best[0] + 0.02:
            best = (f, down)
    return _sankey_plan(ch, W, H, best[1], best[0], fv)


def waterfall_steps(ch: dict[str, Any]) -> list[tuple[str, float, float, str]]:
    """A vízesés oszlopai: (címke, honnan, hová, total | up | down) — a kezdő és a (kiszámolt) záró érték 0-ról indul.
    A videó és a pakli (pptx_charts) ugyanebből rajzol."""
    items = ch["items"]
    steps = [(items[0]["label"], 0.0, items[0]["value"], "total")]
    run = items[0]["value"]
    for it in items[1:]:
        steps.append((it["label"], run, run + it["value"], "up" if it["value"] >= 0 else "down"))
        run += it["value"]
    steps.append((ch.get("total_label") or "", 0.0, run, "total"))
    return steps


# ---------- rajz: a builder elemei ----------

def elements(ch: dict[str, Any], W: int, H: int, lang: str, accent: list[float], dur: Optional[float] = None) -> list[dict[str, Any]]:
    from app.tools.video import _decimals, _fit_size, _fmt_fixed, _fmt_val
    m, vertical = min(W, H), H > W * 1.2
    unit, typ = ch.get("unit") or "", ch["type"]
    x0, x1 = W * 0.09, W * 0.91
    yT, yB = H * (0.2 if vertical else 0.22), H * (0.66 if vertical else 0.7)
    vl, cl = round(m * 0.034), round(m * (0.031 if not vertical else 0.033))
    out: list[dict[str, Any]] = []
    # egy ábrán belül egyforma tizedes (ne álljon „80 %” mellett „6,0 %”), a forrás pontosságával
    allnums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", " ".join(numbers(ch)))] if typ != "scatter" else []
    dec = max([_decimals(v) for v in allnums] + [0])
    fv = lambda v: _fmt_fixed(v, dec, unit, lang)   # noqa: E731
    cols_n = lambda n: [accent] + [[round(int(h[i:i + 2], 16) / 255, 4) for i in (1, 3, 5)]   # noqa: E731
                                   for h in _distinct(n)][1:]

    def rect(name, size, pos, **o):
        out.append({"k": "rect", "name": name, "size": [round(size[0], 1), round(max(size[1], 2), 1)],
                    "pos": [round(pos[0], 1), round(pos[1], 1)], **o})

    def text(name, st, size, box, pos, **o):
        out.append({"k": "text", "name": name, "str": st, "size": _fit_size(st, size, box[0], box[1], name),
                    "box": [round(box[0], 1), round(box[1], 1)], "pos": [round(pos[0], 1), round(pos[1], 1)], **o})

    def path(name, pts, **o):
        out.append({"k": "path", "name": name, "points": [[round(x, 1), round(y, 1)] for x, y in pts], **o})

    def seg(name, p, q, color, th, t0, d, opacity=100):
        dx, dy = q[0] - p[0], q[1] - p[1]
        out.append({"k": "rect", "name": name, "size": [round(math.hypot(dx, dy), 1), round(th, 1)], "pos": [round(p[0], 1), round(p[1], 1)],
                    "rot": round(math.degrees(math.atan2(dy, dx)), 2), "color": color, "opacity": opacity, "grow": "segment",
                    "t0": round(t0, 3), "dur": round(d, 3)})

    def dot(name, p, d, color, t0):
        rect(name, (d, d), p, color=color, opacity=100, round=d / 2, grow="pop", t0=round(t0, 3), dur=0.25)

    th = max(4, m * 0.006)
    if typ == "waterfall":
        steps = waterfall_steps(ch)
        lo = min(0.0, *[min(a, b) for _, a, b, _ in steps])
        hi = max(0.0, *[max(a, b) for _, a, b, _ in steps])
        top, bottom = yT + vl * 1.8, yB - (vl * 1.8 if lo < 0 else 0)
        sc = (bottom - top) / ((hi - lo) or 1)
        Y = lambda v: top + (hi - v) * sc   # noqa: E731
        n = len(steps)
        slot = (x1 - x0) / n
        bw = min(slot * 0.62, m * 0.15)
        pace = min(0.55, max(0.3, ((dur or 8) - 2.2) / n))
        rect("Baseline", (x1 - x0, max(2, m * 0.003)), ((x0 + x1) / 2, Y(0)), color=[1, 1, 1], opacity=45, t0=0.1, dur=0.4, grow="right")
        for k, (lab, a, b, kind) in enumerate(steps):
            cx, t0 = x0 + slot * (k + 0.5), 0.35 + k * pace
            ya, yb = Y(a), Y(b)
            col = LIGHT if kind == "total" else ("accent" if kind == "up" else RED)
            up = yb <= ya
            rect(f"Bar – {lab or k + 1}", (bw, abs(ya - yb)), (cx, (ya + yb) / 2), color=col, opacity=92 if kind == "total" else 100,
                 round=min(bw * 0.06, 8), grow="up" if up else "down", t0=round(t0, 3), dur=round(pace * 0.9, 3))
            val = fv(b) if kind == "total" else ("+" if b >= a else "−") + fv(abs(b - a))
            text(f"Value – {lab or k + 1}", val, vl, (slot * 1.1, vl * 1.4),
                 (cx, (min(ya, yb) - vl * 0.75) if (up or kind == "total") else (max(ya, yb) + vl * 0.95)), align="center", font="bold",
                 color=None if kind == "total" else (accent if kind == "up" else RED), t0=round(t0 + pace * 0.7, 3))
            text(f"Label – {lab or k + 1}", lab, cl, (slot * 0.98, cl * 2.7), (cx, yB + cl * 2.0), align="center", font="semibold",
                 color=SOFT, t0=0.2)
            if k < n - 1:                       # összekötő a következő oszlop induló szintjén
                rect(f"Link {k + 1}", (slot - bw, max(2, m * 0.0025)), (cx + slot / 2, yb), color=[1, 1, 1], opacity=40, grow="right",
                     t0=round(t0 + pace * 0.85, 3), dur=0.2)
    elif typ == "slope":
        items, labs = ch["items"], ch.get("labels") or ["", ""]
        vals = [v for it in items for v in (it["from"], it["to"])]
        lo, hi = min(vals), max(vals)
        pad = (hi - lo) * 0.08 or abs(hi) * 0.1 or 1
        lo, hi = lo - pad, hi + pad
        top, bottom = yT + cl * 2.6, yB
        Y = lambda v: bottom - (v - lo) / (hi - lo) * (bottom - top)   # noqa: E731
        # a két tengely helye a feliratokból: balra a név + a kezdőérték, jobbra a végérték fér el (állóban is)
        val_w = max(len(fv(v)) for v in vals) * 0.56 * vl + vl * 0.5
        name_w = min(max(len(it["label"]) for it in items) * 0.56 * cl + cl * 0.5, (x1 - x0) * 0.34)
        xa, xb = x0 + name_w + val_w + vl * 0.9, x1 - val_w - vl * 0.9
        for nm, x, lab in (("Axis A", xa, labs[0]), ("Axis B", xb, labs[1])):
            rect(nm, (max(2, m * 0.003), bottom - top), (x, (top + bottom) / 2), color=[1, 1, 1], opacity=30, t0=0.1, dur=0.4, grow="up")
            text(f"Heading – {lab}", lab, round(cl * 1.15), (m * 0.3, cl * 1.6), (x, top - cl * 1.4), align="center", font="bold", t0=0.15)
        gap = vl * 1.25
        la = _spread([Y(it["from"]) for it in items], gap, top, bottom + vl)
        lb = _spread([Y(it["to"]) for it in items], gap, top, bottom + vl)
        pace = min(0.6, max(0.3, ((dur or 8) - 2.2) / max(len(items), 1)))
        for k, it in enumerate(items):
            t0 = 0.45 + k * pace
            pa, pb = (xa, Y(it["from"])), (xb, Y(it["to"]))
            col = accent if it["to"] >= it["from"] else RED
            dot(f"From – {it['label']}", pa, max(10, m * 0.016), col, t0)
            seg(f"Change – {it['label']}", pa, pb, col, th, t0 + 0.15, pace * 1.2)
            dot(f"To – {it['label']}", pb, max(10, m * 0.016), col, t0 + 0.15 + pace * 1.2)
            text(f"Name – {it['label']}", it["label"], cl, (name_w, cl * 1.4), (x0 + name_w / 2, la[k]),
                 align="right", font="semibold", color=SOFT, t0=round(t0, 3))
            text(f"From value – {it['label']}", fv(it["from"]), vl, (val_w, vl * 1.4), (xa - vl * 0.6 - val_w / 2, la[k]), align="right",
                 font="bold", t0=round(t0, 3))
            text(f"To value – {it['label']}", fv(it["to"]), vl, (val_w, vl * 1.4), (xb + vl * 0.6 + val_w / 2, lb[k]),
                 align="left", font="bold", color=col, t0=round(t0 + 0.2 + pace * 1.2, 3))
    elif typ == "scatter":
        items = ch["items"]
        px0, px1 = x0 + m * 0.07, x1 - m * 0.01
        py0, py1 = yT + cl * 1.8, yB - cl * 0.6
        xl, xh, xs = _nice(min(i["x"] for i in items), max(i["x"] for i in items))
        yl, yh, ys = _nice(min(i["y"] for i in items), max(i["y"] for i in items))
        X = lambda v: px0 + (v - xl) / ((xh - xl) or 1) * (px1 - px0)   # noqa: E731
        Y = lambda v: py1 - (v - yl) / ((yh - yl) or 1) * (py1 - py0)   # noqa: E731
        rect("X axis", (px1 - px0, max(2, m * 0.003)), ((px0 + px1) / 2, py1), color=[1, 1, 1], opacity=45, t0=0.1, dur=0.4, grow="right")
        rect("Y axis", (max(2, m * 0.003), py1 - py0), (px0, (py0 + py1) / 2), color=[1, 1, 1], opacity=45, t0=0.1, dur=0.4, grow="up")
        tick = round(m * 0.026)
        v = yl + ys
        while v <= yh + 1e-9:
            rect(f"Grid y {v:g}", (px1 - px0, max(1, m * 0.0015)), ((px0 + px1) / 2, Y(v)), color=[1, 1, 1], opacity=12, t0=0.2, dur=0.3, grow="fade")
            text(f"Tick y {v:g}", _fmt_val(v, "", lang), tick, (m * 0.07, tick * 1.4), (px0 - m * 0.04, Y(v)), align="right",
                 font="regular", color=SOFT, t0=0.2)
            v += ys
        v = xl
        while v <= xh + 1e-9:
            text(f"Tick x {v:g}", _fmt_val(v, "", lang), tick, (m * 0.1, tick * 1.4), (X(v), py1 + tick * 1.0), align="center",
                 font="regular", color=SOFT, t0=0.2)
            v += xs
        if ch.get("x_title"):
            text("X title", ch["x_title"], cl, ((px1 - px0), cl * 1.4), ((px0 + px1) / 2, py1 + tick * 2.4), align="center",
                 font="semibold", color=SOFT, t0=0.25)
        if ch.get("y_title"):
            text("Y title", "↑ " + ch["y_title"], cl, ((px1 - px0) * 0.7, cl * 1.4), (px0 + (px1 - px0) * 0.35 - m * 0.02, py0 - cl * 1.3),
                 align="left", font="semibold", color=SOFT, t0=0.25)
        smax = max((i.get("size") or 0) for i in items)
        dmin, dmax = max(12, m * 0.018), m * 0.09
        order = sorted(range(len(items)), key=lambda i: items[i]["x"])
        P = [(X(i["x"]), Y(i["y"])) for i in items]
        D = [dmin + (dmax - dmin) * math.sqrt(i["size"] / smax) if smax and i.get("size") else dmin for i in items]
        sides = label_sides([P[i] for i in order], [items[i]["label"] for i in order], (px0, py0 - cl, px1, py1),
                            0.56 * cl, cl * 1.3, dot=max(D) / 2 if smax else dmin / 2)
        pace = min(0.3, max(0.12, ((dur or 8) - 2.4) / len(items)))
        for rank, i in enumerate(order):
            it, (px, py), d, t0 = items[i], P[i], D[i], 0.5 + rank * pace
            rect(f"Point – {it['label'] or i + 1}", (d, d), (px, py), color="accent", opacity=85 if smax else 100, round=d / 2,
                 grow="pop", t0=round(t0, 3), dur=0.3)
            if it["label"]:
                w = len(it["label"]) * 0.56 * cl + 6
                side, g = sides[rank], d / 2 + 6
                pos = {"right": (px + g + w / 2, py), "left": (px - g - w / 2, py), "above": (px, py - g - cl * 0.6),
                       "below": (px, py + g + cl * 0.7)}[side]
                text(f"Name – {it['label']}", it["label"], cl, (w + 4, cl * 1.4), pos, align="center", font="semibold",
                     t0=round(t0 + 0.15, 3))
    elif typ in ("band", "lines"):
        labs = [i["label"] for i in ch["items"]] if typ == "band" else ch["labels"]
        series = [[i["value"] for i in ch["items"]]] if typ == "band" else [s["values"] for s in ch["items"]]
        names = [""] if typ == "band" else [s["label"] for s in ch["items"]]
        allv = [v for s in series for v in s] + [v for i in ch["items"] if typ == "band" for v in (i.get("low"), i.get("high")) if v is not None]
        lo, hi = min(allv), max(allv)
        pad = (hi - lo) * 0.1 or abs(hi) * 0.1 or 1
        lo, hi = (0.0 if lo >= 0 and lo - pad < 0 else lo - pad), hi + pad
        n = len(labs)
        name_w = 0 if typ == "band" else min((x1 - x0) * 0.3, max(len(s) for s in names) * 0.56 * cl + vl * 4)
        px0, px1 = x0 + m * 0.02, x1 - name_w - m * 0.02
        top, bottom = yT + vl * 1.6, yB
        X = lambda k: px0 + (px1 - px0) * k / max(n - 1, 1)   # noqa: E731
        Y = lambda v: bottom - (v - lo) / ((hi - lo) or 1) * (bottom - top)   # noqa: E731
        rect("Baseline", (px1 - px0, max(2, m * 0.003)), ((px0 + px1) / 2, bottom), color=[1, 1, 1], opacity=45, t0=0.1, dur=0.4, grow="right")
        every = 1 if n <= 8 else math.ceil(n / 8)
        for k, lab in enumerate(labs):
            if k % every == 0 or k == n - 1:
                text(f"Label – {lab}", lab, cl, ((px1 - px0) / min(n, 8), cl * 1.6), (X(k), bottom + cl * 1.4), align="center",
                     font="semibold", color=SOFT, t0=0.2)
        draw = min(1.6, max(0.8, ((dur or 8) - 2.5) / len(series)))
        cols = [accent, LIGHT] + cols_n(4)[1:3]
        if typ == "band":
            it = ch["items"]
            ks = [k for k, i in enumerate(it) if i.get("low") is not None]
            ff = ch.get("forecast_from")
            kf = labs.index(ff) if ff in labs else n          # az első előrejelzett időszak
            if ks:                              # a sáv a vonal mögött: felső él balról jobbra, alsó vissza
                k0, k1 = ks[0], ks[-1]
                if k0 > 0 and k0 >= kf:         # az utolsó mért pontból indul (nem a semmiből)
                    k0 -= 1
                upper = [(X(k), Y(it[k].get("high", it[k]["value"]))) for k in range(k0, k1 + 1)]
                lower = [(X(k), Y(it[k].get("low", it[k]["value"]))) for k in range(k1, k0 - 1, -1)]
                path("Range band", upper + lower, closed=True, fill="accent", opacity=26, anim="appear", t0=0.5 + draw * k0 / max(n - 1, 1), dur=0.8)
                rng = it[k1]
                text("Range", f"{_fmt_fixed(rng['low'], dec, '', lang)}–{fv(rng['high'])}", round(cl * 0.95), (m * 0.32, cl * 1.4),
                     (min(X(k1), x1 - m * 0.16), Y(rng["high"]) - cl * 1.1), align="center", font="semibold", color=SOFT,
                     t0=round(0.9 + draw, 3))
            last = max(0, min(kf, n) - 1)       # az utolsó mért pont: itt a választóvonal, innen szaggatott
            if kf < n:
                rect("Forecast line", (max(2, m * 0.0025), bottom - top), (X(last), (top + bottom) / 2), color=[1, 1, 1], opacity=30,
                     t0=0.3, dur=0.4, grow="up")
                text("Forecast", "előrejelzés" if lang == "hu" else "forecast", round(cl * 0.9), (m * 0.3, cl * 1.4),
                     (X(last) + m * 0.16, top - cl * 0.2), align="left", font="semibold", color=SOFT, t0=0.4)
            vals = series[0]
            solid = [(X(k), Y(v)) for k, v in enumerate(vals[:last + 1])]
            if len(solid) >= 2:
                path("Line", solid, stroke="accent", stroke_w=th, anim="draw", t0=0.5, dur=round(draw * (len(solid) - 1) / max(n - 1, 1), 3))
            if last < n - 1:
                t_f = 0.5 + draw * last / max(n - 1, 1)
                path("Forecast", [(X(k), Y(vals[k])) for k in range(last, n)], stroke="accent", stroke_w=th, dash=[th * 2.5, th * 1.8],
                     anim="draw", t0=round(t_f, 3), dur=round(draw * (n - 1 - last) / max(n - 1, 1), 3))
            for k in sorted({0, n - 1, last}):
                dot(f"Point – {labs[k]}", (X(k), Y(vals[k])), max(10, m * 0.016), "accent", 0.5 + draw * k / max(n - 1, 1))
                text(f"Value – {labs[k]}", fv(vals[k]), vl, (vl * 5, vl * 1.4), (X(k), Y(vals[k]) - vl * 1.0), align="center",
                     font="bold", t0=round(0.6 + draw * k / max(n - 1, 1), 3))
        else:
            ends = _spread([Y(s[-1]) for s in series], cl * 1.3, top - cl, bottom)
            for j, (vals, nm) in enumerate(zip(series, names)):
                t0, col = 0.45 + j * draw * 0.75, cols[j % len(cols)]
                path(f"Line – {nm}", [(X(k), Y(v)) for k, v in enumerate(vals)], stroke=col, stroke_w=th, anim="draw", t0=round(t0, 3),
                     dur=round(draw, 3))
                dot(f"End – {nm}", (X(n - 1), Y(vals[-1])), max(10, m * 0.016), col, t0 + draw)
                text(f"Name – {nm}", f"{nm}  {fv(vals[-1])}", cl, (name_w, cl * 1.4), (px1 + m * 0.02 + name_w / 2, ends[j]),
                     align="left", font="bold", color=col, t0=round(t0 + draw, 3))
    elif typ == "race":
        labs, items = ch["labels"], ch["items"]
        P, N = len(labs), len(items)
        K = min(N, 8)
        lab_w = (x1 - x0) * (0.26 if not vertical else 0.3)
        bx0, bx1 = x0 + lab_w + m * 0.015, x1 - vl * 4.6
        row = (yB - yT) / K
        bh = min(row * 0.7, m * 0.07)
        T = min(2.0, max(0.8, ((dur or 12) - 2.5) / P))
        ts = [0.6 + j * T for j in range(P)]   # ekkor ÉRKEZIK meg a j. időszak állapota
        hold, move = T * 0.55, T * 0.45         # megáll (ekkor látszanak a számai), aztán átrendeződik a következőre
        cols = cols_n(N)
        ranks = []
        for j in range(P):
            order = sorted(range(N), key=lambda i: -items[i]["values"][j])
            ranks.append({i: r for r, i in enumerate(order)})
        for i, it in enumerate(items):
            keys_b, keys_n = [], []
            for j in range(P):
                v, r = items[i]["values"][j], ranks[j][i]
                vmax = max(x["values"][j] for x in items) or 1
                w = max(2.0, (bx1 - bx0) * max(v, 0) / vmax)
                y = yT + row * (min(r, K) + 0.5)
                op = 100 if r < K else 0
                for t_ in ((ts[j], ts[j] + hold) if j < P - 1 else (ts[j],)):
                    keys_b.append([round(t_, 3), round(bx0, 1), round(y, 1), round(w, 1), op])
                    keys_n.append([round(t_, 3), round(x0 + lab_w / 2, 1), round(y, 1), None, op])
                text(f"Value – {it['label']} {labs[j]}", fv(v), vl, (vl * 4.4, vl * 1.4), (bx0 + w + vl * 2.4, y), align="center",
                     font="bold", t0=round(ts[j], 3), **({"t1": round(ts[j] + hold, 3)} if j < P - 1 else {}),
                     cut=True, **({} if op else {"hidden": True}))
            w0 = keys_b[0][3]
            out.append({"k": "rect", "name": f"Bar – {it['label']}", "size": [round(w0, 1), round(bh, 1)],
                        "pos": [round(bx0 + w0 / 2, 1), keys_b[0][2]], "color": cols[i], "opacity": 100, "round": round(min(bh * 0.12, 8), 1),
                        "keys": [[round(ts[0] - 0.4, 3), keys_b[0][1], keys_b[0][2], 2.0, 0]] + keys_b})
            out.append({"k": "text", "name": f"Name – {it['label']}", "str": it["label"],
                        "size": _fit_size(it["label"], cl, lab_w, cl * 1.4, it["label"]), "box": [round(lab_w, 1), round(cl * 1.4, 1)],
                        "pos": [keys_n[0][1], keys_n[0][2]], "align": "right", "font": "semibold", "color": SOFT,
                        "keys": [[round(ts[0] - 0.4, 3), keys_n[0][1], keys_n[0][2], None, 0]] + keys_n})
        big = round(m * 0.09)
        for j, lab in enumerate(labs):
            el = {"k": "text", "name": f"Period {lab}", "str": lab, "size": big, "box": [round(m * 0.5, 1), round(big * 1.3, 1)],
                  "pos": [round(x1 - m * 0.25, 1), round(yB - big * 0.2, 1)], "align": "right", "font": "bold", "color": SOFT,
                  "t0": round(ts[j] - (move if j else 0.6), 3), "cut": True}   # az évszám az átrendeződéssel együtt vált
            if j < P - 1:
                el["t1"] = round(ts[j + 1] - move, 3)
            out.append(el)
        # a rangsoron kívüli tétel értéke nem kell (a „hidden” jelzés csak a fenti szűréshez)
        out[:] = [e for e in out if not e.pop("hidden", False)]
    elif typ == "sankey":
        sdec = max(_decimals(fl["value"]) for fl in ch["flows"])     # a folyamok pontossága (a nevekben lévő szám nem számít)
        fs = lambda v: _fmt_fixed(v, sdec, unit, lang)   # noqa: E731
        p = _sankey_layout(ch, W, H, fs)
        hint = {n["name"]: n.get("color") for n in ch.get("nodes") or []}
        g = _sankey_graph(ch["flows"], list(hint))
        names = g["names"]
        # a megadott szín marad; a többi sorban a palettából (az első az akcentus) — a csomópontok sorrendjéből, nem a
        # rajzéból, így minden formátumban ugyanaz. A szalag a forrása színét viszi: a legjobban elváló (első) színek a
        # kifolyós csomópontoké, a végpontok a maradékot kapják
        free = iter(cols_n(len(names)))
        srcs = {fl["from"] for fl in ch["flows"]}
        colour = {n: [round(int(hint[n][i:i + 2], 16) / 255, 4) for i in (1, 3, 5)] if hint.get(n) else next(free)
                  for n in sorted(names, key=lambda n: n not in srcs)}
        C = p["cols"]
        # oszloponként balról jobbra (fentről lefelé) épül; az utolsó oszlop is a jelenet vége előtt ~1,5 s-mal áll össze
        pace = min(1.3, max(0.35, ((dur or 8) - 2.2) / max(C - 1, 1)))
        tc = [0.45 + c * pace for c in range(C)]
        seen: dict[int, int] = {}
        for rb in sorted(p["ribbons"], key=lambda r: r["col"]):   # a szalag a forrása után jelenik meg, a cél előtt
            j = seen[rb["col"]] = seen.get(rb["col"], -1) + 1
            path(f"Flow – {rb['from']} → {rb['to']}", rb["pts"], closed=True, fill=colour[rb["from"]], opacity=45, anim="appear",
                 t0=round(tc[rb["col"]] + 0.3 + min(0.3, j * 0.05), 3), dur=0.6)
        for n in names:
            nd = p["nodes"][n]
            rect(f"Node – {n}", nd["size"], nd["pos"], color=colour[n], opacity=100, round=round(min(nd["size"]) * 0.2, 1),
                 grow="right" if p["down"] else "down", t0=round(tc[nd["col"]], 3), dur=0.45)
        nz, vz = round(p["nz"]), round(p["vz"])
        for n in names:                     # a távolabbi lépcső címkéje vékony vezetővonallal a csomópontjához
            lab, nd = p["labels"][n], p["nodes"][n]
            if lab["tier"]:
                bx0, by0, bx1, by1 = lab["box"]
                face = nd["pos"][1] + nd["size"][1] / 2 * (1 if lab["side"] == "after" else -1)
                path(f"Leader – {n}", [(nd["pos"][0], face + m * 0.006 * (1 if lab["side"] == "after" else -1)),
                                       ((bx0 + bx1) / 2, by0 - m * 0.004 if lab["side"] == "after" else by1 + m * 0.004)],
                     stroke=colour[n], stroke_w=max(2, round(m * 0.0025)), opacity=70, anim="draw",
                     t0=round(tc[nd["col"]] + 0.2, 3), dur=0.35)
        for n in names:
            lab, t0 = p["labels"][n], round(tc[p["nodes"][n]["col"]] + 0.2, 3)
            bx0, by0, bx1, by1 = lab["box"]
            w, nh, cx = bx1 - bx0, lab["lines"] * nz * 1.22, (bx0 + bx1) / 2
            over = {"shadow": 14} if lab["inner"] else {}       # a belső címke szalagon ül: árnyékkal olvasható
            text(f"Name – {n}", n, nz, (w, nh), (cx, by0 + nh / 2), align=lab["align"], font="semibold", color=SOFT, t0=t0, **over)
            text(f"Value – {n}", fs(g["total"][n]), vz, (w, vz * 1.22), (cx, by0 + nh + vz * 0.61),
                 align=lab["align"], font="bold", color=None if lab["inner"] else colour[n], t0=t0, **over)
    return out


def _distinct(n: int) -> list[str]:
    """Jól megkülönböztethető színek (a magyarázó ábrák szereplő-palettája)."""
    from app.tools import diagrams
    return diagrams.palette(None, max(n, 2))


def seconds_needed(ch: dict[str, Any]) -> Optional[float]:
    """A verseny időszakonként ~1,2 s-ot kér — a jelenet ennyi legyen (különben a builder sűríti)."""
    if ch.get("type") == "race":
        return round(0.6 + len(ch.get("labels") or []) * 1.2 + 1.5, 1)
    return None
