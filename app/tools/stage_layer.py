"""Stage layer (N5, 10-26): a studio-look composition around the talking head — floating, perspective-tilted panels
(slides, pictures, screenshots, a playing clip) arranged around the speaker, some of them behind him; an animated
motion-graphics background behind him; short labels ("pills") popping in. The owner: "nekünk is mehetne" (after the
screenshots of a Hungarian AI channel's studio look, 10-26).

ONE layer type (story.layers[], type "stage"), not several: the panels, the background and the pills are arranged
TOGETHER (a preset places the panels around the speaker, the background and the "behind" panels share one speaker
cut-out span, the pills point at the panels) — as separate layer types every one of them would have to know the others.
Inside, the builder gets one builder layer per part (as the deck does), so the live preview renders and caches each part
on its own and an edit of one pill re-renders only that pill:

    {"id": "L40", "type": "stage", "at": s, "dur" | "until": s,
     "preset": "grid4" | "pair" | "two_left_one_right" | "one_left_two_right" | "trio" | "left_stack" | "right_stack" |
               "single_left" | "single_right" | "grid5" | "hexa"       (default: by the number of panels),
     "deck": "<pptx>"                                 the slides' deck (a panel can name its own file_id instead),
     "panels": [{"slide": n | "file_id": "<png|jpg|webp|mp4|mov|pptx>", "slide": n,
                 "slot": k (the preset's place, 1…) | "box": [cx, cy, w] (0–1 of the frame),
                 "tilt": [rx, ry, rz] (degrees: rx + = the top edge back, ry + = the right edge back, rz + = clockwise),
                 "depth": "front" | "behind" (behind = the speaker cut out in front of it),
                 "at": s, "until": s (output time; default: one after the other from the stage's start, until its end),
                 "enter": "fly" | "scale" | "fade" | "none", "frame": "plain" | "window" | "none",
                "float": true | false (the subtle drift; default: the stage's "float", true)}, …]   1–6 panels,
     "background": "glow" | "particles" | "halftone" | "gradient" | "none" | {"kind": …, "color": "#hex",
                   "intensity": 0–1} | {"kind": "video", "file_id": "<mp4>" | "stock": "words (Pexels)"},
     "pills": [{"text": "Claude Desktop", "at": s, "until": s, "near": panel n | "pos": [x, y],
                "style": "accent" | "light" | "dark"}, …]}

Builder layers (stage → D.layers, drawing order bottom → top; app/tools/stage_builder.jsx draws them):
  · "Layer L40.b – stage behind (glow + 2 panels)" — full frame, opaque: the background as a few generated pictures
    (PIL, here: a gradient, a blurred glow shape, constellation clusters, a halftone tile …) moved by EXPRESSIONS in the
    engine — so the engine only composites pictures (no blur per frame) and every motion repeats exactly after PERIOD_S
    (the phase is computed from the integer frame number: frame f and f + period are bit-identical) — with the behind
    panels that stay the whole stage; then the behind panels of their own ("L40.pK"), the front group ("L40.f – stage
    front (2 panels)"), the front panels of their own. A panel: its picture (rounded, framed, padded with transparency —
    the engine's Corner Pin draws only inside the layer's own bounds), a Corner Pin with the projected corners of the
    tilted rectangle (a pinhole projection here, focal 1.6 × W), a Drop Shadow, the entrance as Corner Pin keys (from
    further back / more turned), a float by expression (period PERIOD_S), the fly-in as position keys;
  · "Layer L40.tK – stage pill …": a rounded label, popping in with an overshoot.
Depth: the speaker is put back over the background and the behind panels from the person matte — live: `matte_part`
adds the stage's span to the scene's matte spans and flags those clips `behind` (app/static/live-player.js draws the
footage again through the mask right above the last behind clip); final: `add_speakers` puts a speaker item (the
background deck's: video_builder.jsx speakerLayer, the matte clip from app/tools/deck_matte.matte) right after the last
behind item, so the front panels and the pills stay in front of him.

Live preview cost (app/tools/live_layers.py): the background and the panels that stay the whole stage are grouped
(`grouping`: one behind clip, one front clip), and every stage part's cycle is known by construction (`live_cycle`): the
clip is the entrance + ONE period (no probe), a pill is a still after its pop.
"""
from __future__ import annotations

import asyncio
import colorsys
import hashlib
import json
import logging
import math
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("aice.stage_layer")

PRESETS_V = 1
ART_V = 1                       # the generated pictures' recipe (raise when it changes: new content names)
PERIOD_S = 6.0                  # every stage motion repeats after this (the background spin, the float, the twinkle)
ENTER_S = 0.6                   # a panel's entrance (Corner Pin keys inside its own comp)
FOCAL = 1.6                     # the pinhole camera's focal length × the frame width (a subtle perspective)
MAX_PANELS, MAX_PILLS = 6, 6
STAGGER_S, PILL_DELAY_S = 0.18, 1.0
BG_KINDS = ("glow", "particles", "halftone", "gradient", "video", "none")
DEPTHS = ("front", "behind")
ENTERS = ("fly", "scale", "fade", "none")
FRAMES = ("plain", "window", "none")
PILL_STYLES = ("accent", "light", "dark")
SLIDE_EXT = re.compile(r"\.(pptx|ppt|odp)$", re.I)
VIDEO_EXT = re.compile(r"\.(mp4|mov|m4v|webm|mkv)$", re.I)
IMAGE_EXT = re.compile(r"\.(png|jpe?g|webp|gif|bmp)$", re.I)
ITEM_RE = re.compile(r"^Layer (\S+)\.(b|f|p\d+|t\d+) – stage ")
STILL = ", still"               # in a part's name: nothing moves after its entrance (live_cycle: period 1)
ART_DIR = "_stage"

# the presets: (cx, cy, w) as parts of the frame (16:9; the speaker in the middle), tilt (rx, ry, rz), depth.
# grid4 = the owner's screenshot (20-57-27): two panels beside the head, two lower ones behind the shoulders.
_L, _R = (3.0, 16.0, -1.5), (3.0, -16.0, 1.5)
_LL, _RL = (-3.0, 15.0, 1.0), (-3.0, -15.0, -1.0)
PRESETS: dict[str, list[tuple[float, float, float, tuple[float, float, float], str]]] = {
    "grid4": [(0.205, 0.285, 0.33, _L, "front"), (0.795, 0.285, 0.33, _R, "front"),
              (0.205, 0.735, 0.33, _LL, "behind"), (0.795, 0.735, 0.33, _RL, "behind")],
    "grid5": [(0.205, 0.285, 0.31, _L, "front"), (0.795, 0.285, 0.31, _R, "front"),
              (0.205, 0.735, 0.31, _LL, "behind"), (0.795, 0.735, 0.31, _RL, "behind"),
              (0.5, 0.16, 0.24, (8.0, 0.0, 0.0), "behind")],
    "pair": [(0.2, 0.46, 0.34, (2.0, 15.0, -1.0), "front"), (0.8, 0.46, 0.34, (2.0, -15.0, 1.0), "front")],
    "two_left_one_right": [(0.2, 0.29, 0.31, _L, "front"), (0.2, 0.72, 0.31, _LL, "behind"),
                           (0.79, 0.47, 0.36, (2.0, -15.0, 1.0), "front")],
    "one_left_two_right": [(0.21, 0.47, 0.36, (2.0, 15.0, -1.0), "front"), (0.8, 0.29, 0.31, _R, "front"),
                           (0.8, 0.72, 0.31, _RL, "behind")],
    "trio": [(0.19, 0.5, 0.3, (2.0, 16.0, -1.0), "front"), (0.5, 0.19, 0.28, (8.0, 0.0, 0.0), "behind"),
             (0.81, 0.5, 0.3, (2.0, -16.0, 1.0), "front")],
    "left_stack": [(0.24, 0.28, 0.36, _L, "front"), (0.24, 0.73, 0.36, _LL, "front")],
    "right_stack": [(0.76, 0.28, 0.36, _R, "front"), (0.76, 0.73, 0.36, _RL, "front")],
    "single_left": [(0.25, 0.46, 0.42, (2.0, 16.0, -1.0), "front")],
    "single_right": [(0.75, 0.46, 0.42, (2.0, -16.0, 1.0), "front")],
    "hexa": [(0.165, 0.2, 0.26, _L, "front"), (0.835, 0.2, 0.26, _R, "front"),
             (0.155, 0.5, 0.26, (0.0, 15.0, 0.0), "behind"), (0.845, 0.5, 0.26, (0.0, -15.0, 0.0), "behind"),
             (0.165, 0.8, 0.26, _LL, "behind"), (0.835, 0.8, 0.26, _RL, "behind")],
}
# the speaker off-centre (on the left: the panels fan out on his right, the inner column partly behind him)
PRESETS["right_grid4"] = [(0.635, 0.29, 0.25, (3.0, -12.0, 1.0), "behind"), (0.865, 0.29, 0.25, (3.0, -18.0, 1.5), "front"),
                          (0.635, 0.72, 0.25, (-3.0, -12.0, -1.0), "behind"), (0.865, 0.72, 0.25, (-3.0, -18.0, -1.5), "front")]
PRESETS["right_grid6"] = [(0.655, 0.2, 0.22, (3.0, -10.0, 1.0), "behind"), (0.87, 0.2, 0.22, (3.0, -14.0, 1.5), "front"),
                          (0.655, 0.5, 0.22, (0.0, -10.0, 0.0), "behind"), (0.87, 0.5, 0.22, (0.0, -14.0, 0.0), "front"),
                          (0.655, 0.8, 0.22, (-3.0, -10.0, -1.0), "behind"), (0.87, 0.8, 0.22, (-3.0, -14.0, -1.5), "front")]


def _mirror(slots):
    return [(round(1 - cx, 4), cy, w, (rx, -ry, -rz), d) for cx, cy, w, (rx, ry, rz), d in slots]


PRESETS["left_grid4"] = _mirror(PRESETS["right_grid4"])
PRESETS["left_grid6"] = _mirror(PRESETS["right_grid6"])
BY_COUNT = {1: "single_right", 2: "pair", 3: "two_left_one_right", 4: "grid4", 5: "grid5", 6: "hexa"}
# preset "auto" (the default): by where the speaker's face is over the stage's span (speaker_zone) — centred: BY_COUNT;
# on the left: the right-side family; on the right: the left-side family
BY_SIDE = {"right": {1: "single_right", 2: "right_stack", 3: "right_grid4", 4: "right_grid4", 5: "right_grid6", 6: "right_grid6"},
           "left": {1: "single_left", 2: "left_stack", 3: "left_grid4", 4: "left_grid4", 5: "left_grid6", 6: "left_grid6"}}
AUTO_SLOTS = 6
_ALIAS = {"grid": "grid4", "grid_4": "grid4", "four": "grid4", "2l1r": "two_left_one_right", "1l2r": "one_left_two_right",
          "two_left": "two_left_one_right", "two_right": "one_left_two_right", "sides": "pair", "left": "left_stack",
          "right": "right_stack", "six": "hexa", "grid6": "hexa", "five": "grid5"}


def _tc(t: float) -> str:
    t = max(0.0, float(t))
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def _num(v: Any, what: str, lo: float, hi: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: {v!r} is not a number.") from None
    if not lo <= x <= hi:
        raise ValueError(f"{what}: {x:g} — between {lo:g} and {hi:g}.")
    return x


def _choice(v: Any, allowed: tuple[str, ...], what: str, default: str) -> str:
    s = str(v or default).strip().lower()
    if s not in allowed:
        raise ValueError(f"{what}: {v!r} — {' | '.join(allowed)}.")
    return s


def kind_of(fid: str) -> Optional[str]:
    if SLIDE_EXT.search(fid):
        return "slide"
    if VIDEO_EXT.search(fid):
        return "video"
    if IMAGE_EXT.search(fid):
        return "image"
    return None


# ---------- validation (called from video._layer_from; pure) ----------

def prepare(r: dict[str, Any], raw: dict[str, Any], base: Optional[dict[str, Any]], what: str) -> dict[str, Any]:
    """Before the common at / dur handling: an edit that moves the whole stage (a new at, no new panels / pills) moves
    the panels' and pills' own times with it."""
    from app.tools.video import timecode
    r = dict(r)
    if base and "at" in raw and base.get("at") is not None:
        delta = timecode(raw["at"], f"{what} at") - float(base["at"])
        for key in ("panels", "pills"):
            if key not in raw and base.get(key):
                r[key] = [{**p, **{k: round(float(p[k]) + delta, 3) for k in ("at", "until") if p.get(k) is not None}}
                          for p in base[key]]
    return r


def default_dur(r: dict[str, Any], at: float) -> Optional[float]:
    return None                                    # (_LAYER_DUR["stage"])


def _background(v: Any, what: str) -> dict[str, Any]:
    if v in (None, ""):
        return {"kind": "glow"}
    if isinstance(v, str):
        v = {"kind": v}
    if not isinstance(v, dict):
        raise ValueError(f"{what}: background = {' | '.join(BG_KINDS)} or {{\"kind\": \"glow\", \"color\": \"#3b82f6\"}}.")
    kind = _choice(v.get("kind") or ("video" if v.get("file_id") or v.get("stock") else "glow"), BG_KINDS,
                   f"{what} background.kind", "glow")
    out: dict[str, Any] = {"kind": kind}
    if v.get("color"):
        c = str(v["color"]).strip()
        if not re.fullmatch(r"#?[0-9a-fA-F]{6}", c):
            raise ValueError(f"{what} background.color: {c!r} — a hex colour such as \"#3b82f6\" (default: the accent).")
        out["color"] = "#" + c.lstrip("#").lower()
    if v.get("intensity") is not None:
        out["intensity"] = round(_num(v["intensity"], f"{what} background.intensity", 0.0, 1.0), 3)
    if kind == "video":
        fid = v.get("file_id")
        if not fid:
            if v.get("stock"):
                raise ValueError(f"{what} background: the stock video “{v['stock']}” wasn't fetched — try other words, or give "
                                 "a file_id (an uploaded clip, or one made with the AI clip tool).")
            raise ValueError(f"{what} background (video): give file_id (an uploaded or AI-made clip) or stock: \"words\" "
                             "(a free Pexels clip).")
        if not VIDEO_EXT.search(str(fid)):
            raise ValueError(f"{what} background: {fid!r} isn't a video (mp4 / mov / webm).")
        out["file_id"] = str(fid)
        for k in ("credit", "stock"):
            if v.get(k):
                out[k] = str(v[k])[:200]
    return out


def fields(r: dict[str, Any], raw: dict[str, Any], base: Optional[dict[str, Any]], ly: dict[str, Any], what: str,
           notes: list[str]) -> dict[str, Any]:
    """The stage's own fields, checked: the preset, the panels (file, place, tilt, depth, times, entrance, frame), the
    background and the pills. ly: the common part already made (id, type, at, dur)."""
    from app.tools.video import timecode
    a, b = float(ly["at"]), float(ly["at"]) + float(ly["dur"])
    deck = r.get("deck") or r.get("file_id")
    if deck is not None and (not isinstance(deck, str) or not SLIDE_EXT.search(deck)):
        raise ValueError(f"{what} (stage): deck {deck!r} — the slides' PowerPoint file (.pptx).")
    panels = r.get("panels") or []
    if not isinstance(panels, list) or len(panels) > MAX_PANELS:
        raise ValueError(f"{what} (stage): panels = a list of 1–{MAX_PANELS} panels, e.g. [{{\"slide\": 4}}, {{\"file_id\": "
                         "\"<png>\", \"depth\": \"behind\"}}].")
    pills = r.get("pills") or []
    if not isinstance(pills, list) or len(pills) > MAX_PILLS:
        raise ValueError(f"{what} (stage): pills = a list of at most {MAX_PILLS} labels, e.g. [{{\"text\": \"Claude Desktop\"}}].")
    if not panels and not pills and r.get("background") in (None, "", "none"):
        raise ValueError(f"{what} (stage): give panels, pills or a background — e.g. {{\"type\": \"stage\", \"at\": \"03:00\", "
                         "\"until\": \"03:30\", \"deck\": \"<pptx>\", \"panels\": [{\"slide\": 2}, {\"slide\": 4}, {\"slide\": 7}, "
                         "{\"slide\": 9}], \"background\": \"glow\", \"pills\": [{\"text\": \"összehasonlítás\"}]}.")
    preset = str(r.get("preset") or "").strip().lower().replace("-", "_").replace(" ", "_")
    preset = _ALIAS.get(preset, preset)
    if preset and preset not in ("custom", "auto") and preset not in PRESETS:
        raise ValueError(f"{what} (stage): preset {r.get('preset')!r} — auto (by where you are in the picture) | "
                         f"{' | '.join(PRESETS)} | custom (every panel its box).")
    preset = preset or "auto"
    slots = PRESETS.get(preset, []) if preset != "auto" else [None] * AUTO_SLOTS
    out: dict[str, Any] = {"preset": preset}
    if r.get("float") is not None:                         # false: the panels stand still after their entrance (a still clip)
        out["float"] = bool(r["float"])
    if deck:
        out["deck"] = deck
    used_slots: set[int] = set()
    plist = []
    for i, p in enumerate(panels):
        w_ = f"{what} panels[{i}]"
        if isinstance(p, (int, float)) and not isinstance(p, bool):
            p = {"slide": p}
        if not isinstance(p, dict):
            raise ValueError(f"{w_}: a panel is {{\"slide\": n}} (from the stage's deck) or {{\"file_id\": \"<png|mp4|pptx>\"}}.")
        fid = p.get("file_id") or p.get("image") or p.get("video")
        if fid is not None and not isinstance(fid, str):
            raise ValueError(f"{w_}: file_id = a stored file (picture, clip or deck).")
        fid = fid or deck
        if not fid:
            raise ValueError(f"{w_}: which picture? Give file_id, or slide n with the stage's deck (\"deck\": \"<pptx>\").")
        kind = kind_of(fid)
        if kind is None:
            raise ValueError(f"{w_}: {fid!r} — a picture (png / jpg / webp), a clip (mp4 / mov / webm) or a deck (pptx).")
        q: dict[str, Any] = {"kind": kind, "file_id": fid}
        if kind == "slide":
            try:
                q["slide"] = int(p["slide"]) if p.get("slide") not in (None, "") else 1
            except (TypeError, ValueError):
                raise ValueError(f"{w_}: slide {p.get('slide')!r} — a slide number (1 = the first).") from None
            if q["slide"] < 1:
                raise ValueError(f"{w_}: slide numbers start at 1.")
        elif p.get("slide") not in (None, ""):
            notes.append(f"⚠ {w_}: slide is for a deck — {fid} is a {kind}, so it was not applied.")
        if p.get("box") is not None:
            bx = p["box"]
            if not (isinstance(bx, (list, tuple)) and len(bx) == 3):
                raise ValueError(f"{w_}: box = [cx, cy, w] — the centre and the width, 0–1 of the frame.")
            q["box"] = [round(_num(bx[0], f"{w_} box cx", -0.2, 1.2), 4), round(_num(bx[1], f"{w_} box cy", -0.2, 1.2), 4),
                        round(_num(bx[2], f"{w_} box w", 0.06, 0.9), 4)]
        else:
            if not slots:
                raise ValueError(f"{w_}: preset custom — give every panel its box [cx, cy, w].")
            k = p.get("slot")
            if k is None:
                k = next((j for j in range(1, len(slots) + 1) if j not in used_slots), None)
                if k is None:
                    raise ValueError(f"{w_}: the preset {preset} has {len(slots)} places — give this panel its box [cx, cy, w], "
                                     f"or another preset ({' | '.join(n for n, s in PRESETS.items() if len(s) >= len(panels))}).")
            try:
                k = int(k)
            except (TypeError, ValueError):
                raise ValueError(f"{w_}: slot {p.get('slot')!r} — the preset's place, 1–{len(slots)}.") from None
            if not 1 <= k <= len(slots):
                raise ValueError(f"{w_}: slot {k} — the preset {preset} has places 1–{len(slots)}.")
            used_slots.add(k)
            q["slot"] = k
        dflt = slots[q["slot"] - 1] if "slot" in q else None
        if dflt is None and preset == "auto" and "slot" in q and q["slot"] > len(panels):
            raise ValueError(f"{w_}: slot {q['slot']} — with preset auto the places are 1–{len(panels)}.")
        if p.get("tilt") is not None:
            tl = p["tilt"]
            if not (isinstance(tl, (list, tuple)) and len(tl) == 3):
                raise ValueError(f"{w_}: tilt = [rx, ry, rz] in degrees (e.g. [3, 14, -1.5]).")
            q["tilt"] = [round(_num(v, f"{w_} tilt", -45, 45), 2) for v in tl]
        if p.get("depth") not in (None, "") and not p.get("depth_auto"):      # given: kept; else the preset's / the face's
            q["depth"] = _choice(p.get("depth"), DEPTHS, f"{w_} depth", "front")
        elif dflt is not None:
            q["depth"], q["depth_auto"] = dflt[4], True
        elif p.get("depth") in DEPTHS:
            q["depth"], q["depth_auto"] = p["depth"], True
        q["enter"] = _choice(p.get("enter"), ENTERS, f"{w_} enter", "fly")
        q["frame"] = _choice(p.get("frame"), FRAMES, f"{w_} frame", "window" if kind == "image" else "plain")
        if p.get("float") is not None:
            q["float"] = bool(p["float"])
        t0 = timecode(p["at"], f"{w_} at") if p.get("at") is not None else a + 0.35 + STAGGER_S * i
        t1 = timecode(p["until"], f"{w_} until") if p.get("until") is not None else b
        t0, t1 = max(a, t0), min(b, t1)
        if t1 - t0 < 1.0:
            raise ValueError(f"{w_}: it would be shown {max(0.0, t1 - t0):.1f} s ({_tc(t0)}–{_tc(t1)}) — at least 1 s, inside the "
                             f"stage ({_tc(a)}–{_tc(b)}).")
        q["at"], q["until"] = round(t0, 3), round(t1, 3)
        plist.append(q)
    out["panels"] = plist
    out["background"] = _background(r.get("background"), what)
    tags = []
    for i, p in enumerate(pills):
        w_ = f"{what} pills[{i}]"
        if isinstance(p, str):
            p = {"text": p}
        if not isinstance(p, dict) or not str(p.get("text") or "").strip():
            raise ValueError(f"{w_}: a pill is {{\"text\": \"Claude Desktop\"}} (+ at, until, near: panel n, style).")
        txt = " ".join(str(p["text"]).split())
        if len(txt) > 40:
            notes.append(f"⚠ {w_}: “{txt[:40]}…” is long for a label — cut at 40 characters.")
            txt = txt[:40].rstrip()
        q = {"text": txt, "style": _choice(p.get("style"), PILL_STYLES, f"{w_} style", "accent")}
        if p.get("near") is not None:
            try:
                n = int(p["near"])
            except (TypeError, ValueError):
                raise ValueError(f"{w_}: near = the panel's number (1 = the first).") from None
            if not 1 <= n <= len(plist):
                raise ValueError(f"{w_}: near {n} — the stage has {len(plist)} panel(s).")
            q["near"] = n
        elif p.get("pos") is not None:
            ps = p["pos"]
            if not (isinstance(ps, (list, tuple)) and len(ps) == 2):
                raise ValueError(f"{w_}: pos = [x, y], the label's centre, 0–1 of the frame.")
            q["pos"] = [round(_num(ps[0], f"{w_} pos x", 0, 1), 4), round(_num(ps[1], f"{w_} pos y", 0, 1), 4)]
        t0 = timecode(p["at"], f"{w_} at") if p.get("at") is not None else a + PILL_DELAY_S + 0.5 * i
        t1 = timecode(p["until"], f"{w_} until") if p.get("until") is not None else b
        t0, t1 = max(a, t0), min(b, t1)
        if t1 - t0 < 0.8:
            raise ValueError(f"{w_}: it would be shown {max(0.0, t1 - t0):.1f} s — at least 0.8 s, inside the stage.")
        q["at"], q["until"] = round(t0, 3), round(t1, 3)
        tags.append(q)
    out["pills"] = tags
    return out


def text(ly: dict[str, Any]) -> str:
    n, m = len(ly.get("panels") or []), len(ly.get("pills") or [])
    bg = (ly.get("background") or {}).get("kind", "none")
    parts = [f"{n} panel{'s' if n != 1 else ''}"] if n else []
    parts.append(f"{bg} background" if bg != "none" else "no background")
    if m:
        parts.append(f"{m} pill{'s' if m != 1 else ''}")
    return "stage (" + ", ".join(parts) + ")"


def recut(ly: dict[str, Any], span: tuple[float, float], remap: Callable[[float], Optional[float]]) -> dict[str, Any]:
    """After a cut: the panels' and pills' own times on the new timeline (a part cut out → the next kept time), inside
    the stage's new span; one that is left with less than its minimum is dropped."""
    def move(items: list[dict[str, Any]], least: float) -> list[dict[str, Any]]:
        out = []
        for p in items or []:
            t0 = remap(float(p["at"])) if p.get("at") is not None else span[0]
            t1 = remap(float(p["until"])) if p.get("until") is not None else span[1]
            t0 = span[0] if t0 is None else max(span[0], t0)
            t1 = span[1] if t1 is None else min(span[1], t1)
            if t1 - t0 >= least:
                out.append({**p, "at": round(t0, 4), "until": round(t1, 4)})
        return out
    return {**ly, "panels": move(ly.get("panels") or [], 1.0), "pills": move(ly.get("pills") or [], 0.8)}


async def resolve(raw: Any, notes: list[str]) -> Any:
    """Before validation: a stock-video background ({"kind": "video", "stock": "words"}) → a fetched Pexels clip's
    file_id (+ its credit), so the stored layer names a file and a rebuild doesn't search again."""
    if not isinstance(raw, list):
        return raw
    out = []
    for x in raw:
        bg = x.get("background") if isinstance(x, dict) else None
        if isinstance(bg, dict) and bg.get("stock") and not bg.get("file_id"):
            from app.tools import stock_video
            try:
                fid, credit = await stock_video.fetch({"stock_video": str(bg["stock"])}, short=1080, orientation="landscape")
                x = {**x, "background": {**bg, "kind": "video", "file_id": fid, "credit": credit}}
                notes.append(f"Layer {x.get('id') or 'stage'}: the background is a free Pexels clip for “{bg['stock']}” ({credit}).")
            except Exception as e:                                   # noqa: BLE001 — the validation names what is missing
                notes.append(f"⚠ Layer {x.get('id') or 'stage'}: no stock video for “{bg['stock']}” ({str(e)[:120]}).")
        out.append(x)
    return out


def has_stock(raw: Any) -> bool:
    return isinstance(raw, list) and any(isinstance(x, dict) and isinstance(x.get("background"), dict)
                                         and x["background"].get("stock") and not x["background"].get("file_id") for x in raw)


# ---------- geometry (pure) ----------

def corners(w: float, h: float, tilt: tuple[float, float, float], *, z: float = 0.0, f: float = 3072.0) -> list[list[float]]:
    """The four corners (UL, UR, LL, LR) of a w × h rectangle centred on (0, 0), rotated by tilt (degrees: rx + = the top
    edge back, ry + = the right edge back, rz + = clockwise on screen), pushed back by z, seen by a pinhole camera of focal
    length f looking at its centre → screen offsets from the centre's projection (y down)."""
    rx, ry, rz = (math.radians(float(v)) for v in tilt)
    out = []
    for x, y in ((-w / 2, -h / 2), (w / 2, -h / 2), (-w / 2, h / 2), (w / 2, h / 2)):
        y1, z1 = y * math.cos(rx), -y * math.sin(rx)               # top edge (y < 0) back (z > 0) when rx > 0
        x2, z2 = x * math.cos(ry), z1 * math.cos(ry) + x * math.sin(ry)
        x3, y3 = x2 * math.cos(rz) - y1 * math.sin(rz), x2 * math.sin(rz) + y1 * math.cos(rz)
        s = f / (f + z + z2)
        out.append([x3 * s, y3 * s])
    return out


def _ease_out(u: float) -> float:
    u = max(0.0, min(1.0, u))
    return 1 - (1 - u) ** 3


def place(ly: dict[str, Any], k: int, W: int, H: int) -> dict[str, Any]:
    """Panel k (0-based) of a stage: centre (px), width (px), tilt, the side it comes from."""
    p = ly["panels"][k]
    if p.get("box"):
        cx, cy, w = p["box"]
        dflt_tilt = (2.0, 14.0 if cx < 0.5 else -14.0 if cx > 0.5 else 0.0, 0.0)
    else:
        cx, cy, w, dflt_tilt, _ = PRESETS[resolved_preset(ly)][int(p["slot"]) - 1]
    tilt = tuple(p.get("tilt") or dflt_tilt)
    side = -1 if cx < 0.45 else 1 if cx > 0.55 else 0
    return {"cx": cx * W, "cy": cy * H, "w": w * W, "tilt": tilt, "side": side, "top": cy < 0.5}


def resolved_preset(ly: dict[str, Any]) -> str:
    """The preset the panels are placed by: auto → the one chosen from the speaker's place (`_preset`, set at materialize),
    without it by the number of panels."""
    pr = ly.get("_preset") or ly.get("preset") or "auto"
    if pr == "auto":
        pr = BY_COUNT.get(len(ly.get("panels") or []), "grid4")
    return pr if pr in PRESETS else "grid4"


def auto_preset(n: int, zone: Optional[dict[str, Any]]) -> str:
    """preset auto: the speaker's face centre (median over the span) left of 0.42 → the right-side family, right of 0.58 →
    the left-side family, else (or unknown) by the count."""
    cx = (zone or {}).get("cx")
    if cx is not None and cx < 0.42:
        return BY_SIDE["right"].get(n, "right_grid6")
    if cx is not None and cx > 0.58:
        return BY_SIDE["left"].get(n, "left_grid6")
    return BY_COUNT.get(n, "grid4")


def panel_box(ly: dict[str, Any], k: int, W: int, H: int, aspect: float) -> tuple[float, float, float, float]:
    """Panel k's rest box on the frame, 0–1: (x0, y0, x1, y1)."""
    pl = place(ly, k, W, H)
    w, h = pl["w"], pl["w"] / max(0.4, min(3.0, aspect))
    return ((pl["cx"] - w / 2) / W, (pl["cy"] - h / 2) / H, (pl["cx"] + w / 2) / W, (pl["cy"] + h / 2) / H)


def covers(box: tuple[float, float, float, float], head: list[float]) -> float:
    """How much of the panel's box lies on the head box (0–1 of the panel)."""
    ix = max(0.0, min(box[2], head[2]) - max(box[0], head[0]))
    iy = max(0.0, min(box[3], head[3]) - max(box[1], head[1]))
    return ix * iy / max(1e-9, (box[2] - box[0]) * (box[3] - box[1]))


# ---------- where the speaker is (the face, over the stage's span) ----------

ZONE_V = 1
ZONE_STEP_S = 0.5


def speaker_zone(src: Path, t0: float, t1: float) -> Optional[dict[str, Any]]:
    """The speaker's head over [t0, t1) of the footage: the background deck's person segmenter (MediaPipe Selfie
    MULTICLASS, the vendored model — deck_matte.model_path) on a frame every ZONE_STEP_S; its hair + face-skin classes
    are the head → {head: [x0, y0, x1, y1] (0–1; the 10th / 90th percentile of the edges over the frames), cx (the median
    head centre), n (frames with a head), samples}; None with fewer than 3 heads, or without MediaPipe on this server (then
    the preset goes by the number of panels and the depth is as given). Cached by the file and the span (a JSON next to
    the stage pictures)."""
    import subprocess
    from app.tools import deck_matte
    if deck_matte.available():
        return None
    st = src.stat()
    key = _art_key({"zone": ZONE_V, "src": str(src.resolve().name), "size": st.st_size, "t0": round(t0, 2), "t1": round(t1, 2),
                    "model": deck_matte.MODEL_SHA256})
    cp = art_root() / f"zone-{key[:32]}.json"
    try:
        return json.loads(cp.read_text("utf-8"))
    except (OSError, ValueError):
        pass
    import mediapipe as mp
    import numpy as np
    from mediapipe.tasks.python import BaseOptions, vision
    w = h = 256
    proc = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t0):.3f}", "-i", str(src), "-t", f"{max(0.5, t1 - t0):.3f}",
                           "-vf", f"fps={1 / ZONE_STEP_S:g},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                          capture_output=True, timeout=240)
    buf = proc.stdout
    n = len(buf) // (w * h * 3)
    opts = vision.ImageSegmenterOptions(base_options=BaseOptions(model_asset_path=str(deck_matte.model_path(deck_matte.MODEL_NAME))),
                                        running_mode=vision.RunningMode.IMAGE, output_confidence_masks=True,
                                        output_category_mask=False)
    heads, cxs = [], []
    with vision.ImageSegmenter.create_from_options(opts) as seg:
        for i in range(n):
            fr = np.frombuffer(buf[i * w * h * 3:(i + 1) * w * h * 3], np.uint8).reshape(h, w, 3)
            ms = seg.segment(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(fr))).confidence_masks
            if len(ms) < 4:
                continue
            head = np.maximum(ms[1].numpy_view(), ms[3].numpy_view()).reshape(h, w) > 0.5     # hair | face skin
            if head.mean() < 0.003:
                continue
            ys, xs = np.nonzero(head)
            heads.append([xs.min() / w, ys.min() / h, (xs.max() + 1) / w, (ys.max() + 1) / h])
            cxs.append(float(np.median(xs)) / w)
    out = None
    if len(heads) >= 3:
        a = np.array(heads)
        out = {"head": [round(float(np.percentile(a[:, 0], 10)), 4), round(float(np.percentile(a[:, 1], 10)), 4),
                        round(float(np.percentile(a[:, 2], 90)), 4), round(float(np.percentile(a[:, 3], 90)), 4)],
               "cx": round(float(np.median(cxs)), 4), "n": len(heads), "samples": n}
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(out), "utf-8")
    return out


def _even(v: float) -> int:
    return max(4, int(round(v / 2)) * 2)


def panel_geometry(ly: dict[str, Any], k: int, W: int, H: int, aspect: float) -> dict[str, Any]:
    """The panel's picture box (w × h), the padded canvas (= its builder comp), the rest corners on the canvas and the
    entrance keys [[t, UL, UR, LL, LR, opacity]] in the comp's own time."""
    pl = place(ly, k, W, H)
    p = ly["panels"][k]
    w = pl["w"]
    h = w / max(0.4, min(3.0, aspect))
    m = min(W, H)
    pad = round(0.065 * m + 0.05 * max(w, h))
    cw, ch = _even(w + 2 * pad), _even(h + 2 * pad)
    f = FOCAL * W
    c = (cw / 2, ch / 2)

    def pins(tilt, z=0.0):
        """Corner Pin maps the layer's WHOLE source (the padded canvas) onto the four points: the points are the padded
        canvas's corners in the panel's plane, projected — the card inside lands where its own corners project."""
        return [[round(c[0] + x, 2), round(c[1] + y, 2)] for x, y in corners(cw, ch, tilt, z=z, f=f)]

    rest = pins(pl["tilt"])
    keys = []
    enter = p.get("enter", "fly")
    if enter in ("fly", "scale"):
        n = max(2, int(math.ceil(ENTER_S / 0.05)))
        rx, ry, rz = pl["tilt"]
        for i in range(n + 1):
            u = i / n
            e = _ease_out(u)
            back = (1 - e) * (0.55 if enter == "fly" else 0.8) * f
            turn = (1 - e) * (22.0 * (pl["side"] or 1) * -1 if enter == "fly" else 8.0)
            tl = (rx + (1 - e) * (6.0 if enter == "scale" else 0.0), ry + turn, rz)
            keys.append([round(u * ENTER_S, 4)] + pins(tl, z=back) + [round(100 * min(1.0, u / 0.45), 1)])
    elif enter == "fade":
        keys = [[0.0] + rest + [0.0], [round(ENTER_S * 0.7, 4)] + rest + [100.0]]
    shadow = {"distance": round(0.012 * m, 1), "softness": round(0.035 * m, 1), "opacity": 55}
    fl = {"ax": round(0.0028 * W, 2), "ay": round(0.0062 * H, 2), "ph": round((k * 2.39996) % (2 * math.pi), 4)}
    for (x, y) in corners(w, h, pl["tilt"], f=f):            # (the padding holds the tilt, the shadow and the float)
        assert abs(x) <= cw / 2 and abs(y) <= ch / 2, (x, y, cw, ch)
    return {"w": round(w, 1), "h": round(h, 1), "pad": pad, "canvas": [cw, ch], "pos": [round(pl["cx"]), round(pl["cy"])],
            "pins": rest, "keys": keys, "shadow": shadow, "float": fl, "side": pl["side"], "top": pl["top"]}


def pill_geometry(ly: dict[str, Any], k: int, W: int, H: int, aspects: dict[int, float]) -> dict[str, Any]:
    """Pill k: its font size, the comp size (generous: the builder measures the text and sizes the rounded box to it)
    and its centre — near a panel (over its outer upper corner, as in the owner's screenshot), at pos, or down the
    left side."""
    t = ly["pills"][k]
    m = min(W, H)
    size = round(0.04 * m)
    est_w = 0.62 * size * max(4, len(t["text"])) + 1.6 * size
    cw, ch = _even(est_w + 0.9 * size + 2 * 0.05 * m), _even(size * 1.6 + 2 * 0.05 * m)
    if t.get("near"):                          # a tab on the panel's top edge, from its left edge (its title stays readable)
        j = t["near"] - 1
        g = panel_geometry(ly, j, W, H, aspects.get(j, 16 / 9))
        cx = g["pos"][0] - g["w"] / 2 + 0.06 * g["w"] + est_w / 2
        cy = g["pos"][1] - g["h"] / 2 - 0.55 * size          # (overlapping the edge a little, above the title)
    elif t.get("pos"):
        cx, cy = t["pos"][0] * W, t["pos"][1] * H
    else:
        cx, cy = 0.03 * W + est_w / 2, (0.4 + 0.1 * k) * H
    cx = max(cw / 2, min(W - cw / 2, cx))
    cy = max(ch / 2, min(H - ch / 2, cy))
    return {"size": size, "w": cw, "h": ch, "pos": [round(cx), round(cy)]}


# ---------- colours ----------

def _hex_rgb(h: str) -> list[float]:
    h = h.lstrip("#")
    return [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]


def palette(acc: list[float], bg: dict[str, Any]) -> dict[str, list[float]]:
    """The stage's colours from the project's accent (or the background's own colour): the glow, a second hue for the
    gradients, the near-black base tinted by the accent."""
    c = _hex_rgb(bg["color"]) if bg.get("color") else [float(v) for v in acc[:3]]
    hh, ll, ss = colorsys.rgb_to_hls(*c)
    glow = list(colorsys.hls_to_rgb(hh, max(0.45, min(0.62, ll)), max(0.55, ss)))
    second = list(colorsys.hls_to_rgb((hh + 0.13) % 1.0, 0.5, max(0.5, ss * 0.9)))
    third = list(colorsys.hls_to_rgb((hh - 0.1) % 1.0, 0.42, max(0.45, ss * 0.8)))
    base = list(colorsys.hls_to_rgb(hh, 0.045, min(0.6, ss)))
    deep = list(colorsys.hls_to_rgb(hh, 0.1, min(0.65, ss)))
    return {"glow": glow, "second": second, "third": third, "base": base, "deep": deep, "accent": c}


def _u8(c: list[float], a: float = 1.0) -> tuple[int, int, int, int]:
    return tuple(max(0, min(255, round(v * 255))) for v in c[:3]) + (max(0, min(255, round(a * 255))),)


# ---------- the generated pictures (PIL; cached by content) ----------

def art_root() -> Path:
    from app.tools import preview_cache
    return preview_cache.cache_root() / ART_DIR


def _art_key(obj: Any) -> str:
    return hashlib.sha256(json.dumps({"v": ART_V, **obj}, sort_keys=True, default=str).encode()).hexdigest()


def _cached_png(key_obj: dict[str, Any], make: Callable[[], Any]) -> Path:
    """The picture for key_obj (made by make() → a PIL image when missing) under <cache>/_stage/<key>.png."""
    key = _art_key(key_obj)
    dst = art_root() / f"{key[:40]}.png"
    if dst.exists():
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.stem}.tmp-{uuid.uuid4().hex[:6]}.png")
    try:
        make().save(tmp, optimize=False, compress_level=3)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return dst


def _round_mask(w: int, h: int, r: float, ss: int = 4):
    from PIL import Image, ImageDraw
    big = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=max(1, round(r * ss)), fill=255)
    return big.resize((w, h), Image.LANCZOS)


def card_image(src: Optional[Path], w: int, h: int, pad: int, frame: str, *, video: bool = False):
    """A panel's picture: the content cover-fitted into w × h (below a window bar for frame "window"), rounded corners,
    a thin light rim, on a transparent canvas padded by pad. video=True: the content area is left transparent (the clip
    plays under this frame in the engine)."""
    from PIL import Image, ImageDraw
    r = max(6.0, 0.028 * min(w, h))
    bar = round(max(14, 0.06 * h)) if frame == "window" else 0
    card = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if bar:
        ImageDraw.Draw(card).rectangle((0, 0, w, bar), fill=(232, 234, 239, 255))
        d = ImageDraw.Draw(card)
        rr = max(2.5, bar * 0.17)
        for j, col in enumerate(((236, 106, 94), (244, 191, 79), (97, 197, 84))):
            x = bar * 0.55 + j * rr * 3.1
            d.ellipse((x - rr, bar / 2 - rr, x + rr, bar / 2 + rr), fill=col + (255,))
    if src is not None and not video:
        with Image.open(src) as im0:
            im = im0.convert("RGB")
        ch_ = h - bar
        k = max(w / im.width, ch_ / im.height)
        im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
        x0, y0 = (im.width - w) // 2, (im.height - ch_) // 2
        card.paste(im.crop((x0, y0, x0 + w, y0 + ch_)), (0, bar))
    elif not video:
        ImageDraw.Draw(card).rectangle((0, bar, w, h), fill=(245, 246, 248, 255))
    mask = _round_mask(w, h, r)
    if frame != "none":                                     # the thin rim (light, half see-through)
        rim = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(rim).rounded_rectangle((0.5, 0.5, w - 1.5, h - 1.5), radius=round(r), outline=(255, 255, 255, 120),
                                              width=max(1, round(min(w, h) / 300)))
        card = Image.alpha_composite(card, rim)
    from PIL import ImageChops
    card.putalpha(ImageChops.multiply(card.getchannel("A"), mask) if video else mask)
    out = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    out.paste(card, (pad, pad), card)
    return out


def _gradient_base(W: int, H: int, pal: dict[str, list[float]], intensity: float, seed: int):
    """The opaque base: near-black tinted by the accent, a soft lighter region low in the middle (behind the speaker) and a
    faint second hue in one corner; ±1 LSB noise against banding. Drawn at half size (the engine scales it 200 %)."""
    import numpy as np
    from PIL import Image
    w, h = max(8, W // 2), max(8, H // 2)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    xn, yn = xx / w, yy / h
    base, deep, sec = (np.array(pal[k], np.float32) for k in ("base", "deep", "second"))
    d1 = np.sqrt(((xn - 0.5) * 1.5) ** 2 + (yn - 0.62) ** 2)
    g1 = np.clip(1 - d1 / 0.75, 0, 1) ** 1.6 * (0.55 + 0.45 * intensity)
    d2 = np.sqrt(((xn - 0.92) * 1.4) ** 2 + (yn - 0.95) ** 2)
    g2 = np.clip(1 - d2 / 0.6, 0, 1) ** 2 * 0.35 * intensity
    img = base[None, None, :] * (1 - g1[..., None]) + deep[None, None, :] * g1[..., None]
    img = img * (1 - g2[..., None]) + sec[None, None, :] * 0.35 * g2[..., None]
    rng = np.random.default_rng(seed)
    img = np.clip(img * 255 + rng.uniform(-0.5, 0.5, img.shape), 0, 255).astype(np.uint8)
    return Image.fromarray(img, "RGB").convert("RGBA")


def _glow_shape(S: int, pal: dict[str, list[float]], arms: int, intensity: float):
    """The big glowing shape: `arms` rounded bars through the centre (an asterisk), blurred, in the glow colour — drawn
    once; the engine only rotates it (a blur doesn't care about rotation)."""
    from PIL import Image, ImageDraw, ImageFilter
    big = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(big)
    L, T = S * 0.44, S * 0.075
    for j in range(arms):
        a = math.pi * j / arms
        dx, dy = math.cos(a), math.sin(a)
        nx, ny = -dy, dx
        pts = [(S / 2 + dx * L + nx * T, S / 2 + dy * L + ny * T), (S / 2 - dx * L + nx * T, S / 2 - dy * L + ny * T),
               (S / 2 - dx * L - nx * T, S / 2 - dy * L - ny * T), (S / 2 + dx * L - nx * T, S / 2 + dy * L - ny * T)]
        d.polygon(pts, fill=255)
        for sx in (1, -1):
            cx, cy = S / 2 + sx * dx * L, S / 2 + sx * dy * L
            d.ellipse((cx - T, cy - T, cx + T, cy + T), fill=255)
    core = big.filter(ImageFilter.GaussianBlur(S * 0.022))
    halo = big.filter(ImageFilter.GaussianBlur(S * 0.07))
    import numpy as np
    a = np.clip(np.asarray(core, np.float32) * (0.62 + 0.3 * intensity) + np.asarray(halo, np.float32) * 0.5, 0, 255)
    rgb = np.zeros((S, S, 3), np.float32) + np.array(pal["glow"], np.float32) * 255
    out = np.dstack([rgb, a]).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def _blob(S: int, col: list[float], alpha: float):
    import numpy as np
    from PIL import Image
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
    d = np.sqrt((xx - S / 2) ** 2 + (yy - S / 2) ** 2) / (S / 2)
    a = np.clip(1 - d, 0, 1) ** 2.2 * 255 * alpha
    rgb = np.zeros((S, S, 3), np.float32) + np.array(col, np.float32) * 255
    return Image.fromarray(np.dstack([rgb, a]).astype(np.uint8), "RGBA")


def _cluster(S: int, pts: list[tuple[float, float]], edges: list[tuple[int, int]], col: list[float], dot: float):
    """A constellation: points (with a small glow) joined by thin lines, on S × S."""
    from PIL import Image, ImageDraw, ImageFilter
    ss = 3
    im = Image.new("RGBA", (S * ss, S * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = _u8(col, 0.42)
    for i, j in edges:
        d.line([(pts[i][0] * ss, pts[i][1] * ss), (pts[j][0] * ss, pts[j][1] * ss)], fill=c, width=max(1, round(ss * 0.9)))
    for x, y in pts:
        r = dot * ss
        d.ellipse((x * ss - r, y * ss - r, x * ss + r, y * ss + r), fill=_u8([min(1, v * 0.4 + 0.6) for v in col], 1.0))
    im = im.resize((S, S), Image.LANCZOS)
    glow = im.filter(ImageFilter.GaussianBlur(dot * 2.2))
    return Image.alpha_composite(glow, im)


def _dust(W: int, H: int, col: list[float], n: int, seed: int):
    import random
    from PIL import Image, ImageDraw
    rnd = random.Random(seed)
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for _ in range(n):
        x, y, r = rnd.uniform(0, W), rnd.uniform(0, H), rnd.uniform(0.6, 1.8) * W / 1920
        d.ellipse((x - r, y - r, x + r, y + r), fill=_u8([min(1, v * 0.3 + 0.7) for v in col], rnd.uniform(0.25, 0.8)))
    return im


def _halftone(W: int, H: int, pitch: int, col: list[float], alpha: float):
    """A dot grid (one dot per pitch), (W + pitch) × (H + pitch): sliding by exactly one pitch per period repeats."""
    from PIL import Image, ImageDraw
    ss = 2
    w, h = W + pitch, H + pitch
    im = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    r = pitch * 0.2 * ss
    c = _u8(col, alpha)
    for j in range(0, h // pitch + 1):
        for i in range(0, w // pitch + 1):
            x, y = (i + 0.5 + 0.5 * (j % 2)) * pitch * ss, (j + 0.5) * pitch * ss
            d.ellipse((x - r, y - r, x + r, y + r), fill=c)
    return im.resize((w, h), Image.LANCZOS)


def _vignette(W: int, H: int, base: list[float], strength: float):
    """Dark in the middle (behind the speaker), clear at the edges — the halftone fades out toward him."""
    import numpy as np
    from PIL import Image
    w, h = max(8, W // 2), max(8, H // 2)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.sqrt(((xx / w - 0.5) * 1.6) ** 2 + (yy / h - 0.55) ** 2)
    a = np.clip(1 - d / 0.62, 0, 1) ** 1.2 * 255 * strength
    rgb = np.zeros((h, w, 3), np.float32) + np.array(base, np.float32) * 255
    return Image.fromarray(np.dstack([rgb, a]).astype(np.uint8), "RGBA")


def background_sprites(bg: dict[str, Any], acc: list[float], W: int, H: int, fps: float,
                       emit: Callable[[str, dict[str, Any], Callable[[], Any]], str]) -> dict[str, Any]:
    """The background's builder description: {sprites: […], video?} — every sprite a generated picture (emit(kind, key,
    make) → its asset name) with its rest place and its periodic motions (orbit, spin, twinkle, breathe, slide). The
    numbers are deterministic (seeded by the background's own parameters)."""
    import random
    kind = bg.get("kind", "glow")
    inten = float(bg.get("intensity", 0.7))
    pal = palette(acc, bg)
    seed = int(_art_key({"bg": bg, "acc": [round(float(v), 4) for v in acc[:3]], "W": W, "H": H})[:8], 16)
    rnd = random.Random(seed)
    sprites: list[dict[str, Any]] = []
    pk = {"pal": {k: [round(v, 4) for v in c] for k, c in pal.items()}, "W": W, "H": H, "i": inten}
    base = emit("base", {**pk, "kind": "base"}, lambda: _gradient_base(W, H, pal, inten, seed))
    sprites.append({"file": base, "pos": [W / 2, H / 2], "scale": 200.0, "opacity": 100})
    if kind == "glow":
        S = max(64, round(0.98 * H / 2))
        star = emit("glow", {**pk, "kind": "glow", "S": S, "arms": 3}, lambda: _glow_shape(S, pal, 3, inten))
        halo = emit("blob", {**pk, "kind": "blob2", "S": 256, "c": "deep"}, lambda: _blob(256, pal["glow"], 0.35))
        sprites.append({"file": halo, "pos": [W * 0.5, H * 0.55], "scale": round(1.25 * H / 256 * 100, 2), "opacity": 70,
                        "breathe": [0.05, 1, 0.0]})
        sprites.append({"file": star, "pos": [W * 0.5, H * 0.5], "scale": 200.0, "opacity": round(70 + 25 * inten),
                        "spin": 60.0, "breathe": [0.035, 1, 1.3]})
        dust = emit("dust", {**pk, "kind": "dust", "n": 90}, lambda: _dust(W, H, pal["glow"], 90, seed))
        sprites.append({"file": dust, "pos": [W / 2, H / 2], "scale": 100.0, "opacity": 60,
                        "orbit": [0.006 * W, 0.008 * H, 1, 0.0], "twinkle": [35, 70, 2, 0.0]})
    elif kind == "particles":
        blob = emit("blob", {**pk, "kind": "blob", "S": 256}, lambda: _blob(256, pal["glow"], 0.5))
        sprites.append({"file": blob, "pos": [W * 0.78, H * 0.62], "scale": round(1.1 * H / 256 * 100, 2), "opacity": 55,
                        "breathe": [0.06, 1, 0.0], "orbit": [0.02 * W, 0.015 * H, 1, 0.7]})
        S = round(0.2 * W)
        for j in range(9):
            pts = [(rnd.uniform(0.12, 0.88) * S, rnd.uniform(0.12, 0.88) * S) for _ in range(rnd.randint(4, 7))]
            edges = _mst(pts) + ([(0, len(pts) - 1)] if len(pts) > 4 and rnd.random() < 0.5 else [])
            col = pal["glow"] if j % 3 else pal["second"]
            dot = max(1.2, 0.0016 * W)
            name = emit(f"cluster{j}", {**pk, "kind": "cluster", "j": j, "pts": [[round(x, 1), round(y, 1)] for x, y in pts],
                                        "e": edges, "dot": dot}, lambda pts=pts, edges=edges, col=col, dot=dot: _cluster(S, pts, edges, col, dot))
            # around the edges of the frame, the middle (the speaker) mostly free
            gx = [0.1, 0.9, 0.12, 0.88, 0.3, 0.7, 0.5, 0.22, 0.8][j]
            gy = [0.2, 0.18, 0.78, 0.8, 0.12, 0.1, 0.9, 0.5, 0.48][j]
            sprites.append({"file": name, "pos": [gx * W, gy * H], "scale": 100.0, "opacity": 85,
                            "orbit": [rnd.uniform(0.006, 0.014) * W, rnd.uniform(0.006, 0.014) * H, rnd.choice([1, 1, 2]),
                                      round(rnd.uniform(0, 6.283), 3)],
                            "rot": [round(rnd.uniform(1.5, 4.0), 2), 1, round(rnd.uniform(0, 6.283), 3)],
                            "twinkle": [55, 100, rnd.choice([1, 2]), round(rnd.uniform(0, 6.283), 3)]})
        dust = emit("dust", {**pk, "kind": "dust", "n": 160}, lambda: _dust(W, H, pal["glow"], 160, seed))
        sprites.append({"file": dust, "pos": [W / 2, H / 2], "scale": 100.0, "opacity": 70,
                        "orbit": [0.005 * W, 0.007 * H, 1, 1.1], "twinkle": [40, 80, 2, 0.4]})
    elif kind == "halftone":
        pitch = max(8, round(0.028 * H))
        dots = emit("halftone", {**pk, "kind": "halftone", "p": pitch}, lambda: _halftone(W, H, pitch, pal["glow"], 0.55 + 0.3 * inten))
        sprites.append({"file": dots, "pos": [W / 2 + pitch / 2, H / 2 + pitch / 2], "scale": 100.0, "opacity": 100,
                        "slide": [-pitch, -pitch]})
        vig = emit("vignette", {**pk, "kind": "vignette"}, lambda: _vignette(W, H, pal["base"], 0.97))
        sprites.append({"file": vig, "pos": [W / 2, H / 2], "scale": 200.0, "opacity": 100})
        blob = emit("blob", {**pk, "kind": "blob3", "S": 256}, lambda: _blob(256, pal["second"], 0.45))
        sprites.append({"file": blob, "pos": [W * 0.85, H * 0.2], "scale": round(0.9 * H / 256 * 100, 2), "opacity": 60,
                        "orbit": [0.03 * W, 0.02 * H, 1, 0.0], "breathe": [0.08, 1, 0.5]})
    elif kind == "gradient":
        for j, (ck, gx, gy, sz) in enumerate((("glow", 0.25, 0.35, 1.3), ("second", 0.78, 0.3, 1.1), ("third", 0.6, 0.85, 1.2))):
            blob = emit(f"gblob{j}", {**pk, "kind": "gblob", "c": ck}, lambda ck=ck: _blob(256, pal[ck], 0.7))
            sprites.append({"file": blob, "pos": [gx * W, gy * H], "scale": round(sz * H / 256 * 100, 2), "opacity": 80,
                            "orbit": [0.09 * W, 0.07 * H, 1, round(j * 2.094, 3)], "breathe": [0.1, 1, round(j * 1.3, 3)]})
    out: dict[str, Any] = {"sprites": sprites}
    if kind == "video":
        vig = emit("vignette", {**pk, "kind": "vignette-v"}, lambda: _vignette(W, H, pal["base"], 0.55))
        out["video"] = {"dim": round(0.55 - 0.35 * inten, 3), "dim_color": pal["base"]}
        out["sprites"] = [sprites[0]] + [{"file": vig, "pos": [W / 2, H / 2], "scale": 200.0, "opacity": 100, "over": True}]
    return out


def _mst(pts: list[tuple[float, float]]) -> list[tuple[int, int]]:
    left, done, edges = set(range(1, len(pts))), {0}, []
    while left:
        i, j = min(((i, j) for i in done for j in left), key=lambda e: math.dist(pts[e[0]], pts[e[1]]))
        edges.append((i, j))
        done.add(j)
        left.discard(j)
    return edges


# ---------- materialize: the pictures into the build folder ----------

async def materialize(layers: list[dict[str, Any]], work: Path, W: int, H: int, acc: list[float], notes: list[str], *,
                      fps: float = 30.0, footage: Optional[Path] = None) -> list[str]:
    """For every stage: the panels' pictures (slides from the deck — deck_layer.slide_files, cached by the deck's
    content —, pictures, clips) made into framed, padded cards, the background's generated pictures, all content-named
    in the build folder; the layer's runtime `_art`. → the asset names to add to the project."""
    names: list[str] = []

    def put(path: Path, name: str) -> str:
        if not (work / name).exists():
            try:
                os.link(path, work / name)
            except OSError:
                shutil.copyfile(path, work / name)
        if name not in names:
            names.append(name)
        return name

    for ly in layers:
        if ly.get("type") != "stage":
            continue
        ly["_fps"] = float(fps)
        zone = None
        if footage is not None and ly.get("panels"):
            try:
                zone = await asyncio.to_thread(speaker_zone, footage, float(ly["at"]), float(ly["at"]) + float(ly["dur"]))
            except Exception as e:                                   # noqa: BLE001 — the preset by the count, the depth as given
                log.warning("stage %s: speaker zone failed: %s", ly["id"], e)
        ly["_zone"] = zone
        if (ly.get("preset") or "auto") == "auto":
            ly["_preset"] = auto_preset(len(ly["panels"]), zone)
            notes.append(f"Layer {ly['id']} (stage): preset {ly['_preset']}"
                         + (f" — you are {'left of' if zone['cx'] < 0.42 else 'right of' if zone['cx'] > 0.58 else 'in'} the middle "
                            f"there (face centre {zone['cx']:.2f})." if zone else " (by the number of panels)."))
        slots = PRESETS[resolved_preset(ly)]
        for k, p in enumerate(ly["panels"]):
            if p.get("depth_auto") or not p.get("depth"):
                p["depth"], p["depth_auto"] = (slots[int(p["slot"]) - 1][4] if p.get("slot") and int(p["slot"]) <= len(slots)
                                               else "front"), True
        art: dict[str, Any] = {"cards": {}, "aspects": {}}
        decks: dict[str, Any] = {}
        for k, p in enumerate(ly.get("panels") or []):
            try:
                if p["kind"] == "slide":
                    from app.tools import deck_layer
                    if p["file_id"] not in decks:
                        decks[p["file_id"]] = await deck_layer.slide_files(p["file_id"], W, H)
                    files = decks[p["file_id"]]["files"]
                    if p["slide"] > len(files):
                        notes.append(f"⚠ Layer {ly['id']}: panel {k + 1} — the deck has {len(files)} slides, not {p['slide']}; "
                                     "left out.")
                        continue
                    src, sha = files[p["slide"] - 1], f"{decks[p['file_id']]['sha']}:{p['slide']}"
                else:
                    from app.tools import crafts
                    src = crafts.existing(p["file_id"])
                    sha = await asyncio.to_thread(_file_sha, src)
            except FileNotFoundError:
                notes.append(f"⚠ Layer {ly['id']}: panel {k + 1} — the file {p['file_id']} isn't stored (any more); left out.")
                continue
            aspect = await asyncio.to_thread(_aspect_of, src, p["kind"])
            if zone and covers(panel_box(ly, k, W, H, aspect), zone["head"]) > 0.04:
                if p.get("depth_auto") and p.get("depth") != "behind":
                    p["depth"] = "behind"
                    notes.append(f"Layer {ly['id']}: panel {k + 1} would cover your face — it goes behind you.")
                elif p.get("depth") == "front":
                    notes.append(f"⚠ Layer {ly['id']}: panel {k + 1} (depth front) covers your face in places — depth "
                                 "\"behind\" puts it behind you.")
            g = panel_geometry(ly, k, W, H, aspect)
            w, h = round(g["w"]), round(g["h"])
            key = {"card": sha, "w": w, "h": h, "pad": g["pad"], "frame": p["frame"], "video": p["kind"] == "video"}
            png = await asyncio.to_thread(_cached_png, key, lambda src=src, w=w, h=h, g=g, p=p: card_image(
                None if p["kind"] == "video" else src, w, h, g["pad"], p["frame"], video=p["kind"] == "video"))
            card = {"file": put(png, f"stage-{png.stem[:20]}.png"), "aspect": aspect}
            if p["kind"] == "video":
                cp = await clip_copy(src, sha, min(W, _even(g["w"] * 1.25)))
                card["video"] = put(cp, f"stageclip-{cp.stem[5:25]}.mp4")
                card["video_size"] = await asyncio.to_thread(_video_size, cp)
            art["cards"][k] = card
            art["aspects"][k] = aspect
        bg = ly.get("background") or {"kind": "none"}
        if bg.get("kind") != "none":
            fps = float(ly.get("_fps") or 30.0)

            def emit(tag: str, key: dict[str, Any], make: Callable[[], Any]) -> str:
                png = _cached_png(key, make)
                return put(png, f"stagebg-{png.stem[:20]}.png")
            try:
                art["bg"] = await asyncio.to_thread(background_sprites, bg, acc, W, H, fps, emit)
            except Exception as e:                                   # noqa: BLE001 — the panels still go
                log.warning("stage %s background failed: %s", ly["id"], e)
                notes.append(f"⚠ Layer {ly['id']}: the {bg.get('kind')} background couldn't be drawn ({str(e)[:100]}) — left out.")
            if bg.get("kind") == "video" and art.get("bg"):
                from app.tools import crafts
                try:
                    src = crafts.existing(bg["file_id"])
                    sha = await asyncio.to_thread(_file_sha, src)
                    cp = await clip_copy(src, sha, W)
                    art["bg"]["video"].update(file=put(cp, f"stagebgclip-{cp.stem[5:25]}.mp4"),
                                              size=await asyncio.to_thread(_video_size, cp))
                except FileNotFoundError:
                    notes.append(f"⚠ Layer {ly['id']}: the background clip {bg['file_id']} isn't stored (any more) — left out.")
                    art.pop("bg", None)
        ly["_art"] = art
    return names


CLIP_V = 1


async def clip_copy(src: Path, sha: str, max_w: int) -> Path:
    """A panel's / the background's clip as the engine draws it reliably: 8-bit H.264 BT.709 (an iPhone HDR / 10-bit / HEVC
    clip tone-mapped as the working copies are — videonorm.tonemap_filters; measured 10-26: a 10-bit High-10 clip came
    out transparent in the engine), at most max_w wide (a panel is a third of the frame), no sound. Cached by the clip's
    content and the size (<cache>/_stage/clip-<key>.mp4)."""
    from app.tools import mediaprobe, videonorm
    key = _art_key({"clip": sha, "w": int(max_w), "v": CLIP_V})
    dst = art_root() / f"clip-{key[:32]}.mp4"
    if dst.exists():
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.stem}.tmp-{uuid.uuid4().hex[:6]}.mp4")
    vf = videonorm.tonemap_filters(await mediaprobe.info(src)) + [f"scale='min({int(max_w)},iw)':-2:flags=lanczos", "format=yuv420p"]
    from app.convert.engines import run
    try:
        await run("ffmpeg", "-y", "-v", "error", "-i", str(src), "-an", "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "veryfast",
                  "-crf", "18", "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                  "-movflags", "+faststart", str(tmp), timeout=900)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return dst


def _file_sha(path: Path) -> str:
    st = path.stat()
    if st.st_size > 64 * 1024 * 1024:                       # (a long clip: its name + size, never re-read)
        return hashlib.sha256(f"{path.name}:{st.st_size}".encode()).hexdigest()
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _aspect_of(src: Path, kind: str) -> float:
    if kind == "video":
        w, h = _video_size(src)
        return w / max(1, h)
    from PIL import Image
    with Image.open(src) as im:
        return im.width / max(1, im.height)


def _video_size(src: Path) -> list[int]:
    import subprocess
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                              "-of", "json", str(src)], capture_output=True, text=True, timeout=30, check=True).stdout
        s = json.loads(out)["streams"][0]
        return [int(s["width"]), int(s["height"])]
    except Exception:                                           # noqa: BLE001
        return [1920, 1080]


# ---------- building (video.layer_comps) ----------

GROUP_SLACK_S = 2.0             # a panel entering within this of the stage's start and staying to its end joins its group


def grouping(ly: dict[str, Any], total: Optional[float] = None) -> dict[str, Any]:
    """Which builder layer each part goes into. The background and the behind panels that come in at the start and stay
    to the end are ONE layer ("b"), the front ones likewise ("f"): the browser then plays two looping clips instead of one
    per panel (measured 10-26 on S4, headless Chrome under load: 5 full-frame stage clips + the footage + the matte = 4.7
    drawn frames / s), and the engine composites them in one pass. A panel with its own times (it comes later, or goes
    earlier) is its own layer ("pK"): its entrance / exit would break the group's loop. Pills: one layer each ("tK").
    → {"b": [panel indexes] | None (no group), "f": […] | None, "solo": [panel indexes]}"""
    a = float(ly["at"])
    b = a + float(ly["dur"])
    if total is not None:
        b = min(b, float(total))
    has_bg = (ly.get("background") or {}).get("kind", "none") != "none"
    out: dict[str, Any] = {"b": [] if has_bg else None, "f": None, "solo": []}
    for k, p in enumerate(ly.get("panels") or []):
        plain = float(p["at"]) <= a + GROUP_SLACK_S and float(p["until"]) >= b - 0.05
        if not plain:
            out["solo"].append(k)
            continue
        g = "b" if p.get("depth") == "behind" else "f"
        if out[g] is None:
            out[g] = []
        out[g].append(k)
    if out["b"] == [] and not has_bg:
        out["b"] = None
    if out["f"] is not None and len(out["f"]) == 1:          # (one front panel: its own layer, the same clip count)
        out["solo"].append(out["f"][0])
        out["f"] = None
    out["solo"].sort()
    return out


def _panel_part(ly: dict[str, Any], k: int, W: int, H: int, card: dict[str, Any], pf: int, fps: float) -> dict[str, Any]:
    p = ly["panels"][k]
    g = panel_geometry(ly, k, W, H, float(card.get("aspect") or 16 / 9))
    m = min(W, H)
    enter = p.get("enter", "fly")
    st: dict[str, Any] = {"role": "panel", "depth": p.get("depth", "front"), "file": card["file"], "canvas": g["canvas"],
                          "card": [g["w"], g["h"]], "pins": g["pins"], "keys": g["keys"], "shadow": g["shadow"],
                          "float": None if p.get("float") is False or (ly.get("float") is False and p.get("float") is not True) else
                          {**g["float"], "pf": pf, "fps": round(fps, 6)}}
    if card.get("video"):
        st["video"] = {"file": card["video"], "size": card.get("video_size") or [1920, 1080]}
    sl = [round(0.07 * W * (g["side"] or 0)), round(0 if g["side"] else -0.07 * m)] if enter == "fly" else [0, 0]
    return {"g": g, "st": st, "slide": sl, "enter": enter,
            "label": f"slide {p['slide']}" if p["kind"] == "slide" else p["kind"]}


def items(ly: dict[str, Any], W: int, H: int, total: float, acc: list[float], fps: float = 30.0) -> list[dict[str, Any]]:
    """The stage's builder layers (D.layers items, in drawing order): the behind group (the background + the behind
    panels), the behind panels of their own, the front group, the front panels of their own, the pills. Each carries
    `stage` (what app/tools/stage_builder.jsx draws) and is named so the live preview knows it (ITEM_RE)."""
    sid = ly["id"]
    at = float(ly["at"])
    end = min(at + float(ly["dur"]), float(total))
    art = ly.get("_art") or {}
    cards = {int(k): v for k, v in (art.get("cards") or {}).items()}
    pf = max(2, int(round(PERIOD_S * fps)))
    m = min(W, H)
    grp = grouping(ly, total)
    bg = ly.get("background") or {"kind": "none"}
    out: list[dict[str, Any]] = []

    def group(key: str, ks: list[int], with_bg: bool) -> Optional[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        if with_bg and art.get("bg"):
            parts.append({"role": "bg", "pf": pf, "fps": round(fps, 6), **art["bg"]})
        n = 0
        for k in ks:
            if k not in cards:
                continue
            pp = _panel_part(ly, k, W, H, cards[k], pf, fps)
            g = pp["g"]
            t0 = round(max(0.0, float(ly["panels"][k]["at"]) - at), 3)
            parts.append({**pp["st"], "pos": g["pos"], "t0": t0, "slide": pp["slide"], "dur": round(end - at - t0, 3)})
            n += 1
        if not parts:
            return None
        depth = "behind" if key == "b" else "front"
        what = (f"{bg['kind']} + " if with_bg and art.get("bg") else "") + f"{n} panel{'s' if n != 1 else ''}"
        if not any(x["role"] == "bg" or x.get("float") for x in parts):
            what += STILL                                   # (nothing moves after the entrance: the live clip is a still)
        return {"id": f"{sid}.{key}", "name": f"Layer {sid}.{key} – stage {depth} ({what})", "at": round(at, 3),
                "dur": round(end - at, 3), "w": int(W), "h": int(H), "pos": [round(W / 2), round(H / 2)], "elements": [],
                "anim": {"in": "fade" if with_bg and art.get("bg") else "none", "out": "fade"}, "slide": [0, 0],
                "stage": {"role": "group", "depth": depth, "parts": parts}}

    def solo(k: int) -> Optional[dict[str, Any]]:
        if k not in cards:
            return None
        p = ly["panels"][k]
        a, b = max(at, float(p["at"])), min(end, float(p["until"]))
        if b - a < 0.5:
            return None
        pp = _panel_part(ly, k, W, H, cards[k], pf, fps)
        g, enter = pp["g"], pp["enter"]
        return {"id": f"{sid}.p{k + 1}", "name": f"Layer {sid}.p{k + 1} – stage panel {k + 1} ({pp['label']}"
                                                 f"{'' if pp['st'].get('float') else STILL})",
                "at": round(a, 3), "dur": round(b - a, 3), "w": int(g["canvas"][0]), "h": int(g["canvas"][1]),
                "pos": g["pos"], "elements": [],
                "anim": {"in": "slide" if enter == "fly" else "none",
                         "out": "slide" if enter == "fly" else "pop" if enter == "scale" else "fade"},
                "slide": pp["slide"], "stage": pp["st"]}

    behind_solo = [k for k in grp["solo"] if ly["panels"][k].get("depth") == "behind"]
    front_solo = [k for k in grp["solo"] if ly["panels"][k].get("depth") != "behind"]
    if grp["b"] is not None:
        out.append(group("b", grp["b"], True))
    elif art.get("bg") and bg.get("kind") != "none":
        out.append(group("b", [], True))
    out += [solo(k) for k in behind_solo]
    if grp["f"] is not None:
        out.append(group("f", grp["f"], False))
    out += [solo(k) for k in front_solo]
    out = [x for x in out if x]
    aspects = {int(k): float(v) for k, v in (art.get("aspects") or {}).items()}
    pal = palette(acc, bg if bg.get("color") else {})
    for k, t in enumerate(ly.get("pills") or []):
        a, b = max(at, float(t["at"])), min(end, float(t["until"]))
        if b - a < 0.5:
            continue
        g = pill_geometry(ly, k, W, H, aspects)
        style = t.get("style", "accent")
        fill = {"accent": pal["glow"], "light": [0.97, 0.97, 0.98], "dark": [0.08, 0.09, 0.12]}[style]
        ink = [0.97, 0.97, 0.98] if style == "dark" else [0.04, 0.05, 0.07]
        if style == "accent" and sum(fill) / 3 < 0.42:
            ink = [0.98, 0.98, 0.99]
        out.append({"id": f"{sid}.t{k + 1}", "name": f"Layer {sid}.t{k + 1} – stage pill “{t['text'][:24]}”",
                    "at": round(a, 3), "dur": round(b - a, 3), "w": int(g["w"]), "h": int(g["h"]), "pos": g["pos"],
                    "elements": [], "anim": {"in": "none", "out": "pop"}, "slide": [0, 0],
                    "stage": {"role": "pill", "text": t["text"], "size": g["size"], "fill": [round(v, 4) for v in fill],
                              "ink": ink, "radius": round(g["size"] * 0.42, 1), "pad": [round(g["size"] * 0.55, 1), round(g["size"] * 0.3, 1)],
                              "shadow": {"distance": round(0.006 * m, 1), "softness": round(0.018 * m, 1), "opacity": 45}}})
    return out


def box(ly: dict[str, Any], W: int, H: int) -> dict[str, Any]:
    """The stage's box on the frame (video._overlaps reads it; a stage covers the frame on purpose — video._loose)."""
    return {"w": int(W), "h": int(H), "cx": round(W / 2), "cy": round(H / 2), "slide": [0, 0], "split": None, "els": []}


def final_notes(layers: list[dict[str, Any]]) -> list[str]:
    """What this server's final render can't do: without MediaPipe the speaker isn't cut out, so the background and the
    behind panels cover him there (a finding, so nobody is surprised by the exported video)."""
    from app.tools import deck_matte
    if deck_matte.available() is None:
        return []
    return [f"Layer {ly['id']} (stage): the live preview puts you in front of its background / behind panels; this server "
            "can't cut you out for the final video — there they cover you." for ly in layers if behind_span(ly)]


# ---------- depth: the speaker in front of the background and the behind panels ----------

def behind_span(ly: dict[str, Any]) -> Optional[list[float]]:
    """The output-time span where the speaker is drawn again in front of this stage's behind parts (the background's span,
    or the behind panels' union); None without behind parts."""
    if not isinstance(ly, dict) or ly.get("type") != "stage":
        return None
    a, b = float(ly["at"]), float(ly["at"]) + float(ly["dur"])
    if (ly.get("background") or {}).get("kind", "none") != "none":
        return [a, b]
    bs = [(max(a, float(p["at"])), min(b, float(p["until"]))) for p in ly.get("panels") or [] if p.get("depth") == "behind"]
    if not bs:
        return None
    return [min(x for x, _ in bs), max(y for _, y in bs)]


def behind_ids(ly: dict[str, Any]) -> set[str]:
    """The builder layer ids of the stage's behind parts (= items: the behind group, the behind panels of their own)."""
    grp = grouping(ly)
    out = {f"{ly['id']}.b"} if grp["b"] is not None or (ly.get("background") or {}).get("kind", "none") != "none" else set()
    return out | {f"{ly['id']}.p{k + 1}" for k in grp["solo"] if ly["panels"][k].get("depth") == "behind"}


def matte_part(spec: dict[str, Any], scene_layers: list[dict[str, Any]], total: float,
               existing: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The live scene's `matte` with the stages added: their spans, and their behind clips flagged `behind: true`
    (live-player.js draws the speaker right above the last behind clip, under the front panels and the pills). existing:
    what deck_layer.matte_part made (the background decks) — kept."""
    stages = [ly for ly in ((spec or {}).get("story") or {}).get("layers") or [] if behind_span(ly)]
    if not stages:
        return existing
    ids: set[str] = set()
    spans = [list(s) for s in (existing or {}).get("spans") or []]
    for ly in stages:
        a, b = behind_span(ly)
        a, b = max(0.0, a), min(float(total), b)
        if b > a:
            spans.append([round(a, 3), round(b, 3)])
        ids |= behind_ids(ly)
    for e in scene_layers or []:
        if str(e.get("id") or "") in ids:
            e["behind"] = True
    spans.sort()
    merged: list[list[float]] = []
    for s in spans:
        if merged and s[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], s[1])
        else:
            merged.append(s)
    if existing:
        return {**existing, "spans": merged}
    from app.tools.deck_matte import RECIPE           # the same recipe as the background deck (live = final)
    return {"spans": merged, "model": RECIPE["live_model"], "semantic": RECIPE["model"], "levels": RECIPE["levels"],
            "ema": RECIPE["ema"], "motion": RECIPE["motion"]} if merged else None


def _stage_groups(D: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """[(index of the stage's last behind item, its first item)] in D.layers."""
    out: dict[str, list[int]] = {}
    for i, it in enumerate(D.get("layers") or []):
        st = it.get("stage") or {}
        if st.get("depth") == "behind" and not it.get("speaker"):
            out.setdefault(str(it["id"]).split(".")[0], []).append(i)
    return [(idx[-1], D["layers"][idx[0]]) for sid, idx in out.items()]


async def add_speakers(D: dict[str, Any], work: Path, footage_file: str, media: str,
                       stages: Optional[list[dict[str, Any]]] = None) -> tuple[list[str], list[str]]:
    """The final render's depth: for every stage with behind parts, the speaker matte clip (deck_matte.matte — the
    background deck's, cached by the footage and the frames) and a speaker item right after the stage's last behind
    item (video_builder.jsx speakerLayer). stages: the story's stage layers (their span); without them the behind items'
    own span. → (asset names, findings)."""
    groups = _stage_groups(D)
    if not groups:
        return [], []
    from app.tools import deck_matte
    why = deck_matte.available()
    if why:
        return [], [f"The stage's background / behind panels — {why}: the final video shows them over you there."]
    fps = float(D["fps"])
    total_f = round(float(D["total"]) * fps)
    by_id = {str(s["id"]): s for s in stages or []}
    names, notes = [], []
    for last, first in sorted(groups, key=lambda x: -x[0]):
        sid = str(first["id"]).split(".")[0]
        span = behind_span(by_id[sid]) if sid in by_id else None
        if span is None:
            its = [x for x in D["layers"] if str(x.get("id", "")).split(".")[0] == sid and (x.get("stage") or {}).get("depth") == "behind"]
            span = [min(float(x["at"]) for x in its), max(float(x["at"]) + float(x["dur"]) for x in its)]
        fa = max(0, round(span[0] * fps))
        fb = min(total_f, round(span[1] * fps))
        if fb - fa < 2:
            continue
        try:
            clip, _st = await deck_matte.matte(work / footage_file, media, fa, fb - fa, fps)
        except Exception as e:                                # noqa: BLE001 — the video still renders, the check says so
            log.warning("speaker matte for stage %s failed: %s", sid, e)
            notes.append(f"Layer {sid} (stage): the speaker couldn't be cut out ({str(e)[:120]}) — the final video shows the "
                         "background over you there.")
            continue
        name = f"speaker-{clip.stem[:16]}.mp4"
        if not (work / name).exists():
            try:
                os.link(clip, work / name)
            except OSError:
                shutil.copyfile(clip, work / name)
        D["layers"].insert(last + 1, {"id": f"{sid}.speaker", "name": f"{deck_matte.SPEAKER}{sid}",
                                      "speaker": {"file": name, "at": round(fa / fps, 6), "dur": round((fb - fa) / fps, 6)}})
        names.append(name)
    return names, notes


# ---------- the live preview's clips (app/tools/live_layers.py) ----------

def live_cycle(name: str, fps: float, late: float) -> Optional[tuple[int, int]]:
    """A stage part's cycle in frames, known by construction (None: not a stage part): (intro, period). Every motion is
    an expression on (frame mod period) and every key (the entrance, the fly-in, the pop) is part of the entrance, so
    from the frame after the latest key (late: live_layers' `late`, in frames — the layer's comp and the nested comps,
    the main comp's keys outside the exit) frames [intro, intro + period) loop seamlessly whatever the engine's rounding:
    the live clip is just those frames (no probe). A pill is a still after its pop: period 1."""
    m = ITEM_RE.match(name or "")
    if not m:
        return None
    intro = max(1, int(math.ceil(float(late or 0))) + 2)
    if m.group(2).startswith("t") or name.endswith(STILL + ")"):
        return intro, 1
    return intro, max(2, int(round(PERIOD_S * fps)))
