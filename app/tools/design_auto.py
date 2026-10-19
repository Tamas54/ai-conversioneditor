"""Automata grafikai tervező: cél + szöveg + kép (+ arculat) → 3 VALÓBAN eltérő kompozíció → választás → minden méret.

Egy terv (spec) = archetípus (elrendezés-család) + betűpár + paletta + hangsúlyok. A döntéseket a DeepSeek-V4.1-Flash
hozza JSON-ban (art director), a rajzolás determinisztikus: design.Doc → VectorCraft-parancsok, a szöveg szerkeszthető.
Ugyanaz a terv BÁRMELY méretre újratördelhető (kampánycsomag): az archetípus a képarányhoz igazodik (álló → egymás
alatt, fekvő → egymás mellett), a betűméret a rövidebb oldalból és a szöveg hosszából jön, a kép kivágása az alanyhoz
igazodik (ember: a fej fent marad; tárgy: az ISNet-maszk), nem nyújtunk és nem vágunk középre vakon.

Archetípusok: full_bleed (a fotó kitölt, a szöveg átmenetes takarón) · split (fotó + színes mező) · typographic (az óriás
cím a kép, a fotó kicsi) · framed (keretezett fotó, szerkesztőségi, középre zárt) · band (színsáv viszi a címet a fotón) ·
cutout (a fotó alanya kivágva, egyszínű háttéren, a cím mögötte).

Betűk: a szerveren a fonts-noto (Noto Serif/Sans Display) + fonts-bebas-neue, fonts-lato, fonts-lobster (Dockerfile);
a VectorCraft maga hozza a Source Sans 3 / Source Serif 4 / Inter családot. A böngésző-szerkesztőbe csak ez utóbbiak
vannak befordítva — ott a többi címbetű helyettesítve látszik, amíg a VectorCraft web-buildbe be nem kerülnek.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.tools import crafts
from app.tools.design import Doc

log = logging.getLogger("aice.design_auto")

# ---------- méretek ----------
# kulcs → (szélesség, magasság, felirat, fő kimenet); a közösségi méretek px (= pt), a nyomtatottak pt
SIZES: dict[str, tuple[float, float, str, str]] = {
    "ig_post": (1080, 1080, "Instagram post · 1080×1080", "png"),
    "ig_portrait": (1080, 1350, "Instagram portrait · 1080×1350", "png"),
    "ig_story": (1080, 1920, "Story / Reel · 1080×1920", "png"),
    "youtube": (1280, 720, "YouTube thumbnail · 1280×720", "png"),
    "link": (1200, 630, "Link preview · Facebook, LinkedIn · 1200×630", "png"),
    "x_post": (1600, 900, "X post · 1600×900", "png"),
    "A4": (595.3, 841.9, "A4 poster", "pdf"),
    "A3": (841.9, 1190.6, "A3 poster", "pdf"),
    "A5": (419.5, 595.3, "A5 flyer", "pdf"),
    "letter": (612, 792, "US Letter poster", "pdf"),
}
ALIASES = {"square": "ig_post", "post": "ig_post", "instagram": "ig_post", "portrait": "ig_portrait", "story": "ig_story",
           "reel": "ig_story", "youtube_thumbnail": "youtube", "yt": "youtube", "landscape": "link", "facebook": "link",
           "linkedin": "link", "twitter": "x_post", "x": "x_post", "a4": "A4", "a3": "A3", "a5": "A5", "us_letter": "letter",
           "poster": "A4"}


def size_key(s: Optional[str]) -> str:
    k = str(s or "ig_post").strip()
    k = ALIASES.get(k.lower(), k)
    if k not in SIZES:
        raise ValueError(f"Unknown size: {s} (choose from {', '.join(SIZES)})")
    return k


# ---------- betűk ----------
# család → stílusok és átlagos betűszélesség (em) a sortörés-becsléshez
FAMILIES: dict[str, dict[str, Any]] = {
    "Noto Serif Display": {"head": "Bold", "italic": "Bold Italic", "cw": 0.54, "kind": "serif"},
    "Noto Sans Display": {"head": "Bold", "italic": "Bold Italic", "cw": 0.55, "kind": "sans"},
    "Bebas Neue": {"head": "Regular", "cw": 0.40, "kind": "condensed", "upper": True},
    "Lato": {"head": "Black", "body": "Regular", "strong": "Bold", "italic": "Italic", "cw": 0.57, "kind": "sans"},
    "Lobster Two": {"head": "Bold Italic", "cw": 0.50, "kind": "script"},
    "Source Serif 4": {"head": "Regular", "cw": 0.52, "kind": "serif"},
    "Source Sans 3": {"head": "Bold", "body": "Regular", "strong": "Bold", "italic": "Italic", "cw": 0.50, "kind": "sans"},
    "Inter": {"head": "SemiBold", "body": "Regular", "strong": "SemiBold", "cw": 0.55, "kind": "sans"},
}
BODY_FAMILIES = ("Source Sans 3", "Inter", "Lato")
FONT_PAIRS: dict[str, dict[str, Any]] = {
    "editorial": {"head": "Noto Serif Display", "body": "Source Sans 3", "lead": 1.04, "track": 0},
    "modern": {"head": "Noto Sans Display", "body": "Inter", "lead": 1.0, "track": -10},
    "condensed": {"head": "Bebas Neue", "body": "Lato", "lead": 0.92, "track": 20},
    "heavy": {"head": "Lato", "body": "Lato", "lead": 0.98, "track": -15},
    "elegant": {"head": "Noto Serif Display", "head_style": "Bold Italic", "body": "Source Sans 3", "lead": 1.04, "track": 0},
    "playful": {"head": "Lobster Two", "body": "Lato", "lead": 1.06, "track": 0},
    "classic": {"head": "Source Serif 4", "body": "Source Sans 3", "lead": 1.08, "track": 0},
}
ARCHETYPES = ("full_bleed", "split", "typographic", "framed", "band", "cutout")
DECORATIONS = ("rule", "frame", "blocks", "none")
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def font_family(name: Optional[str], *, body: bool = False) -> Optional[str]:
    """Bármilyen betűnév → a legközelebbi ELÉRHETŐ család (a brand/minta „Montserrat”-ja → Lato stb.)."""
    if not name:
        return None
    n = str(name).strip()
    pool = BODY_FAMILIES if body else tuple(FAMILIES)
    for f in pool:
        if f.lower() == n.lower():
            return f
    low = n.lower()
    table = [
        (("bebas", "oswald", "anton", "league gothic", "impact", "condensed", "narrow", "knockout", "druk", "compressed"), "Bebas Neue", "Lato"),
        (("lobster", "pacifico", "script", "brush", "hand", "dancing", "satisfy", "great vibes", "cursive"), "Lobster Two", "Lato"),
        (("montserrat", "gotham", "poppins", "proxima", "avenir", "futura", "black", "heavy", "lato", "raleway", "nunito", "brandon"), "Lato", "Lato"),
        (("playfair", "didot", "bodoni", "display serif", "dm serif", "abril", "prata", "cormorant", "garamond", "caslon", "baskerville",
          "noto serif", "times", "georgia", "cambria", "caladea", "merriweather", "lora", "serif", "minion", "charter"), "Noto Serif Display", "Source Sans 3"),
        (("inter", "helvetica", "arial", "roboto", "open sans", "segoe", "sf pro", "calibri", "carlito", "verdana", "noto sans", "dejavu",
          "liberation sans", "source sans", "work sans", "ibm plex sans", "manrope", "dm sans", "sans"), "Noto Sans Display", "Inter"),
    ]
    for keys, head, bodyf in table:
        if any(k in low for k in keys):
            return bodyf if body else head
    return "Source Sans 3" if body else "Noto Sans Display"


def _pair(spec: dict[str, Any], brand: Optional[dict[str, Any]]) -> dict[str, Any]:
    p = dict(FONT_PAIRS.get(spec.get("fonts") or "modern", FONT_PAIRS["modern"]))
    bf = (brand or {}).get("fonts") or {}
    if bf.get("heading"):
        p["head"] = font_family(bf["heading"]) or p["head"]
        p.pop("head_style", None)
    if bf.get("body"):
        p["body"] = font_family(bf["body"], body=True) or p["body"]
    fam = FAMILIES[p["head"]]
    p["head_style"] = p.get("head_style") or fam["head"]
    b = FAMILIES.get(p["body"], FAMILIES["Source Sans 3"])
    p["body_style"], p["strong_style"] = b.get("body", "Regular"), b.get("strong", "Bold")
    p["cw"] = fam["cw"]
    p["upper_native"] = bool(fam.get("upper"))
    return p


# ---------- színek ----------
def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex(rgb) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(v)))) for v in rgb)


def _lum(h: str) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(h)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def mix(a: str, b: str, t: float) -> str:
    ra, rb = _rgb(a), _rgb(b)
    return _hex([x + (y - x) * t for x, y in zip(ra, rb)])


def readable(fg: str, bg: str, need: float = 4.5) -> str:
    """fg, ha elég kontrasztos a bg-n; különben a fehér/majdnem-fekete felé tolva, amíg az lesz."""
    if contrast(fg, bg) >= need:
        return fg
    target = "#FFFFFF" if _lum(bg) < 0.4 else "#111418"
    for t in (0.3, 0.5, 0.7, 0.85, 1.0):
        c = mix(fg, target, t)
        if contrast(c, bg) >= need:
            return c
    return target


def _sat(h: str) -> float:
    r, g, b = (v / 255 for v in _rgb(h))
    mx, mn = max(r, g, b), min(r, g, b)
    return 0 if mx == 0 else (mx - mn) / mx


# ---------- fotó: elemzés és okos kivágás ----------
@dataclass
class Photo:
    file_id: str
    path: Path
    w: int
    h: int
    focus: tuple[float, float] = (0.5, 0.45)
    box: Optional[tuple[float, float, float, float]] = None      # alany (x0, y0, x1, y1), 0–1
    people: bool = False
    colors: list[str] = field(default_factory=list)
    cut: Optional[Path] = None                                  # a kivágott alany (PNG, átlátszó), ha kellett

    @property
    def orientation(self) -> str:
        r = self.w / self.h
        return "landscape" if r > 1.15 else "portrait" if r < 0.87 else "square"

    def where(self) -> str:
        fx = self.focus[0]
        return "left" if fx < 0.4 else "right" if fx > 0.6 else "centre"


def _kmeans_colors(im, k: int = 8) -> list[str]:
    """A kép fő színei gyakoriság szerint (numpy k-means, 64×64-es mintán)."""
    import numpy as np
    a = np.asarray(im.convert("RGB").resize((64, 64)), dtype=np.float32).reshape(-1, 3)
    rng = np.random.default_rng(7)
    c = a[rng.choice(len(a), k, replace=False)]
    for _ in range(12):
        lab = ((a[:, None, :] - c[None]) ** 2).sum(-1).argmin(1)
        for i in range(k):
            sel = a[lab == i]
            if len(sel):
                c[i] = sel.mean(0)
    counts = np.bincount(lab, minlength=k)
    return [_hex(c[i]) for i in counts.argsort()[::-1] if counts[i] > 0]


def _analyze_sync(path: Path) -> tuple[int, int, tuple[float, float], Optional[tuple], bool, list[str]]:
    import numpy as np
    from PIL import Image, ImageOps
    from app.tools import cutout
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    W, H = im.size
    small = im.copy()
    small.thumbnail((768, 768))
    colors = _kmeans_colors(small)
    focus, box, people = (0.5, 0.45), None, False
    try:
        mask = None
        hum = cutout._people(small)
        if hum is not None:
            h = np.asarray(hum, dtype=np.float32) / 255
            if (h > 0.5).mean() > 0.02:
                mask, people = h, True
        if mask is None and Path(cutout.settings.CUTOUT_MODEL_PATH).is_file():
            mask = np.asarray(cutout._mask(small), dtype=np.float32) / 255
        if mask is not None and (mask > 0.5).mean() > 0.01:
            ys, xs = np.nonzero(mask > 0.5)
            sw, sh = mask.shape[1], mask.shape[0]
            x0, x1, y0, y1 = xs.min() / sw, (xs.max() + 1) / sw, ys.min() / sh, (ys.max() + 1) / sh
            box = (x0, y0, x1, y1)
            wsum = mask.sum()
            cx = float((mask.sum(0) * np.arange(sw)).sum() / wsum / sw)
            cy = float((mask.sum(1) * np.arange(sh)).sum() / wsum / sh)
            # embernél a fej maradjon: a fókusz a felső harmad felé
            focus = (cx, y0 + (y1 - y0) * 0.3) if people else (cx, cy)
    except Exception as e:                                   # a modell hiánya nem hiba: középre vágunk
        log.info("képelemzés: %s", e)
    return W, H, focus, box, people, colors


async def analyze(file_id: str) -> Photo:
    from app.tools import cutout
    src = crafts.existing(file_id)
    async with cutout._run_lock:                             # a hálók memóriaigénye nagy: egyszerre egy
        W, H, focus, box, people, colors = await asyncio.to_thread(_analyze_sync, src)
    return Photo(file_id=file_id, path=src, w=W, h=H, focus=focus, box=box, people=people, colors=colors)


def crop_box(p: Photo, aspect: float) -> tuple[int, int, int, int]:
    """A legnagyobb `aspect` arányú ablak, a fókuszon, az alanyt lehetőleg egészben megtartva."""
    W, H = p.w, p.h
    cw, ch = (W, W / aspect) if W / H <= aspect else (H * aspect, H)
    cx, cy = p.focus[0] * W, p.focus[1] * H
    x = min(max(cx - cw / 2, 0), W - cw)
    y = min(max(cy - ch / 2, 0), H - ch)
    if p.box:                                                # ha belefér, az egész alany kerüljön bele
        bx0, by0, bx1, by1 = p.box[0] * W, p.box[1] * H, p.box[2] * W, p.box[3] * H
        if bx1 - bx0 <= cw:
            x = min(max(x, bx1 - cw), bx0)
        if by1 - by0 <= ch:
            y = min(max(y, by1 - ch), by0)
        elif p.people:                                       # magas alany: a fej fent maradjon
            y = by0 - ch * 0.04
        x, y = min(max(x, 0), W - cw), min(max(y, 0), H - ch)
    return int(x), int(y), int(x + cw), int(y + ch)


def subject_pos(p: Photo, aspect: float) -> tuple[float, float]:
    """Az alany (fókusz) helye a keret arányára vágott képen belül, 0–1."""
    x0, y0, x1, y1 = crop_box(p, aspect)
    return ((p.focus[0] * p.w - x0) / max(1, x1 - x0), (p.focus[1] * p.h - y0) / max(1, y1 - y0))


def crop_file(p: Photo, aspect: float, tmp: list[Path], max_px: int = 2400) -> Path:
    from PIL import Image, ImageOps
    from app.storage import temp_path
    from app.storage.files import new_file_id
    im = ImageOps.exif_transpose(Image.open(p.path)).convert("RGB").crop(crop_box(p, aspect))
    im.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    out = temp_path(new_file_id("jpg"))
    im.save(out, "JPEG", quality=90)
    tmp.append(out)
    return out


async def cutout_of(p: Photo, tmp: list[Path]) -> Optional[Path]:
    """A fotó alanya átlátszó háttéren, a látható részre vágva (cutout archetípus) — egyszer, a többi méret is ezt kapja."""
    if p.cut and p.cut.exists():
        return p.cut
    from app.storage import temp_path
    from app.storage.files import new_file_id
    from app.tools import cutout
    if not cutout.available():
        return None
    raw = temp_path(new_file_id("png"))
    await cutout.remove_background(p.path, raw)

    def trim():
        from PIL import Image
        im = Image.open(raw)
        bb = im.getchannel("A").point(lambda v: 255 if v > 24 else 0).getbbox()
        if bb:
            im = im.crop(bb)
        im.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
        im.save(raw, "PNG")
    await asyncio.to_thread(trim)
    tmp.append(raw)
    p.cut = raw
    return raw


# ---------- a terv (art director) ----------
_PLAN_SYSTEM = """You are a senior art director. Plan THREE clearly different designs for the same content.
Reply with ONE json object: {"variants": [ {...}, {...}, {...} ]}. Each variant:
{"name": "2-3 words naming the look", "archetype": "full_bleed|split|typographic|framed|band|cutout",
 "fonts": "editorial|modern|condensed|heavy|elegant|playful|classic",
 "palette": {"bg": "#hex", "ink": "#hex (main text on bg)", "accent": "#hex (highlights, meta, rules)"},
 "title_case": "upper|as_is", "title_scale": 0.75-1.3, "align": "left|center", "hero": "title|date",
 "decoration": "rule|frame|blocks|none", "density": "airy|balanced", "why": "one sentence: the idea"}
Archetypes: full_bleed = the photo fills the canvas, text on a soft dark (or light) fade; split = photo on one side
(top on tall formats) and the text on a solid colour field; typographic = a giant headline IS the picture, the photo
small or absent, lots of colour and space; framed = the photo inset with generous margins, editorial and centred;
band = a bold colour band carries the headline across the photo; cutout = the photo's person/object cut out on a
solid colour with the headline big behind it (only for a clear person or product photo).
Fonts: editorial = display serif; modern = clean display sans; condensed = tall all-caps poster type (concerts,
sport, sales); heavy = black geometric sans; elegant = display serif italic; playful = script (parties, kids, food);
classic = book serif.
Rules: the three variants use three DIFFERENT archetypes, three DIFFERENT font choices and clearly different palettes
(e.g. one dark, one light, one bold colour). Colours harmonise with the photo's colours. Text must stay readable:
ink vs bg contrast strong. Match the mood to the goal (a rock concert is not a wedding). With a brand: use ONLY the
brand colours (roles may change) — the fonts are fixed by the brand — but still vary the composition.
No photo: choose from typographic, band, framed, split (split then shows a colour field instead of a photo).
"name" and "why" in the language of the headline (English when unsure)."""


def _fallback(photo: Optional[Photo], brand: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """AI nélkül (vendég, modellhiba): három kézzel összerakott, egymástól eltérő terv a fotó színeiből."""
    cols = (photo.colors if photo else [])[:8] or ["#1D3557", "#E63946", "#F1FAEE"]
    vivid = max(cols, key=lambda c: _sat(c) * (0.4 + min(_lum(c), 0.6)))
    if _sat(vivid) < 0.25:                              # szürke fotó: egy meleg kiemelőszín
        vivid = "#E76F51"
    dark = min(cols, key=_lum)
    if brand and (brand.get("colors") or {}).get("primary"):
        bc = brand["colors"]
        dark, vivid = bc.get("primary") or dark, bc.get("accent") or vivid
    deep = mix(dark, "#000000", 0.55) if _lum(dark) > 0.03 else dark
    band = vivid
    for t in (0.0, 0.2, 0.35, 0.5, 0.65):              # a sávon fehér szöveg olvasható legyen
        band = mix(vivid, "#000000", t)
        if contrast("#FFFFFF", band) >= 4.5:
            break
    light_acc = mix(vivid, "#FFFFFF", 0.55)
    if photo:
        return [
            {"name": "Night poster", "archetype": "full_bleed", "fonts": "condensed", "title_case": "upper", "align": "left",
             "palette": {"bg": deep, "ink": "#FFFFFF", "accent": readable(light_acc, deep, 4.5)}, "decoration": "rule", "title_scale": 1.1,
             "why": "The photo carries the mood; a tall headline over a soft dark fade."},
            {"name": "Editorial", "archetype": "split", "fonts": "editorial", "title_case": "as_is", "align": "left",
             "palette": {"bg": "#F7F4EE", "ink": "#1B1B1F", "accent": readable(vivid, "#F7F4EE", 3)}, "decoration": "none",
             "why": "Calm magazine layout: photo and words side by side."},
            {"name": "Colour block", "archetype": "band", "fonts": "heavy", "title_case": "as_is", "align": "left",
             "palette": {"bg": band, "ink": "#FFFFFF", "accent": readable(light_acc, band, 3)}, "decoration": "blocks",
             "title_scale": 1.0, "why": "A bold colour band makes the headline jump out of the photo."},
        ]
    return [
        {"name": "Big type", "archetype": "typographic", "fonts": "heavy", "title_case": "as_is", "align": "left",
         "palette": {"bg": band, "ink": "#FFFFFF", "accent": readable(light_acc, band, 3)}, "decoration": "blocks",
         "title_scale": 1.15, "why": "The headline is the picture."},
        {"name": "Quiet frame", "archetype": "framed", "fonts": "editorial", "title_case": "as_is", "align": "center",
         "palette": {"bg": "#F7F4EE", "ink": "#1B1B1F", "accent": readable(vivid, "#F7F4EE", 3)}, "decoration": "frame",
         "why": "Centred and calm, like a printed invitation."},
        {"name": "Night", "archetype": "band", "fonts": "condensed", "title_case": "upper", "align": "left",
         "palette": {"bg": deep, "ink": "#FFFFFF", "accent": readable(vivid, deep, 3)}, "decoration": "rule",
         "why": "Dark ground, tall poster type, one bright accent."},
    ]


def normalize(spec: dict[str, Any], photo: Optional[Photo], used: Optional[dict[str, set]] = None) -> dict[str, Any]:
    """A modell tervét érvényes, olvasható tervvé igazítja (ismeretlen archetípus/betű/szín, kontraszt, ismétlés)."""
    used = used if used is not None else {"archetype": set(), "fonts": set()}
    s = dict(spec or {})
    arch = s.get("archetype") if s.get("archetype") in ARCHETYPES else None
    if arch == "cutout" and not photo:
        arch = None
    if arch in (None, *used["archetype"]):
        pool = [a for a in (("full_bleed", "split", "band", "typographic", "framed") if photo else ("typographic", "band", "framed", "split"))
                if a not in used["archetype"]]
        arch = pool[0] if pool else (arch or "typographic")
    fonts = s.get("fonts") if s.get("fonts") in FONT_PAIRS else None
    if fonts in (None, *used["fonts"]):
        pool = [f for f in ("modern", "editorial", "condensed", "heavy", "classic", "elegant") if f not in used["fonts"]]
        fonts = pool[0] if pool else "modern"
    used["archetype"].add(arch)
    used["fonts"].add(fonts)
    pal = s.get("palette") if isinstance(s.get("palette"), dict) else {}
    bg = pal.get("bg") if _HEX.match(str(pal.get("bg", ""))) else "#14213D"
    ink = pal.get("ink") if _HEX.match(str(pal.get("ink", ""))) else ("#FFFFFF" if _lum(bg) < 0.4 else "#14161A")
    acc = pal.get("accent") if _HEX.match(str(pal.get("accent", ""))) else "#FCA311"
    s.update(archetype=arch, fonts=fonts,
             palette={"bg": bg.upper(), "ink": readable(ink, bg).upper(), "accent": readable(acc, bg, 4.5).upper()},
             title_case="upper" if s.get("title_case") == "upper" else "as_is",
             title_scale=max(0.7, min(1.35, float(s.get("title_scale") or 1.0))) if str(s.get("title_scale") or "").replace(".", "", 1).isdigit() else 1.0,
             align="center" if s.get("align") == "center" else "left",
             hero="date" if s.get("hero") == "date" else "title",
             decoration=s.get("decoration") if s.get("decoration") in DECORATIONS else "rule",
             density="airy" if s.get("density") == "airy" else "balanced",
             name=str(s.get("name") or arch.replace("_", " ").title())[:40],
             why=str(s.get("why") or "")[:200])
    return s


async def plan(content: dict[str, Any], photo: Optional[Photo], brand: Optional[dict[str, Any]] = None, *,
               n: int = 3, ai: bool = True) -> list[dict[str, Any]]:
    """n terv; AI-val a DeepSeek dönt (JSON), hiba vagy ai=False esetén a kézi tartalék."""
    specs: list[dict[str, Any]] = []
    if ai:
        brief = {
            "goal": content.get("goal") or "", "texts": {k: v for k, v in content.items() if k != "goal" and v},
            "photo": ({"orientation": photo.orientation, "has_people": photo.people, "subject_position": photo.where(),
                       "main_colours": photo.colors[:6]} if photo else None),
            "brand": ({"colours": brand.get("colors"), "fonts": brand.get("fonts"), "rules": brand.get("rules")} if brand else None),
        }
        try:
            from app.routes.make import _llm_json
            out = await _llm_json(_PLAN_SYSTEM, "Plan the designs for this, answer in json:\n" + json.dumps(brief, ensure_ascii=False))
            specs = [v for v in (out.get("variants") or []) if isinstance(v, dict)]
        except Exception as e:
            log.warning("dizájnterv (AI) nem sikerült: %s — kézi tartalék", e)
    fb = _fallback(photo, brand)
    while len(specs) < n:
        specs.append(fb[len(specs) % len(fb)])
    used: dict[str, set] = {"archetype": set(), "fonts": set()}
    out = [normalize(s, photo, used) for s in specs[:n]]
    if brand:                                          # arculat: csak a saját színei (a szerepek cserélődhetnek)
        bc = brand.get("colors") or {}
        allowed = [c.upper() for c in (bc.get("primary"), bc.get("accent"), bc.get("bg"), bc.get("text"), *(bc.get("extra") or []))
                   if c and _HEX.match(str(c))]
        if allowed:
            for s in out:
                p = s["palette"]
                for k in ("bg", "accent"):
                    if p[k] not in allowed:
                        p[k] = min(allowed, key=lambda c: sum((a - b) ** 2 for a, b in zip(_rgb(c), _rgb(p[k]))))
                p["ink"] = readable(p["ink"], p["bg"])
                p["accent"] = readable(p["accent"], p["bg"], 4.5)
    return out


# ---------- tördelés ----------
_FONT_FILES: dict[tuple[str, str], Optional[str]] = {}


def _font_file(family: str, style: str) -> Optional[str]:
    """A rendszerre telepített betűfájl (fontconfig) — a VectorCraft beépített betűinél (Source Sans 3, Inter) nincs."""
    key = (family, style)
    if key not in _FONT_FILES:
        import subprocess
        try:
            out = subprocess.run(["fc-match", "-f", "%{family}|%{file}", f"{family}:style={style}"], capture_output=True,
                                 text=True, timeout=5).stdout
            fam, path = out.split("|", 1)
            _FONT_FILES[key] = path if family.lower() in fam.lower() else None
        except Exception:
            _FONT_FILES[key] = None
    return _FONT_FILES[key]


def _measurer(family: Optional[str], style: Optional[str], cw: float):
    """width(szöveg, méret) → pont: valódi mérés a betűfájlból, ha van; különben átlagos betűszélesség-becslés."""
    path = _font_file(family, style or "Regular") if family else None
    if path:
        from functools import lru_cache
        from PIL import ImageFont

        @lru_cache(maxsize=64)
        def font(sz: int):
            return ImageFont.truetype(path, sz)

        def width(text: str, size: float) -> float:
            f = font(100)
            return f.getlength(text) * size / 100
        return width
    return lambda text, size: len(text) * cw * size


def _wrap(text: str, size: float, w: float, width) -> int:
    """Hány sorba tördelődik (szavanként, mohón) — a túl hosszú szó a saját sorában (a motor tördeli)."""
    lines, cur = 0, ""
    for para in (text or " ").split("\n"):
        lines += 1
        cur = ""
        for wd in para.split():
            t = f"{cur} {wd}".strip()
            if not cur or width(t, size) <= w:
                cur = t
            else:
                lines += 1
                cur = wd
            while width(cur, size) > w * 1.02 and len(cur) > 1:   # egy szó sem fér el: törik
                lines += 1
                cur = cur[max(1, int(len(cur) * w / width(cur, size))):]
    return max(1, lines)


def fit_size(text: str, w: float, h: float, cw: float, lead: float, size_max: float, size_min: float = 7,
             family: Optional[str] = None, style: Optional[str] = None) -> tuple[float, float]:
    """A legnagyobb betűméret, amivel a szöveg (szavanként tördelve) kifér w×h-ba → (méret, a blokk magassága).
    Ha a betűfájl a rendszeren van, valódi méréssel tördel (különben átlagos betűszélesség-becsléssel)."""
    width = _measurer(family, style, cw)
    longest = max((text or " ").split(), key=lambda wd: width(wd, 100), default=" ")
    s = size_max
    while s > size_min:
        n = _wrap(text, s, w * 0.97, width)
        if n * s * lead <= h and width(longest, s) <= w * 0.96:     # szó közben nem törünk
            return s, n * s * lead
        s *= 0.95
    return size_min, min(h, _wrap(text, size_min, w * 0.97, width) * size_min * lead)


def block_h(items) -> float:
    return sum(i.h + i.gap for i in items) - (items[-1].gap if items else 0)


@dataclass
class _Item:
    text: str
    size: float
    h: float
    font: str
    style: str
    color: str
    gap: float = 0
    tracking: Optional[int] = None
    lead: float = 1.15
    kind: str = "text"
    min_size: float = 0


def fit_block(items: list[_Item], limit: float) -> list[_Item]:
    """Ha a tömb nem fér el: előbb a törzsszöveg és a handle marad ki, aztán a másodlagos elemek kisebbednek
    (a saját legkisebb méretükig), a cím csak legvégül — a hierarchia megmarad."""
    for kind in ("body", "handle"):
        if block_h(items) <= limit:
            break
        items = [i for i in items if i.kind != kind]
    for _ in range(60):
        if block_h(items) <= limit:
            break
        shrunk = False
        for it in items:
            if it.kind != "title" and it.size * 0.93 >= it.min_size:
                it.size, it.h, it.gap = it.size * 0.93, it.h * 0.93, it.gap * 0.93
                shrunk = True
        if not shrunk:
            for it in items:
                it.size, it.h, it.gap = it.size * 0.93, it.h * 0.93, it.gap * 0.93
    return items


class _Ctx:
    def __init__(self, d: Doc, spec: dict[str, Any], content: dict[str, Any], brand: Optional[dict[str, Any]]):
        self.d, self.spec, self.c, self.brand = d, spec, content, brand
        self.W, self.H = d.w, d.h
        self.base = min(self.W, self.H)
        self.a = self.W / self.H
        self.m = self.base * (0.085 if spec.get("density") == "airy" else 0.068)
        self.p = _pair(spec, brand)
        self.pal = spec["palette"]

    @property
    def tall(self) -> bool:
        return self.a < 0.87

    @property
    def wide(self) -> bool:
        return self.a > 1.15

    def title_text(self) -> str:
        t = (self.c.get("title") or " ").strip()
        return t.upper() if (self.spec.get("title_case") == "upper" or self.p["upper_native"]) else t

    def items(self, w: float, h_title: float, ink: str, accent: str, *, muted: Optional[str] = None, body: bool = True,
              scale: float = 1.0) -> list[_Item]:
        """A szövegtömb elemei felülről lefelé: (felső címke) · cím · alcím · (szöveg) · dátum/hely · cta · handle."""
        c, p, b = self.c, self.p, self.base * scale
        muted = muted or mix(ink, self.pal["bg"], 0.35)
        cw = p["cw"] + (0.1 if (self.spec.get("title_case") == "upper" and not p["upper_native"]) else 0)
        out: list[_Item] = []
        if c.get("kicker"):
            ks = b * 0.027
            out.append(_Item(c["kicker"].upper(), ks, ks * 1.4, p["body"], p["strong_style"], accent, gap=b * 0.018, tracking=140,
                             kind="kicker", min_size=b * 0.02))
        tmax = b * (0.2 if p["upper_native"] else 0.15) * float(self.spec.get("title_scale") or 1)
        ts, th = fit_size(self.title_text(), w, h_title, cw, p["lead"], tmax, b * 0.05, p["head"], p["head_style"])
        out.append(_Item(self.title_text(), ts, th + ts * 0.15, p["head"], p["head_style"], ink, gap=b * 0.03,
                         tracking=p.get("track"), lead=p["lead"], kind="title", min_size=b * 0.05))
        if c.get("subtitle"):
            ss, sh = fit_size(c["subtitle"], w, b * 0.14, 0.5, 1.25, b * 0.046, b * 0.026, p["body"], p["body_style"])
            out.append(_Item(c["subtitle"], ss, sh + ss * 0.3, p["body"], p["body_style"], ink, gap=b * 0.025, lead=1.25,
                             kind="subtitle", min_size=b * 0.026))
        if body and c.get("body"):
            bs, bh = fit_size(c["body"], w, b * 0.2, 0.5, 1.35, b * 0.03, b * 0.02, p["body"], p["body_style"])
            out.append(_Item(c["body"], bs, bh + bs * 0.3, p["body"], p["body_style"], muted, gap=b * 0.025, lead=1.35,
                             kind="body", min_size=b * 0.02))
        hero = self.spec.get("hero") == "date" and bool(c.get("date"))
        if hero:
            ds, dh = fit_size(c["date"], w, b * 0.16, 0.55, 1.05, b * 0.085, b * 0.04, p["head"], p["head_style"])
            out.append(_Item(c["date"], ds, dh + ds * 0.2, p["head"], p["head_style"], accent, gap=b * 0.012, lead=1.05,
                             kind="meta", min_size=b * 0.035))
        meta = " · ".join(x for x in ((None if hero else c.get("date")), c.get("place")) if x)
        if meta:
            ms, mh = fit_size(meta.upper(), w, b * 0.08, 0.6, 1.2, b * 0.036, b * 0.022, p["body"], p["strong_style"])
            out.append(_Item(meta.upper(), ms, mh + ms * 0.3, p["body"], p["strong_style"], ink if hero else accent,
                             gap=b * 0.02, tracking=40, kind="meta", min_size=b * 0.024))
        if c.get("cta"):
            cs = b * 0.034
            out.append(_Item(c["cta"], cs, cs * 1.5, p["body"], p["strong_style"], ink, gap=b * 0.012, kind="cta", min_size=b * 0.024))
        if c.get("handle"):
            hs = b * 0.026
            out.append(_Item(c["handle"], hs, hs * 1.5, p["body"], p["body_style"], muted, kind="handle", min_size=b * 0.02))
        return out

    def draw(self, items: list[_Item], x: float, w: float, *, top: Optional[float] = None, bottom: Optional[float] = None,
             align: Optional[str] = None, limit: Optional[float] = None) -> tuple[float, float]:
        """A tömb kirajzolása felülről (top) vagy alulra zárva (bottom) → (a tömb teteje, alja).
        limit: a tömb legnagyobb magassága — ha nem fér el, minden elem arányosan kisebb (nem lóg ki a lapról)."""
        if limit:
            items[:] = fit_block(items, limit)
        total = block_h(items)
        y = top if top is not None else (bottom or self.H) - total
        y0 = y
        al = align or self.spec.get("align", "left")
        for it in items:
            self.d.text(x, y, it.text, size=round(it.size, 1), font=it.font, style=it.style, color=it.color, w=w, h=it.h,
                        fit="shrinkText", align="center" if al == "center" else None, leading=round(it.size * it.lead, 1),
                        tracking=it.tracking)
            y += it.h + it.gap
        return y0, y0 + total

    def rule(self, x: float, y: float, color: str, *, center_w: Optional[float] = None):
        bw, bh = self.base * 0.1, max(3, self.base * 0.008)
        if center_w is not None:
            x = x + (center_w - bw) / 2
        self.d.rect(x, y, bw, bh, color)

    def logo(self, dark_bg: bool, corner: str = "tr", under: Optional[str] = None):
        lp = (self.brand or {}).get("_logo_path")
        if not lp:
            return
        try:
            from PIL import Image
            with Image.open(lp) as im:
                lw, lh = im.size
        except Exception:
            return
        w = self.base * 0.16
        h = w * lh / max(lw, 1)
        if h > self.base * 0.09:
            h = self.base * 0.09
            w = h * lw / max(lh, 1)
        x = self.W - self.m - w if corner.endswith("r") else self.m
        y = self.m * 0.8 if corner.startswith("t") else self.H - self.m * 0.8 - h
        under = under or ("#111418" if dark_bg else "#F7F7F5")
        place_logo(self.d, lp, x, y, w, h, under)


def logo_tone(path: str) -> Optional[str]:
    """A logó látható (nem átlátszó) pixeleinek átlagszíne — ebből dől el, kell-e lap mögé."""
    try:
        import numpy as np
        from PIL import Image
        with Image.open(path) as im:
            a = np.asarray(im.convert("RGBA").resize((96, 96)), dtype=np.float32)
        m = a[..., 3] > 128
        return _hex(a[..., :3][m].mean(0)) if m.any() else None
    except Exception:
        return None


def place_logo(d: Doc, path: str, x: float, y: float, w: float, h: float, under: Optional[str]) -> None:
    """Logó a lapra; ha a színe beleolvadna az alatta lévőbe (kontraszt < 2,2), világos/sötét lapocska mögé."""
    tone = logo_tone(path)
    if under and tone and contrast(tone, under) < 2.2:
        pad = max(4.0, h * 0.18)
        plate = "#FFFFFF" if _lum(tone) < 0.4 else "#111418"
        d._add("shape.rectangle", {"x": d.ox + x - pad, "y": y - pad, "width": w + 2 * pad, "height": h + 2 * pad,
                                   "radius": pad * 0.8}, creates=True)
        d._add("paint.setFill", {"color": plate})
        d._add("paint.setStroke", {"none": True})
    d.image(path, x, y, w, h)


def _scrim(d: Doc, x, y, w, h, color: str, angle: float, max_op: float = 0.9):
    """Átmenetes takaró a fotón (a szöveg alá): átlátszóból `color`-ba. A szög y-tengelye FELFELÉ mutat (mérve):
    -90 = fent átlátszó, lent sötét; 180 = jobbra átlátszó, balra sötét."""
    d._add("shape.rectangle", {"x": d.ox + x, "y": y, "width": w, "height": h}, creates=True)
    d._add("paint.setFill", {"gradient": {"kind": "linear", "angle": angle, "stops": [
        {"offset": 0, "color": color, "opacity": 0}, {"offset": 0.3, "color": color, "opacity": max_op * 0.62},
        {"offset": 0.6, "color": color, "opacity": max_op * 0.9}, {"offset": 1, "color": color, "opacity": max_op}]}})
    d._add("paint.setStroke", {"none": True})


def compose(d: Doc, spec: dict[str, Any], content: dict[str, Any], img: Optional[Path], *,
            cut: Optional[Path] = None, brand: Optional[dict[str, Any]] = None, crop=None, subject=None) -> None:
    """Egy terv kirajzolása a d méretére. crop(aspect) → Path: a fotó okos kivágása a keret arányára;
    subject(aspect) → (x, y): hol van az alany a kivágott képen (a szöveg ne takarja)."""
    x = _Ctx(d, spec, content, brand)
    W, H, m, pal, base = x.W, x.H, x.m, x.pal, x.base
    arch = spec["archetype"]
    if arch in ("full_bleed", "band", "cutout", "split", "framed") and not img and arch != "split":
        arch = "typographic" if arch != "framed" else "framed"
    deco = spec.get("decoration", "rule")
    center = spec.get("align") == "center"

    if arch == "full_bleed":
        d.rect(0, 0, W, H, pal["bg"])
        d.image(crop(W / H), 0, 0, W, H)
        dark = _lum(pal["bg"]) < 0.4
        shade = "#000000" if dark else "#FFFFFF"
        ink = "#FFFFFF" if dark else readable(pal["ink"], "#FFFFFF")
        acc = readable(pal["accent"], "#000000" if dark else "#FFFFFF", 4.5)     # kis betű a takarón: 4.5
        right = bool(x.wide and subject and subject(W / H)[0] < 0.5)
        if x.wide:                                            # fekvő: a szöveg az alannyal ellentétes oldalra
            tw = W * 0.52
            _scrim(d, W * 0.3 if right else 0, 0, W * 0.7, H, shade, 0 if right else 180)
            its = x.items(tw - m, H * 0.42, ink, acc, body=False)
            y0, _ = x.draw(its, W - tw if right else m, tw - m, bottom=H - m, align="left", limit=H - 2.2 * m)
        else:
            _scrim(d, 0, H * 0.3, W, H * 0.7, shade, -90)
            its = x.items(W - 2 * m, H * (0.3 if x.tall else 0.26), ink, acc, body=x.tall)
            y0, _ = x.draw(its, m, W - 2 * m, bottom=H - m * 1.1, limit=H * (0.42 if x.tall else 0.46))
        if deco in ("rule", "blocks"):
            x.rule((W - tw) if right else m, y0 - base * 0.035, acc, center_w=(W - 2 * m) if center and not x.wide else None)
        if deco == "frame":
            _frame(d, m * 0.5, W, H, ink)
        x.logo(dark, "tl" if right else "tr")

    elif arch == "split":
        field_c = pal["bg"]
        ink, acc = pal["ink"], pal["accent"]
        if x.wide:
            iw = W * 0.5
            d.rect(0, 0, W, H, field_c)
            if img:
                d.image(crop(iw / H), W - iw, 0, iw, H)
            else:
                d.rect(W - iw, 0, iw, H, acc)
            tw = W - iw - 2 * m
            its = x.items(tw, H * 0.4, ink, acc, body=True, scale=1.05)
            its = fit_block(its, H - 2 * m)
            total = block_h(its)
            y0, _ = x.draw(its, m, tw, top=max(m, (H - total) / 2), limit=H - 2 * m)
            if deco != "none":
                x.rule(m, y0 - base * 0.04, acc)
            x.logo(_lum(field_c) < 0.4, "bl")
        else:
            ih = H * (0.5 if x.tall else 0.52)
            d.rect(0, 0, W, H, field_c)
            if img:
                d.image(crop(W / ih), 0, 0, W, ih)
            else:
                d.rect(0, 0, W, ih, acc)
            if deco == "blocks":
                d.rect(m, ih - base * 0.012, W * 0.28, base * 0.024, acc)
            its = x.items(W - 2 * m, (H - ih) * 0.42, ink, acc, body=x.tall)
            y0, y1 = x.draw(its, m, W - 2 * m, top=ih + m * 0.9, limit=H - ih - m * 1.6)
            if deco == "rule":
                x.rule(m, ih + m * 0.45, acc, center_w=(W - 2 * m) if center else None)
            x.logo(_lum(field_c) < 0.4, "br")

    elif arch == "typographic":
        bg, ink, acc = pal["bg"], pal["ink"], pal["accent"]
        d.rect(0, 0, W, H, bg)
        blocks = deco == "blocks"
        blk = acc                                             # a színfolt TELJESEN alátámasztja a címtömböt (fél-fél háttéren
        x.spec = {**spec, "title_scale": float(spec.get("title_scale") or 1) * (1.45 if not x.p["upper_native"] else 1.3)}
        under = blk if blocks else bg                         # egyik szín sem lenne olvasható mindkettőn)
        t_ink, t_acc = readable(ink, under), (readable(acc, bg, 4.5) if not blocks else readable(bg, blk, 3))
        f_ink, f_acc = readable(ink, bg), readable(acc, bg, 4.5)

        def head_block(items, x0, top, limit, width):
            """A felső tömb (címke, cím, alcím) — „blocks” díszítésnél előbb a színfolt, rá a szöveg."""
            items = fit_block(items, limit)
            if blocks:
                hgt = block_h(items)
                d.rect(0, 0, x0 + width + m * 0.9, top + hgt + m * 0.9, blk)
            x.draw(items, x0, width, top=top)

        if x.wide:
            tw = W * 0.56 - 2 * m
            its = x.items(tw, H * 0.56, t_ink, t_acc, body=False)
            head = [i for i in its if i.kind in ("kicker", "title", "subtitle")]
            foot = [i for i in its if i.kind not in ("kicker", "title", "subtitle")]
            for i in foot:
                i.color = f_acc if i.kind == "meta" else f_ink
            head_block(head, m, m * 1.2, H * 0.62, tw)
            if foot:
                x.draw(foot, m, tw, bottom=H - m, limit=H * 0.26)
            if img:
                s_ = H - 2 * m
                d.image(crop(0.8), W - m - s_ * 0.8, m, s_ * 0.8, s_)
        else:
            tw = (W * 0.84 if blocks else W) - 2 * m
            ph = (H * (0.3 if x.tall else 0.36)) if img else 0
            pw = min(W * 0.5, ph * 1.25) if img else 0
            its = x.items(tw, H * ((0.36 if x.tall else 0.3) if img else 0.46), t_ink, t_acc, body=x.tall)
            head = [i for i in its if i.kind in ("kicker", "title", "subtitle")]
            foot = [i for i in its if i.kind not in ("kicker", "title", "subtitle")]
            for i in foot:                                    # a részletek a háttéren: ahhoz igazított színnel
                i.color = f_acc if i.kind == "meta" else (mix(f_ink, bg, 0.35) if i.kind == "handle" else f_ink)
            foot = fit_block(foot, ph or H * 0.26)
            room = (ph + m * 3.2) if img else (block_h(foot) + m * 3.4)
            head_block(head, m, m * 1.3, H - room, tw)
            if img:
                d.image(crop(pw / ph), W - m - pw, H - m - ph, pw, ph)
            if foot:                                          # a részletek alul (fotó mellett, ha van)
                x.draw(foot, m, (W - 3 * m - pw) if img else (W - 2 * m), bottom=H - m, limit=ph or H * 0.26)
        if deco == "frame":
            _frame(d, m * 0.45, W, H, acc)
        x.logo(_lum(bg) < 0.4, "tr", under=blk if (blocks and not x.wide) else bg)

    elif arch == "framed":
        bg, ink, acc = pal["bg"], pal["ink"], pal["accent"]
        d.rect(0, 0, W, H, bg)
        fm = m * 1.25
        if x.wide and img:
            iw, ih = W * 0.5, H - 2 * fm
            d.image(crop(iw / ih), fm, fm, iw, ih)
            tx, tw = fm + iw + m, W - (fm + iw + m) - fm
            its = x.items(tw, H * 0.45, ink, acc, body=True)
            its = fit_block(its, H - 2 * fm)
            total = block_h(its)
            x.draw(its, tx, tw, top=max(fm, (H - total) / 2), align="center" if center else "left", limit=H - 2 * fm)
        elif not img:                                         # kép nélkül: a szöveg középen, kettős keretben
            its = x.items(W - 2 * fm - m, H * 0.34, ink, acc, body=True)
            its = fit_block(its, H - 3 * fm)
            total = block_h(its)
            x.draw(its, fm + m * 0.5, W - 2 * fm - m, top=(H - total) / 2, align="center")
            deco = "frame"
            _frame(d, fm * 0.75, W, H, acc)
        else:
            ih = H * (0.5 if x.tall else 0.47)
            iw = W - 2 * fm
            d.image(crop(iw / ih), fm, fm, iw, ih)
            its = x.items(W - 2 * fm, (H - ih - 2 * fm) * 0.5, ink, acc, body=x.tall)
            y0, y1 = x.draw(its, fm, W - 2 * fm, top=fm + ih + m * 0.9, align="center", limit=H - ih - 2 * fm - m * 0.9)
            if deco in ("rule", "frame"):
                d.line(W / 2 - base * 0.06, fm + ih + m * 0.45, W / 2 + base * 0.06, fm + ih + m * 0.45, acc, max(1.5, base * 0.003))
        if deco == "frame":
            _frame(d, fm * 0.45, W, H, acc)
        x.logo(_lum(bg) < 0.4, "br" if not x.wide else "tr")

    elif arch == "band":
        d.rect(0, 0, W, H, pal["bg"])
        d.image(crop(W / H), 0, 0, W, H)
        band_c, ink, acc = pal["bg"], pal["ink"], pal["accent"]
        if x.wide:
            bw = W * 0.46
            d.rect(0, 0, bw, H, band_c, opacity=94)
            its = x.items(bw - 2 * m, H * 0.46, ink, acc, body=False)
            its = fit_block(its, H - 2.4 * m)
            total = block_h(its)
            y0, _ = x.draw(its, m, bw - 2 * m, top=max(m * 1.2, (H - total) / 2), limit=H - 2.4 * m)
        else:
            its = x.items(W - 2 * m, H * (0.2 if x.tall else 0.22), ink, acc, body=False)
            maxband = H * (0.46 if x.tall else 0.5)
            its = fit_block(its, maxband - 2 * m)
            total = block_h(its)
            bh = total + 2 * m
            by = H - bh - (H * 0.08 if x.tall else 0)
            d.rect(0, by, W, bh, band_c, opacity=94)
            y0, _ = x.draw(its, m, W - 2 * m, top=by + m, limit=maxband - 2 * m)
        if deco in ("blocks", "rule"):
            bw_, bh_ = base * (0.16 if deco == "blocks" else 0.1), max(4, base * (0.022 if deco == "blocks" else 0.008))
            d.rect(m, y0 - base * 0.03 - bh_, bw_, bh_, acc)
        x.logo(_lum(band_c) < 0.4, "tr")

    elif arch == "cutout":
        bg, ink, acc = pal["bg"], pal["ink"], pal["accent"]
        d.rect(0, 0, W, H, bg)
        t = x.title_text()
        # óriás cím a háttérben (az alany mögött), a többi szöveg elöl
        big_w = W - 2 * m
        bs, bh = fit_size(t, big_w, H * (0.42 if x.tall else 0.6), x.p["cw"], x.p["lead"], base * 0.34, base * 0.08,
                          x.p["head"], x.p["head_style"])
        d.text(m, m * 1.4, t, size=round(bs, 1), font=x.p["head"], style=x.p["head_style"], color=mix(acc, bg, 0.15),
               w=big_w, h=bh + bs * 0.2, fit="shrinkText", leading=round(bs * x.p["lead"], 1),
               align="center" if center else None, tracking=x.p.get("track"))
        rest = dict(content)
        rest["title"] = ""
        x.c = rest
        info_w = W * (0.45 if x.wide else 1) - 2 * m
        its = [i for i in x.items(info_w, H * 0.1, readable(ink, bg), readable(acc, bg, 4.5), body=False) if i.text.strip()]
        its = fit_block(its, H * 0.26)
        band = (block_h(its) + 1.6 * m) if (its and not x.wide) else 0
        if cut:
            from PIL import Image
            with Image.open(cut) as ci:
                cw_, ch_ = ci.size
            ph = (H - band) * (0.82 if x.tall else 0.9)
            pw = ph * cw_ / ch_
            if pw > W * (0.62 if x.wide else 0.95):
                pw = W * (0.62 if x.wide else 0.95)
                ph = pw * ch_ / cw_
            px = (W - pw) / 2 if not x.wide else W - pw - m
            d.image(cut, px, H - band - ph, pw, ph)
        if its:
            if x.wide:                                         # fekvő: a részletek balra, az alany jobbra
                x.draw(its, m, info_w, bottom=H - m, align="left")
            else:                                              # alul tömör sáv a részleteknek (az alany fölötte áll)
                d.rect(0, H - band, W, band, bg)
                d.rect(m, H - band, W - 2 * m, max(2, base * 0.004), acc)
                x.draw(its, m, W - 2 * m, bottom=H - m * 0.8, align="center" if center else "left")
        x.logo(_lum(bg) < 0.4, "tr")


def _frame(d: Doc, inset: float, W: float, H: float, color: str):
    lw = max(1.5, min(W, H) * 0.003)
    for x1, y1, x2, y2 in ((inset, inset, W - inset, inset), (W - inset, inset, W - inset, H - inset),
                           (W - inset, H - inset, inset, H - inset), (inset, H - inset, inset, inset)):
        d.line(x1, y1, x2, y2, color, lw)


# ---------- renderelés ----------
async def render(spec: dict[str, Any], content: dict[str, Any], size: str, *, photo: Optional[Photo] = None,
                 brand: Optional[dict[str, Any]] = None, export: Optional[list[str]] = None,
                 tmp: Optional[list[Path]] = None) -> dict[str, Any]:
    """Egy terv egy méretben → VectorCraft-fájlok (alapból: szerkeszthető .vectorcraft + PNG, nyomtatott méretnél PDF is)."""
    key = size_key(size)
    w, h, label, main = SIZES[key]
    own_tmp = tmp is None
    tmp = [] if tmp is None else tmp
    try:
        cut = await cutout_of(photo, tmp) if (photo and spec.get("archetype") == "cutout") else None
        if spec.get("archetype") == "cutout" and photo and not cut:
            spec = {**spec, "archetype": "split"}
        d = Doc(w, h, (content.get("title") or "Design")[:60])
        compose(d, spec, content, photo.path if photo else None, cut=cut, brand=brand,
                crop=(lambda aspect: crop_file(photo, aspect, tmp)) if photo else None,
                subject=(lambda aspect: subject_pos(photo, aspect)) if photo else None)
        fmts = export or (["vectorcraft", "png"] + (["pdf"] if main == "pdf" else []))
        from app.storage.naming import design_name
        res = await crafts.vector_run(commands=d.cmds, export=fmts, scale=2 if main == "pdf" else None,
                                      name=design_name("auto", content) + (f" ({label})" if label else ""))
    finally:
        if own_tmp:
            for p in tmp:
                p.unlink(missing_ok=True)
    res.update(size=key, size_label=label, width=w, height=h)
    return res
