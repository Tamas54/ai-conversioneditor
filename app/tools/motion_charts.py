"""Újabb mozgó grafikonok a videóhoz (10-17): vízesés, előtte → utána, pont-/buborékdiagram, sáv-előrejelzés, több
vonal (forgatókönyvek), rangsor-verseny. A ChatGPT-értékelés hiánylistájáról (változás-nézet, bizonytalansági sáv,
forgatókönyv-összevetés, pontdiagram) és a mozgógrafika klasszikusaiból (vízesés, „bar race”).

Ugyanaz a szabály, mint a többi ábránál (video._chart_from_plan): minden szám és dátum a FORRÁSBÓL — ami nincs benne,
attól az ábra kimarad, és a check megmondja, miért. A kiszámolt érték (a vízesés vége) kiszámolt: ha a forrás mást
mond, az a check-ben van. A rajz a meglévő elemkészlet (rect / text / path), a builder ugyanúgy rakja le; a versenyhez
a kulcskockás mozgás (keys: [[t, x, y, szélesség?, átlátszóság?]]) új a builderben.
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable, Optional

TYPES = ("waterfall", "slope", "scatter", "band", "lines", "race")
ALIASES = {"bridge": "waterfall", "before_after": "slope", "change": "slope", "bubble": "scatter", "forecast": "band",
           "range": "band", "uncertainty": "band", "scenarios": "lines", "multiline": "lines", "bar_race": "race",
           "racing_bars": "race", "ranking": "race"}
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
    return None, None


def numbers(ch: dict[str, Any]) -> list[str]:
    """A képernyőn megjelenő számok és címkék szövegként (a lefedettség-méréshez)."""
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
