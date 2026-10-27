"""Facecam layout (Kommandant 10-26: "ezt viszont kéne"): the content fills the frame, the speaker sits in a small window
in a corner — a circle, a rounded or a plain rectangle — the classic YouTube screen-recording layout.

It is a PLACEMENT on a content layer, not a layer of its own: `speaker: {…}` on a `fullscreen` layer (a video — an upload,
a screen recording, a Pexels stock clip {"stock_video": "words"}, an AI clip's file_id — a picture, a chart or a text) or on
a `deck` layer (the slides). `type: "facecam"` is a fullscreen layer with the default speaker. Why a placement: the content
keeps everything its own layer already does (the stock search, the clip's in/out, the credits, the slides following the
speech); the facecam only changes how the footage relates to it during the layer's time — like `split` does.

How it is built (one model for the final and the live preview):
  · the content layer goes UNDER the footage layer in the engine project (`under`);
  · the footage layer gets Transform keys (scale + position, eased like zoom / split): at `at` it is full frame, after
    `move` seconds it is in the window, `move` seconds before the end it starts to grow back — the voice runs on;
  · a mask named "Facecam" on the footage layer, in the layer's own pixels (so it moves with the transform): an 8-vertex
    rounded rectangle whose parameters (cx, cy, hw, hh, r) morph from a rectangle bigger than the frame to the window's
    shape (a circle is hw = hh = r). Every vertex and tangent is linear in those five numbers, so the engine's path
    interpolation (vertex by vertex, with the key's eased progress) IS the rounded rectangle with interpolated
    parameters — the browser evaluates exactly that in its footage shader (live-player.js, the footage mask);
  · the ring (a stroke in the accent colour), a soft shadow (under the footage, over the content) and an optional label
    ("Coming Up…") are ordinary graphic layers — the live preview gets them as engine-rendered clips (live_layers).

Coordinates: Python works in the comp's pixels with the footage AT REST (filling the frame); the builder turns the mask
into the layer's pixels (anchor + (p − centre) / base scale) — a working copy at half size has half the pixels, and the
final's full-size swap (footage.swap) scales the mask with the anchor.
"""
from __future__ import annotations

import math
from typing import Any, Optional

SHAPES = ("circle", "rounded", "rect")
CORNERS = {"bl": "bl", "br": "br", "tl": "tl", "tr": "tr", "bottom-left": "bl", "bottom-right": "br", "top-left": "tl",
           "top-right": "tr", "bottom_left": "bl", "bottom_right": "br", "top_left": "tl", "top_right": "tr"}
MASK_NAME = "Facecam"
K = 0.5523                       # a quarter circle's Bézier handle (the same as fast_render._round_rect)
FIELDS = ("shape", "size", "corner", "ring", "ring_width", "shadow", "focus", "zoom", "aspect", "radius", "move", "label",
          "feather")
DEFAULT = {"shape": "circle", "corner": "bl", "ring": "accent", "shadow": True, "focus": [0.5, 0.42], "move": 0.7}
EXAMPLE = ('{"at": "05:55", "until": "06:15", "type": "fullscreen", "image": {"stock_video": "technology network"}, '
           '"speaker": {"shape": "circle", "corner": "bl", "label": "Coming Up..."}}')


def _num(v: Any, lo: float, hi: float, what: str) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: a number between {lo:g} and {hi:g}.") from None
    if not math.isfinite(x):
        raise ValueError(f"{what}: a number between {lo:g} and {hi:g}.")
    return max(lo, min(hi, x))


def _pair(v: Any, what: str) -> list[float]:
    if not (isinstance(v, (list, tuple)) and len(v) == 2):
        raise ValueError(f"{what}: [x, y], each 0–1 of the frame.")
    return [round(_num(v[0], 0.0, 1.0, what), 4), round(_num(v[1], 0.0, 1.0, what), 4)]


def opts(raw: Any, what: str, old: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """The layer's `speaker` → the clean dict (old: the edited layer's speaker — what isn't given stays). False / "off" /
    "none": no speaker window (the footage is hidden under the content, as a plain fullscreen layer)."""
    if raw in (False, "off", "none", "no"):
        return None
    if raw in (None, True, ""):
        raw = {}
    if isinstance(raw, str):                         # "circle" / "rounded" / "rect" alone
        raw = {"shape": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"{what} (speaker): an object {{shape, size, corner, ring, shadow, label…}}, e.g. {EXAMPLE}.")
    sp: dict[str, Any] = {**DEFAULT, **(old or {})}
    unknown = [k for k in raw if k not in FIELDS]
    if unknown:
        raise ValueError(f"{what} (speaker): {', '.join(unknown)} — not a speaker field; the fields: {', '.join(FIELDS)}.")
    if "shape" in raw:
        s = str(raw["shape"] or "").strip().lower().replace("rounded_rect", "rounded").replace("rectangle", "rect")
        s = {"round": "circle", "oval": "circle", "square": "rect", "box": "rect"}.get(s, s)
        if s not in SHAPES:
            raise ValueError(f"{what} (speaker): shape {raw['shape']!r} — {' | '.join(SHAPES)}.")
        if s != sp.get("shape"):                     # a new shape: its own default size / aspect
            sp.pop("size", None)
            sp.pop("aspect", None)
        sp["shape"] = s
    if "corner" in raw:
        c = raw["corner"]
        if isinstance(c, (list, tuple)):
            sp["corner"] = _pair(c, f"{what} (speaker): corner")
        else:
            cc = CORNERS.get(str(c or "").strip().lower())
            if not cc:
                raise ValueError(f"{what} (speaker): corner {c!r} — bl | br | tl | tr, or [x, y] (the window's centre, 0–1).")
            sp["corner"] = cc
    if raw.get("size") is not None:
        v = raw["size"]
        v = {"small": 0.26, "medium": 0.36, "large": 0.5}.get(str(v).lower(), v) if isinstance(v, str) else v
        sp["size"] = round(_num(v, 0.12, 0.7, f"{what} (speaker): size (the window's height, 0.12–0.7 of the frame's)"), 4)
    if raw.get("aspect") is not None:
        a = raw["aspect"]
        try:
            x, y = (float(t) for t in str(a).split(":")) if isinstance(a, str) and ":" in a else (float(a), 1.0)
            sp["aspect"] = round(max(0.5, min(2.4, x / y)), 4)
        except (TypeError, ValueError, ZeroDivisionError):
            raise ValueError(f"{what} (speaker): aspect = \"16:9\" | \"4:3\" | \"1:1\" | a number.") from None
    if "ring" in raw:
        r = raw["ring"]
        if r in (False, None, "none", "off", "no", 0):
            sp["ring"] = None
        elif r in (True, "accent"):
            sp["ring"] = "accent"
        elif isinstance(r, str) and (r.startswith("#") or r.lower() in ("white", "black")):
            sp["ring"] = {"white": "#ffffff", "black": "#000000"}.get(r.lower(), r[:9])
        else:
            raise ValueError(f"{what} (speaker): ring = \"accent\" (the default) | \"#RRGGBB\" | \"white\" | false.")
    if raw.get("ring_width") is not None:
        sp["ring_width"] = round(_num(raw["ring_width"], 0.0, 0.05, f"{what} (speaker): ring_width (0–0.05 of the frame)"), 4)
    if "shadow" in raw:
        sp["shadow"] = bool(raw["shadow"])
    if raw.get("focus") is not None:
        sp["focus"] = _pair(raw["focus"], f"{what} (speaker): focus (where the face is in the footage)")
    if raw.get("zoom") is not None:
        sp["zoom"] = round(_num(raw["zoom"], 1.0, 4.0, f"{what} (speaker): zoom (1 = the whole frame's height)"), 4)
    if raw.get("radius") is not None:
        sp["radius"] = round(_num(raw["radius"], 0.0, 0.5, f"{what} (speaker): radius (0–0.5 of the window's shorter side)"), 4)
    if raw.get("move") is not None:
        sp["move"] = round(_num(raw["move"], 0.0, 3.0, f"{what} (speaker): move (the shrink / grow, seconds)"), 3)
    if raw.get("feather") is not None:
        sp["feather"] = round(_num(raw["feather"], 0.0, 40.0, f"{what} (speaker): feather (px)"), 2)
    if "label" in raw:
        lab = " ".join(str(raw["label"] or "").split())[:40]
        if lab:
            sp["label"] = lab
        else:
            sp.pop("label", None)
    return sp


# ---------- the geometry (comp pixels, the footage at rest) ----------

def window(sp: dict[str, Any], W: int, H: int, safe: Optional[float] = None) -> dict[str, float]:
    """The speaker window on the frame → {cx, cy, w, h, r} (comp px). A bottom corner stays above the captions (safe)."""
    m = min(W, H)
    circle = sp["shape"] == "circle"
    h = float(sp.get("size") or (0.36 if circle else 0.3)) * H
    aspect = 1.0 if circle else float(sp.get("aspect") or 16 / 9)
    w = h * aspect
    if w > W * 0.9:
        w, h = W * 0.9, W * 0.9 / aspect
    mg = 0.04 * m
    c = sp.get("corner") or "bl"
    if isinstance(c, list):
        cx = min(max(c[0] * W, w / 2 + 2), W - w / 2 - 2)
        cy = min(max(c[1] * H, h / 2 + 2), H - h / 2 - 2)
    else:
        bottom = min(H - mg, safe if safe else H - mg)
        cx = mg + w / 2 if c[1] == "l" else W - mg - w / 2
        cy = mg + h / 2 if c[0] == "t" else max(h / 2 + 2, bottom - h / 2)
    r = min(w, h) / 2 if circle else 0.0 if sp["shape"] == "rect" else float(sp.get("radius") or 0.12) * min(w, h)
    return {"cx": round(cx, 2), "cy": round(cy, 2), "w": round(w, 2), "h": round(h, 2), "r": round(r, 2)}


def region(sp: dict[str, Any], win: dict[str, float], W: int, H: int) -> dict[str, float]:
    """The part of the footage the window shows (comp px at rest, the window's aspect) → {cx, cy, hw, hh, r}: around the
    face (focus), `zoom` × closer than the whole frame's height (a circle 1.45 — head and shoulders; a rectangle 1 — the
    whole picture), kept inside the footage (no empty edge in the window)."""
    circle = sp["shape"] == "circle"
    zoom = float(sp.get("zoom") or (1.45 if circle else 1.0))
    aspect = win["w"] / win["h"]
    hh = H / 2 / zoom
    hw = hh * aspect
    if hw > W / 2:                                   # a wide window on a narrow footage: the width decides
        hw, hh = W / 2, W / 2 / aspect
    fx, fy = (sp.get("focus") or DEFAULT["focus"])
    cx = min(max(fx * W, hw), W - hw)
    cy = min(max(fy * H, hh), H - hh)
    k = hh / (win["h"] / 2)                          # footage px per window px
    return {"cx": round(cx, 3), "cy": round(cy, 3), "hw": round(hw, 3), "hh": round(hh, 3), "r": round(win["r"] * k, 3)}


def full_rect(W: int, H: int, feather: float) -> dict[str, float]:
    """The mask at full frame: a rectangle bigger than the footage (its feathered edge never reaches the picture)."""
    pad = 8.0 + 2.0 * feather
    return {"cx": W / 2, "cy": H / 2, "hw": round(W / 2 + pad, 3), "hh": round(H / 2 + pad, 3), "r": 0.0}


def plan(sp: dict[str, Any], at: float, dur: float, W: int, H: int, safe: Optional[float] = None) -> dict[str, Any]:
    """The footage keys and the mask for one speaker window → the builder's `facecam` item: {move, scale (× the rest
    scale), to (the footage layer's position in the window), full / win (mask params, comp px at rest), feather (comp px
    at rest), window (comp px)}."""
    win = window(sp, W, H, safe)
    reg = region(sp, win, W, H)
    s = (win["h"] / 2) / reg["hh"]                   # the footage's scale in the window (× its rest scale)
    to = [round(win["cx"] - s * (reg["cx"] - W / 2), 3), round(win["cy"] - s * (reg["cy"] - H / 2), 3)]
    feather = float(sp["feather"]) if sp.get("feather") is not None else (1.5 if sp.get("ring") else 2.5)
    feather_rest = round(feather / s, 3)              # the feather is in the footage's pixels: ~1.5 px on screen
    move = min(float(sp.get("move") if sp.get("move") is not None else DEFAULT["move"]), dur / 3)
    return {"move": round(move, 4), "scale": round(s, 6), "to": to, "win": reg, "full": full_rect(W, H, feather_rest),
            "feather": feather_rest, "window": win}


# ---------- the mask path (8 vertices: the same convention here, in the builder and in the browser) ----------

def path(p: dict[str, float]) -> dict[str, list[list[float]]]:
    """Rounded rectangle (cx, cy, hw, hh, r) → {vertices, in_tangents, out_tangents} — clockwise from the top edge's left
    end; every coordinate is linear in the five parameters (r ≤ min(hw, hh); a circle: hw = hh = r)."""
    cx, cy, hw, hh = p["cx"], p["cy"], p["hw"], p["hh"]
    r = max(0.0, min(p["r"], hw, hh))
    k = K * r
    v = [[cx - hw + r, cy - hh], [cx + hw - r, cy - hh], [cx + hw, cy - hh + r], [cx + hw, cy + hh - r],
         [cx + hw - r, cy + hh], [cx - hw + r, cy + hh], [cx - hw, cy + hh - r], [cx - hw, cy - hh + r]]
    ti = [[-k, 0], [0, 0], [0, -k], [0, 0], [k, 0], [0, 0], [0, k], [0, 0]]
    to = [[0, 0], [k, 0], [0, 0], [0, k], [0, 0], [-k, 0], [0, 0], [0, -k]]
    return {"vertices": v, "in_tangents": ti, "out_tangents": to}


def params_of(path_v: dict[str, Any]) -> Optional[list[float]]:
    """A mask path from the project → [cx, cy, hw, hh, r] if it is our 8-vertex rounded rectangle, else None."""
    v = path_v.get("vertices") or []
    if len(v) != 8:
        return None
    try:
        left, right = float(v[6][0]), float(v[2][0])
        top, bottom = float(v[0][1]), float(v[4][1])
        r = right - float(v[1][0])
    except (TypeError, ValueError, IndexError):
        return None
    cx, cy, hw, hh = (left + right) / 2, (top + bottom) / 2, (right - left) / 2, (bottom - top) / 2
    if hw < 0 or hh < 0 or r < -1e-6:
        return None
    return [round(cx, 4), round(cy, 4), round(hw, 4), round(hh, 4), round(max(0.0, r), 4)]


def mask_of(layer: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The footage layer (project JSON) → its facecam mask for the live preview: {feather (layer px), keys: [{t, v: [cx,
    cy, hw, hh, r] (layer px), in, out, ie, oe}], spans: [[a, b]] (where it isn't the full rectangle), inverted} | None."""
    from app.tools import live_scene as ls
    root = layer.get("props") or {}
    masks = next((n for n in ls._children(root) if n.get("match") == "masks"), None)
    m = next((n for n in ls._children(masks or {}) if n.get("match") == "mask" and n.get("name") == MASK_NAME), None)
    if m is None or not m.get("enabled", True):
        return None
    out: dict[str, Any] = {"feather": 0.0, "keys": [], "spans": [], "inverted": bool((m.get("kind") or {}).get("inverted"))}
    for p in ls._children(m):
        if p.get("match") == "feather":
            fv = ls._val(p.get("value"))
            out["feather"] = float(fv[0] if isinstance(fv, list) else fv or 0)
        if p.get("match") != "path":
            continue
        ks = p.get("keys") or []
        if not ks:
            pv = params_of(((p.get("value") or {}).get("v")) or {})
            if pv is None:
                return None
            out["keys"].append({"t": 0.0, "v": pv, "in": "hold", "out": "hold", "ie": [], "oe": []})
        for x in ks:
            pv = params_of(((x.get("value") or {}).get("v")) or {})
            if pv is None:
                return None
            out["keys"].append({"t": round(int(x["time"]) / ls.TICKS, 6), "v": pv, "in": ls._INTERP.get(x.get("in_interp"), "linear"),
                                "out": ls._INTERP.get(x.get("out_interp"), "linear"), "ie": ls._ease(x.get("in_ease")),
                                "oe": ls._ease(x.get("out_ease"))})
    if not out["keys"]:
        return None
    out["spans"] = spans(out["keys"])
    return out


def _is_full(v: list[float], ref: list[float]) -> bool:
    return all(abs(a - b) <= 1e-3 * max(1.0, abs(b)) for a, b in zip(v, ref))


def spans(keys: list[dict[str, Any]]) -> list[list[float]]:
    """Where the mask is not the full rectangle (the value of the first key — the builder's full frame): between two keys
    of which either is a window; the browser evaluates the mask only there."""
    if not keys:
        return []
    ref = keys[0]["v"]
    out: list[list[float]] = []
    for a, b in zip(keys, keys[1:]):
        if not _is_full(a["v"], ref) or not _is_full(b["v"], ref):
            if out and a["t"] <= out[-1][1] + 1e-6:
                out[-1][1] = b["t"]
            else:
                out.append([a["t"], b["t"]])
    if len(keys) == 1 or not _is_full(keys[-1]["v"], ref):
        out.append([keys[-1]["t"], 1e12])
    return [[round(a, 6), round(b, 6)] for a, b in out]


# ---------- the extra layers (ring, shadow, label) ----------

def items(ly: dict[str, Any], fc: dict[str, Any], W: int, H: int, acc: list[float], text_fn: Any) -> list[dict[str, Any]]:
    """The ring, the shadow (under the footage) and the label for a speaker window, as builder items — while the window
    stands (after the shrink, before the grow); text_fn: video._tx (a text element)."""
    from app.tools.video import _PANEL, _even, _rgb
    sp, win = ly["speaker"], fc["window"]
    a, b, d = float(ly["at"]), float(ly["at"]) + float(ly["dur"]), fc["move"]
    t0, t1 = a + d, b - d
    if t1 - t0 < 0.6:
        return []
    m = min(W, H)
    out: list[dict[str, Any]] = []
    base = {"at": round(t0, 3), "dur": round(t1 - t0, 3), "slide": [0, 0]}
    ww, wh, wr = win["w"], win["h"], win["r"]
    if sp.get("shadow"):                             # soft shadow: stacked, fading rounded rectangles (cheap, exact)
        spread, n = 0.022 * m, 6
        sw, sh = _even(ww + 2 * spread + 8), _even(wh + 2 * spread + 0.03 * m + 8)
        els = []
        for i in range(n):
            g = spread * (n - i) / n
            els.append({"k": "rect", "name": f"Shadow {i + 1}", "size": [round(ww + 2 * g, 1), round(wh + 2 * g, 1)],
                        "pos": [sw / 2, round(sh / 2 + 0.012 * m, 1)], "color": [0, 0, 0], "opacity": 9 if i < n - 1 else 14,
                        "round": round(wr + g if wr > 0 else g * 0.6, 1)})
        out.append({**base, "id": f"{ly['id']}.shadow", "name": f"Layer {ly['id']}.shadow – facecam shadow", "w": sw, "h": sh,
                    "pos": [round(win["cx"]), round(win["cy"])], "elements": els, "anim": {"in": "fade", "out": "fade"},
                    "under": True})
    if sp.get("ring"):
        col = acc if sp["ring"] == "accent" else _rgb(sp["ring"])
        rw = max(2.0, float(sp.get("ring_width") or 0.007) * m)
        rs = [round(ww + 0.7 * rw, 1), round(wh + 0.7 * rw, 1)]
        rww, rwh = _even(rs[0] + 2 * rw + 4), _even(rs[1] + 2 * rw + 4)
        rr = rs[1] / 2 if sp["shape"] == "circle" else (wr + 0.35 * rw if wr > 0 else 0)
        out.append({**base, "id": f"{ly['id']}.ring", "name": f"Layer {ly['id']}.ring – facecam ring", "w": rww, "h": rwh,
                    "pos": [round(win["cx"]), round(win["cy"])], "anim": {"in": "fade", "out": "fade"},
                    "elements": [{"k": "rect", "name": "Ring", "size": rs, "pos": [rww / 2, rwh / 2], "color": None,
                                  "opacity": 100, "round": round(rr, 1), "stroke": col, "stroke_w": round(rw, 1)}]})
    if sp.get("label"):
        s = round(m * 0.04)
        pad, bar = s * 0.55, max(5.0, m * 0.008)
        from app.tools.video import _wrap
        tw = _wrap(sp["label"], s, W * 0.5)[2] * 1.15     # bold: wider than the estimate (one line, never wrapped)
        lw, lh = _even(bar + 2 * pad + tw + s * 0.6), _even(s * 1.25 + 2 * pad * 0.8)
        right = win["cx"] < W / 2                    # beside the window, on the frame's inner side
        gap = 0.022 * m
        cx = win["cx"] + ww / 2 + gap + lw / 2 if right else win["cx"] - ww / 2 - gap - lw / 2
        cy = win["cy"] - wh / 2 + lh / 2 + 0.08 * wh if win["cy"] > H / 2 else win["cy"] + wh / 2 - lh / 2 - 0.08 * wh
        cx = min(max(cx, lw / 2 + 4), W - lw / 2 - 4)
        els = [{"k": "rect", "name": "Panel", "size": [lw - bar, lh], "pos": [round(bar + (lw - bar) / 2, 1), lh / 2],
                "color": _PANEL, "opacity": 86, "round": 0, "grow": "right", "t0": 0.05, "dur": 0.35},
               {"k": "rect", "name": "Accent bar", "size": [round(bar, 1), lh], "pos": [round(bar / 2, 1), lh / 2],
                "color": "accent", "opacity": 100, "round": 0, "grow": "up", "t0": 0.0, "dur": 0.3},
               text_fn("Label", sp["label"], s, bar + pad, lh / 2, lw - bar - 2 * pad, font="bold", t0=0.2)]
        out.append({**base, "id": f"{ly['id']}.label", "name": f"Layer {ly['id']}.label – facecam label", "w": lw, "h": lh,
                    "pos": [round(cx), round(cy)], "elements": els, "anim": {"in": "slide", "out": "fade"},
                    "slide": [round(-m * 0.04 if right else m * 0.04, 1), 0]})
    return out


def conflicts(layers: list[dict[str, Any]]) -> list[str]:
    """Two things moving the footage at the same time (a speaker window and a split screen, or two windows) fight over
    its keys → the check says so."""
    movers = sorted(((float(ly["at"]), float(ly["at"]) + float(ly["dur"]), ly) for ly in layers
                     if ly.get("speaker") or ly.get("type") == "split" or (ly.get("type") == "deck" and ly.get("placement") == "window")),
                    key=lambda x: x[0])
    out = []
    for i, (a0, a1, la) in enumerate(movers):
        for b0, b1, lb in movers[i + 1:]:
            if b0 < a1 - 1e-3 and (la.get("speaker") or lb.get("speaker")):
                out.append(f"Layers {la['id']} and {lb['id']} both move the footage from {b0:.1f} s to {min(a1, b1):.1f} s (a speaker "
                           "window and a split / another window) — move one of them in time.")
    return out


def no_speaker(layers: list[dict[str, Any]]) -> None:
    """A video made from text has no speaker footage to put in a window."""
    hit = [ly["id"] for ly in layers if ly.get("speaker")]
    if hit:
        raise ValueError(f"Layer {hit[0]} (speaker): the speaker window shows your own footage — use it on a video of your own "
                         "(video action=\"overlay\", or editing such a video).")
