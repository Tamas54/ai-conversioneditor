"""Közös képkocka-normalizálás a videós lépésekhez (merge-files, resize-video, a renderelő bemenete): HDR → SDR
tónusleképezés, vászonméret és illesztés (contain | cover | blur | stretch).

HDR (OMLETT 3.3): az iPhone HDR-videója HLG (color_transfer arib-std-b67), a HDR10 PQ (smpte2084). Leképezés nélkül
a 10 bites BT.2020 jel BT.709-ként kerül a kimenetbe: fakó, szürkés kép (mérve, szintetikus HLG-klip: átlagos telítettség
az SDR-eredeti 39%-a). Most: zscale lineárisra (npl=100) → BT.709 alapszínek → tonemap → BT.709 átvitel, 8 bit. Az
algoritmus VIDEO_TONEMAP (alap: mobius — a középtónusokat tartja, a csúcsfényeket lágyan hajlítja; mérve az SDR-
eredeti fényerejének 97%-a, telítettségének 85%-a; a hable ugyanott 81% / 69%, sötétebb). VIDEO_HDR_TONEMAP=0: ki.

A vászon (canvas): auto = az ELSŐ klip megjelenítési (elforgatás utáni) mérete — 10-19: két álló iPhone-klip (kódolva
1920×1080, rotation 90) + egy fekvő → a régi kód a kódolt méretből 1920×1080-as fekvő vásznat csinált, az álló klipek
608 px széles csíkban ültek; most 1080×1920. vertical | landscape | square = 1080×1920 | 1920×1080 | 1080×1080,
vagy „WxH”.

Illesztés (fit): contain = egészében, fekete sávval · cover = kitölti, a széle levágva · blur = egészében, a maradékot
a klip saját elmosott, kitöltő másolata adja (a telefonos videók szokásos képe) · stretch = torzítva (csak resize).
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from app.convert.options import OptionError

CANVASES = {"vertical": (1080, 1920), "landscape": (1920, 1080), "square": (1080, 1080)}
_CANVAS_ALIASES = {"portrait": "vertical", "9:16": "vertical", "story": "vertical", "reel": "vertical",
                   "horizontal": "landscape", "16:9": "landscape", "1:1": "square"}
FITS = ("contain", "cover", "blur")


def even(n: float) -> int:
    return max(2, int(n) // 2 * 2)


def canvas_size(spec: Optional[str], first: dict[str, Any]) -> tuple[int, int]:
    """canvas = auto | vertical | landscape | square | WxH → (W, H), páros. auto: az első klip megjelenítési mérete."""
    s = str(spec or "auto").strip().lower()
    s = _CANVAS_ALIASES.get(s, s)
    if s == "auto":
        w, h = int(first.get("display_width") or 0), int(first.get("display_height") or 0)
        if not w or not h:
            raise OptionError("the first clip has no picture size — give canvas, e.g. vertical or 1080x1920")
        return even(w), even(h)
    if s in CANVASES:
        return CANVASES[s]
    m = re.fullmatch(r"(\d{2,5})\s*[x×:]\s*(\d{2,5})", s)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if 16 <= w <= 7680 and 16 <= h <= 7680:
            return even(w), even(h)
    raise OptionError("canvas: auto | vertical | landscape | square | WxH (e.g. 1080x1920)")


def same_shape(m: dict[str, Any], W: int, H: int, tol: float = 0.01) -> bool:
    """A klip megjelenítési aránya a vászonéval egyezik-e (ilyenkor nincs mit kitölteni)."""
    w, h = m.get("display_width") or 0, m.get("display_height") or 0
    return bool(w and h) and abs((w / h) / (W / H) - 1) <= tol


def fit_chain(W: int, H: int, fit: str, tag: str = "f") -> str:
    """Egy bemenet → W×H kocka, egyetlen láncként (-vf-be vagy a filter_complex egy ágába, címkék nélkül a két végén).
    A blur ág belső címkéi a `tag`-gel egyediek (több bemenet egy gráfban)."""
    fit = (fit or "contain").lower()
    if fit == "cover":
        return f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"
    if fit == "stretch":
        return f"scale={W}:{H},setsar=1"
    if fit == "blur":
        # háttér: kicsinyítve kitölt, dobozelmosás, vissza nagyra (a teljes méretű gblur-nél sokszorta gyorsabb),
        # kissé sötétebb és fakóbb, hogy az előtér kiváljon
        sw, sh = max(16, even(W / 8)), max(16, even(H / 8))
        r = max(1, min(sw, sh) // 5)
        return (f"split=2[{tag}a][{tag}b];"
                f"[{tag}a]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},boxblur={r}:2,"
                f"scale={W}:{H},eq=brightness=-0.06:saturation=0.85[{tag}bg];"
                f"[{tag}b]scale={W}:{H}:force_original_aspect_ratio=decrease[{tag}fg];"
                f"[{tag}bg][{tag}fg]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2,setsar=1")
    return f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1"


TONEMAPS = ("mobius", "hable", "reinhard", "clip", "linear", "gamma")


def tonemap_filters(m: dict[str, Any]) -> list[str]:
    """HDR (HLG / PQ) bemenet → SDR BT.709 8 bit; SDR bemenetnél üres."""
    kind = m.get("hdr")
    if kind not in ("hlg", "pq") or os.getenv("VIDEO_HDR_TONEMAP", "1").strip() in ("0", "off", "false", "no"):
        return []
    algo = os.getenv("VIDEO_TONEMAP", "mobius").strip().lower()
    algo = algo if algo in TONEMAPS else "mobius"
    tin = "arib-std-b67" if kind == "hlg" else "smpte2084"
    pin = m.get("color_primaries") if m.get("color_primaries") in ("bt2020", "bt709") else "bt2020"
    mat = "bt2020c" if m.get("color_space") == "bt2020c" else "bt2020nc"
    rin = "pc" if m.get("color_range") == "pc" else "tv"
    return [f"zscale=tin={tin}:pin={pin}:min={mat}:rin={rin}:t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709",
            f"tonemap=tonemap={algo}:desat=0", "zscale=t=bt709:m=bt709:r=tv", "format=yuv420p"]


def head_filters(m: dict[str, Any]) -> list[str]:
    """A bemenet elejére tett szűrők (a vászonra illesztés előtt): HDR-nél tónusleképezés, mindig 8 bites 4:2:0."""
    return tonemap_filters(m) or ["format=yuv420p"]


def input_chain(m: dict[str, Any], W: int, H: int, fit: str, tag: str = "f") -> str:
    """head_filters + illesztés egy láncban. Ha a klip aránya a vászoné, a blur fölösleges: contain (gyorsabb)."""
    if fit == "blur" and same_shape(m, W, H):
        fit = "contain"
    return ",".join(head_filters(m) + [fit_chain(W, H, fit, tag)])
