"""Gyors végső render a saját felvételre tett grafikához (overlay-projekt): a felvétel egyetlen ffmpeg-menetben kapja
meg a grafikát, a feliratot és a hangot — az EffectCraft csak ott rajzol, ahol grafika van.

Mérve (10-22, Tamás TV tesztvideó, 2:06, 16 réteg + felirat, i7-11850H): a régi út a TELJES kompozíciót kockánként
rajzolta egy ProRes LT köztes fájlba (325 s, 1,6 GB), és abból kódolt; a kockánkénti költség ~85 ms volt, ebből a
teljes képméretű vízjel-réteg egymaga 27 ms — a videó teljes hosszán. Egy 1 órás felvételnél ez órákat jelentett.
Most:
  · grafikás szakaszok — a felvétel és a felirat kikapcsolva, átlátszó ProRes 4444, csak a szakasz idejére
    (`render --start --end --channels rgba`), párhuzamosan; ahol a felvétel maga mozog (ráközelítés, osztott kép),
    ott a szakasz a felvétellel együtt, átlátszatlanul készül;
  · végig látható rétegek (vízjel, logó: PERSISTENT_S-nél hosszabbak) — egy rövid mintából megkeressük, hol válik
    ismétlődővé (kockánkénti md5); a bevezetés + egy kör + a kivezetés készül el, az ffmpeg ismételgeti;
    ha nem ismétlődik, a teljes idejére készül (ezt a check megmondja);
  · felirat — ASS (libass) a make-videó feliratsávjának pontos méreteivel (lekerekített sáv, Inter, szavankénti
    kiemelés), a projekt felirat-rétegei helyett; ha a projektben kézzel átírták a feliratot, a régi út marad;
  · hang — az EffectCraft keveri, csak hangként (a teljes 2:06 ~1 s);
  · egy ffmpeg-menet: felvétel → rétegek (időre) → felirat → x264 (a végső profil, encoding.x264("final")).
Ami ezen az úton nem adható vissza pontosan (korrekciós réteg, nem normál keverési mód, track matte, effekt a
felvételen), annál a régi út fut. VIDEO_FAST_RENDER=0 kikapcsolja.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

PERSISTENT_S = 30.0          # ennél hosszabb réteg végig látható (vízjel, logó, állandó sáv)
PROBE_S = 12.0               # a végig látható rétegek mintája — ebben keressük az ismétlődést
OUTRO_S = 1.0                # a kivezetés (elhalványulás) a végig látható réteg végén
MERGE_GAP_S = 0.5            # ennél kisebb szünet két grafikás szakasz között: egy szakasz
MAX_SPANS = 60               # ennyi szakasznál több: a közelebbieket összevonjuk (ennyi bemenet egy ffmpeg-menetben)
INTER_DIR = "/usr/share/fonts/opentype/inter"
_CAPTION = re.compile(r"^Caption (box )?(\d+)$")
NOT_OVERLAY = "not a project on your own footage"


def enabled() -> bool:
    return os.getenv("VIDEO_FAST_RENDER", "1").strip().lower() not in ("0", "false", "no", "off")


def _workers() -> int:
    return max(1, min(4, (os.cpu_count() or 4) // 4))


# ---------- a projekt felmérése (a motor szkriptfelületén) ----------

_INSPECT = r"""
var c = null;
for (var i = 1; i <= app.project.numItems; i++) { var it = app.project.item(i); if (it instanceof CompItem && it.name == __COMP__) c = it; }
var out = {W: c.width, H: c.height, fps: c.frameRate, dur: c.duration, layers: []};
var tprops = ["ADBE Anchor Point", "ADBE Position", "ADBE Scale", "ADBE Rotate Z", "ADBE Opacity"];
for (var j = 1; j <= c.numLayers; j++) {
  var L = c.layer(j), o = {i: j, name: L.name, a: L.inPoint, b: L.outPoint, on: !!L.enabled, video: !!L.hasVideo,
                           nul: !!L.nullLayer, adj: !!L.adjustmentLayer, keys: [], fx: 0};
  try { o.blend = String(L.blendingMode); o.normal = L.blendingMode == BlendingMode.NORMAL; } catch (e) { o.normal = true; }
  try { o.matte = !!L.trackMatteType && L.trackMatteType != TrackMatteType.NO_TRACK_MATTE; } catch (e) { o.matte = false; }
  try { if (L.source && L.source.file) o.file = L.source.file.name; } catch (e) {}
  try { o.st = L.startTime; if (L.timeRemapEnabled) { var R = L.property("ADBE Time Remapping"); o.tr = [];
        for (var q = 1; q <= R.numKeys; q++) o.tr.push([R.keyTime(q), R.keyValue(q)]); } } catch (e) {}
  try { var tp = L.property("ADBE Text Properties"); if (tp) o.text = tp.property("ADBE Text Document").value.text; } catch (e) {}
  try { var fxp = L.property("ADBE Effect Parade"); o.fx = fxp ? fxp.numProperties : 0; } catch (e) {}
  try {
    var T = L.property("ADBE Transform Group");
    o.kv = [];
    for (var p = 0; p < tprops.length; p++) { var P = T.property(tprops[p]); if (!P) continue;
      for (var k = 1; k <= P.numKeys; k++) { o.keys.push(P.keyTime(k)); if (tprops[p] != "ADBE Opacity") o.kv.push([tprops[p], P.keyTime(k), P.keyValue(k)]); }
      if (tprops[p] != "ADBE Opacity") { o.t0 = (o.t0 || []).concat([P.valueAtTime(0, false)]); o.t1 = (o.t1 || []).concat([P.valueAtTime(c.duration, false)]); } }
  } catch (e) {}
  out.layers.push(o);
}
writeLn("@@" + JSON.stringify(out));
"""

_SWITCH = r"""
var c = null;
for (var i = 1; i <= app.project.numItems; i++) { var it = app.project.item(i); if (it instanceof CompItem && it.name == __COMP__) c = it; }
var off = __OFF__;
for (var j = 1; j <= c.numLayers; j++) if (off.indexOf(j) >= 0) c.layer(j).enabled = false;
writeLn("ok");
"""


async def _script(work: Path, name: str, code: str, project: str, save_as: Optional[str] = None) -> str:
    from app.tools import video
    (work / name).write_text(code, "utf-8")
    args = ["script", name, project] + (["--save-as", save_as] if save_as else [])
    return await video._run(*args, cwd=work)


async def inspect(project: Path) -> dict[str, Any]:
    from app.tools import video
    comp = video._main_comp(project)
    out = await _script(project.parent, "fr-inspect.jsx", _INSPECT.replace("__COMP__", json.dumps(comp)), project.name)
    line = next((ln for ln in out.splitlines() if ln.startswith("@@")), None)
    if not line:
        raise RuntimeError("the project couldn't be read: " + out.strip()[-300:])
    d = json.loads(line[2:])
    d["comp"] = comp
    return d


async def variant(project: Path, info: dict[str, Any], off: list[int], name: str) -> str:
    """A projekt egy változata, a megadott rétegek kikapcsolva (a fő kompozícióban) → a fájl neve a munkamappában."""
    await _script(project.parent, f"fr-{name}.jsx",
                  _SWITCH.replace("__COMP__", json.dumps(info["comp"])).replace("__OFF__", json.dumps(sorted(set(off)))),
                  project.name, save_as=f"{name}.ecproj")
    return f"{name}.ecproj"


# ---------- mit hogyan ----------

def _same(a: Any, b: Any) -> bool:
    """Ugyanaz-e két kulcskocka-érték — a számok lebegőpontos zaj nélkül (10-26: az osztott kép a felvétel méretét
    199.99999999999994-re tette vissza a 200 helyett, és a moved() a felvétel ~700 másodpercét a motorral rajzoltatta)."""
    num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)          # noqa: E731
    if num(a) and num(b):
        return abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(a)), abs(float(b)))
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def classify(info: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """A fő kompozíció rétegei: felvétel, felirat, grafika (rövid / végig látható). → {ok, why, footage, captions, gfx,
    persistent, opaque} — ok=False esetén a why mondja, miért marad a régi út."""
    base = (spec.get("base") or {})
    label = "Footage"
    L = info["layers"]
    vis = [x for x in L if x["on"] and x["video"] and not x["nul"]]
    foot = [x for x in vis if x["name"] == label and x.get("file")]
    if spec.get("kind") != "overlay" or not base:
        return {"ok": False, "why": NOT_OVERLAY}
    if len(foot) != 1:
        return {"ok": False, "why": "the footage layer isn't the project's only base"}
    f = foot[0]
    if f["i"] != max(x["i"] for x in vis):
        return {"ok": False, "why": "something lies under the footage"}
    if f["fx"]:
        return {"ok": False, "why": "the footage has an effect on it in the project"}
    if retimed(f, float(info["dur"]), float(info["fps"])):
        return {"ok": False, "why": "the footage was cut or moved in time in the editor"}
    caps = [x for x in vis if _CAPTION.match(x["name"])]
    gfx = [x for x in vis if x is not f and x not in caps]
    for x in gfx + caps:
        if x["adj"]:
            return {"ok": False, "why": f"“{x['name']}” is an adjustment layer"}
        if not x.get("normal", True):
            return {"ok": False, "why": f"“{x['name']}” uses a blend mode ({x.get('blend')})"}
        if x.get("matte"):
            return {"ok": False, "why": f"“{x['name']}” uses a track matte"}
    dur, fps = float(info["dur"]), float(info["fps"])
    pers = [x for x in gfx if min(dur, x["b"]) - max(0.0, x["a"]) >= PERSISTENT_S]
    short = [x for x in gfx if x not in pers]
    opaque = [(max(0.0, a - 1 / fps), min(dur, b + 1 / fps)) for a, b in moved(f, dur)]   # a felvétel el van mozdítva
    return {"ok": True, "why": "", "footage": f, "captions": caps, "gfx": gfx, "persistent": pers, "short": short,
            "opaque": opaque}


def retimed(f: dict[str, Any], dur: float, fps: float) -> bool:
    """A felvétel-réteg ideje a projektben nem a felvételé (a szerkesztőben megvágva, eltolva, lassítva): a gyors út az
    eredeti idővonalat tenné a grafika alá. Ami szabályos: 0-tól indul, a végéig tart, és az időtérkép (ha van) azonos
    leképezés — a zárókártya kimerevítése (az utolsó kulcs után áll) is ilyen."""
    eps = 1.5 / max(fps, 1)
    if abs(float(f.get("st") or 0)) > eps or float(f["a"]) > eps or float(f["b"]) < dur - eps:
        return True
    return any(abs(float(t) - float(v[0] if isinstance(v, list) else v)) > eps for t, v in (f.get("tr") or []))


def moved(f: dict[str, Any], dur: float) -> list[tuple[float, float]]:
    """A felvétel-réteg időszakai, amikor el van mozdítva (ráközelítés, osztott kép): ahol egy kulcskocka-pár bármelyik
    végén a tulajdonság nem a nyugalmi (0. másodperces) értékén áll. A 0-nál álló kiinduló kulcs magában nem mozgás
    (10-22: a ráközelítés 0 s-os kezdőkulcsa miatt 12 percnyi felvételt rajzolt újra, és a fájl túllépte a 2 GiB-et)."""
    out: list[tuple[float, float]] = []
    by: dict[str, list[tuple[float, Any]]] = {}
    for prop, t, v in f.get("kv") or []:
        by.setdefault(prop, []).append((float(t), v))
    rest = dict(zip((p for p in by), f.get("t0") or []))
    for prop, ks in by.items():
        ks.sort(key=lambda x: x[0])
        r = ks[0][1] if ks[0][0] <= 1e-3 else rest.get(prop, ks[0][1])
        off = lambda v: not _same(v, r)                                                     # noqa: E731
        if off(ks[0][1]):
            out.append((0.0, ks[0][0]))
        for (ta, va), (tb, vb) in zip(ks, ks[1:]):
            if off(va) or off(vb):
                out.append((ta, tb))
        if off(ks[-1][1]):
            out.append((ks[-1][0], dur))
    return sorted(out)


def part_seconds(W: int, H: int, fps: float) -> float:
    """Egy renderelt darab hossza úgy, hogy a fájl 2 GiB alatt maradjon (a renderelő zárkája ennél nagyobbat nem ír,
    jail.py RLIMIT_FSIZE): mérve ProRes 4444 ~0,18, LT ~0,21 bájt/pixel/kocka → 0,25-tel számolva 1,2 GB."""
    return max(5.0, min(60.0, 1.2e9 / (W * H * max(fps, 1) * 0.25)))


def split(parts: list[tuple[float, float]], most: float, fps: float) -> list[tuple[float, float]]:
    """A szakaszok legfeljebb `most` másodperces, kockahatáros darabokra."""
    out = []
    for a, b in parts:
        n = max(1, math.ceil((b - a) / max(most - 1 / fps, 1 / fps) - 1e-9))     # a kockára igazítás se vigye `most` fölé
        cuts = [a] + [round(math.floor((a + (b - a) * k / n) * fps + 1e-6) / fps, 4) for k in range(1, n)] + [b]
        out += [(x, y) for x, y in zip(cuts, cuts[1:]) if y - x > 1e-3]
    return out


def captions_match(caps: list[dict[str, Any]], rows: list[dict[str, Any]]) -> bool:
    """A projekt felirat-rétegei ugyanazok-e, mint amiket a projekt leírása (spec) ad: darabszám, idők, szöveg."""
    texts = sorted((x for x in caps if not x["name"].startswith("Caption box")), key=lambda x: int(_CAPTION.match(x["name"])[2]))
    if len(texts) != len(rows):
        return False
    for x, r in zip(texts, rows):
        if abs(x["a"] - r["t0"]) > 0.05 or abs(x["b"] - r["t1"]) > 0.05:
            return False
        if " ".join(str(x.get("text") or "").split()) != " ".join(str(r["text"]).split()):
            return False
    return True


def spans(intervals: list[tuple[float, float]], dur: float, fps: float, gap: float = MERGE_GAP_S,
          cap: int = MAX_SPANS) -> list[tuple[float, float]]:
    """Időszakaszok összevonva (gap-nél kisebb szünet = egy szakasz), kockahatárra kerekítve, a videón belül; ha cap-nél
    több marad, a legkisebb szüneteket vonjuk össze, amíg belefér."""
    iv = sorted((max(0.0, a), min(dur, b)) for a, b in intervals if min(dur, b) - max(0.0, a) > 1e-3)
    out: list[list[float]] = []
    for a, b in iv:
        if out and a - out[-1][1] < gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    while len(out) > cap:
        k = min(range(len(out) - 1), key=lambda i: out[i + 1][0] - out[i][1])
        out[k][1] = max(out[k][1], out.pop(k + 1)[1])
    return [(round(math.floor(a * fps + 1e-6) / fps, 4), round(min(dur, math.ceil(b * fps - 1e-6) / fps), 4)) for a, b in out]


def cycle(hashes: list[str]) -> Optional[tuple[int, int]]:
    """Kockánkénti md5-ök → (t0, P): a t0. kockától P hosszú körben ismétlődik (legalább két kör látszik), a
    legrövidebb bevezetés + kör; None, ha nem ismétlődik."""
    n, best = len(hashes), None
    for P in range(1, n // 2 + 1):
        i = n - P - 1
        while i >= 0 and hashes[i] == hashes[i + P]:
            i -= 1
        t0 = i + 1
        if n - t0 >= 2 * P and (best is None or t0 + P < best[0] + best[1]):
            best = (t0, P)
    return best


def outside(a: float, b: float, holes: list[tuple[float, float]]) -> str:
    """ffmpeg enable-kifejezés: [a, b) időben, a lyukakon (a grafikás szakaszokon) kívül."""
    e = f"gte(t,{a:.4f})*lt(t,{b:.4f})"
    for x, y in holes:
        if y > a and x < b:
            e += f"*not(between(t,{x:.4f},{y - 1e-4:.4f}))"
    return e


# ---------- felirat: ASS a make-videó feliratsávjának méreteivel ----------

def _ass_color(rgb: list[float] | tuple[float, ...], alpha: float = 1.0) -> str:
    r, g, b = (max(0, min(255, round(v * 255))) for v in rgb[:3])
    return f"&H{round(255 * (1 - alpha)):02X}{b:02X}{g:02X}{r:02X}"


def _ass_rgb(rgb: list[float] | tuple[float, ...]) -> str:
    """Szín a szöveg közbeni felülírásokhoz (\\1c): &HBBGGRR& — a stílussorban az alfa is benne van, itt nincs."""
    r, g, b = (max(0, min(255, round(v * 255))) for v in rgb[:3])
    return f"&H{b:02X}{g:02X}{r:02X}&"


def _ass_time(t: float, fps: Optional[float] = None) -> str:
    """ASS-idő (centiszekundum). fps megadva (a szakasz-előnézet): a t utáni első kockára igazítva — az a centiszekundum,
    amelytől a libass (az ffmpeg ass-szűrője: a kocka ideje egész ms-ra csonkolva) pontosan ettől a kockától mutatja. Így
    a felirat kockái csak a kockaszámtól függnek: egy vágás utáni, nem egész centiszekundumos eltolás (pl. 600 kocka
    29,993 fps-en = 20,0047 s) nem tol el egy-egy szót egy kockával (preview_cache: a feliratos szakaszok újrahasználhatók)."""
    if fps:
        f = math.ceil(t * fps - 1e-6)                    # (= preview_cache._on: az első kocka, amelynek ideje ≥ t)
        # a kocka ideje ms-ban, fél ms tartalékkal (a szűrő lebegőpontos szorzata egy hajszállal alá eshet): a legnagyobb
        # centiszekundum, ami még nem később — és az előző kocka (≥ 33 ms-mal korábban) még nem látja
        cs = max(0, math.floor(f * 1000 / fps - 0.5) // 10) if f > 0 else 0
    else:
        cs = max(0, round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _round_rect(w: float, h: float, r: float) -> str:
    r = max(0.0, min(r, w / 2, h / 2))
    k = r * 0.5523                                                   # negyedkör Bézier-közelítése
    f = lambda v: f"{v:.1f}".rstrip("0").rstrip(".")                  # noqa: E731
    return (f"m {f(r)} 0 l {f(w - r)} 0 b {f(w - r + k)} 0 {f(w)} {f(r - k)} {f(w)} {f(r)} l {f(w)} {f(h - r)} "
            f"b {f(w)} {f(h - r + k)} {f(w - r + k)} {f(h)} {f(w - r)} {f(h)} l {f(r)} {f(h)} "
            f"b {f(r - k)} {f(h)} 0 {f(h - r + k)} 0 {f(h - r)} l 0 {f(r)} b 0 {f(r - k)} {f(r - k)} 0 {f(r)} 0")


_FONTS: dict[tuple[str, int], Any] = {}


def _font(weight: str, size: int):
    from PIL import ImageFont
    key = (weight, size)
    if key not in _FONTS:
        try:
            _FONTS[key] = ImageFont.truetype(f"{INTER_DIR}/Inter-{weight}.otf", size)
        except OSError:
            _FONTS[key] = None
    return _FONTS[key]


_LINES: dict[tuple[Any, ...], list[list[int]]] = {}


def _lines(words: list[str], weight: str, size: int, width: float) -> list[list[int]]:
    """Szótördelés a doboz szélességére, az Inter tényleges szélességeivel (ha nincs meg, betűszám-becsléssel).
    Megjegyezve (a bemenetének tiszta függvénye): a PIL-mérés egy 30 perces műsor ASS-ének ~75 %-a (0,2–0,6 s), így egy
    akcentus- / stílusváltás vagy egy sor szövegének javítása csak a megváltozott sorokat méri újra (az élő előnézet
    feliratának a szerkesztés után < 1 s alatt kell látszania, live_captions)."""
    key = (tuple(words), weight, size, width)
    hit = _LINES.get(key)
    if hit is None:
        ft = _font(weight, size)
        meas = (lambda s: ft.getlength(s)) if ft else (lambda s: len(s) * size * 0.53)    # noqa: E731
        out: list[list[int]] = [[]]
        for i, w in enumerate(words):
            trial = " ".join(words[j] for j in out[-1] + [i])
            if out[-1] and meas(trial) > width:
                out.append([i])
            else:
                out[-1].append(i)
        if len(_LINES) >= 20000:
            _LINES.clear()
        _LINES[key] = hit = out
    return [ln[:] for ln in hit]


# Az ASS betűmérete nem az em-méret: mérve (libass, Inter Semi Bold, 100-as méret) a szöveg az em-100-as szélesség
# 0,8208-szorosa → az em-méretből (a renderelőé, video_builder.jsx) ennyivel szorozva ugyanakkora a betű.
_ASS_SIZE = 1.2183
# A fontconfig család-neve „Inter Semi Bold” (szóközzel); „Inter SemiBold”-ra a libass csendben tartalék betűt vesz.
_SEMIBOLD = "Inter Semi Bold"


def ass_subtitles(subs: list[dict[str, Any]], style: str, W: int, H: int, total: float, accent: list[float],
                  fps: Optional[float] = None) -> str:
    """A spec feliratai → ASS (PlayRes = a videó mérete), ugyanazokkal a darabokkal és szóidőkkel, amiket a projekt
    felirat-rétegei kaptak (video.caption_track), a sáv és a betű a builder méreteivel (video_builder.jsx). fps: az idők
    kockára igazítva (_ass_time) — a szakasz-előnézet ezt kéri."""
    from app.tools import video
    Lc = video._layout(W, H)
    rows = video.caption_track(subs, W, H, total, style)
    cap, pad, capY, capW = Lc["capSize"], Lc["capPad"], Lc["capY"], Lc["capW"]
    acc, white_o = _ass_rgb(accent), "&HFFFFFF&"
    white = "&H00FFFFFF"
    head = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 2",
            "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "", "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding",
            f"Style: Cap,{_SEMIBOLD},{round(cap * _ASS_SIZE)},{white},{white},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1",
            f"Style: Bar,Inter,20,{_ass_color([0.04, 0.045, 0.06], 0.72)},{white},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
            "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    ev: list[str] = []

    def dialogue(layer: int, a: float, b: float, st: str, text: str) -> None:
        if b - a >= 0.01:
            ev.append(f"Dialogue: {layer},{_ass_time(a, fps)},{_ass_time(b, fps)},{st},,0,0,0,,{text}")

    def states(r: dict[str, Any], words: list[str]) -> list[tuple[float, float, int]]:
        """(a, b, i): i = az épp mondott szó (−1: még egy sem)."""
        ts = [min(max(float(t), r["t0"]), r["t1"]) for t in (r.get("words") or [])][:len(words)]
        if style == "box" or not ts:
            return [(r["t0"], r["t1"], -2)]
        cuts = [r["t0"]] + ts + [r["t1"]]
        return [(cuts[k], cuts[k + 1], k - 1) for k in range(len(cuts) - 1)]

    def word_markup(words: list[str], lines: list[list[int]], i: int, base: str) -> str:
        out_lines = []
        for ln in lines:
            parts = []
            for j in ln:
                w = words[j]
                if i == -2:
                    parts.append(w)
                elif style == "karaoke":
                    parts.append(f"{{\\1c{acc}}}{w}{{\\1c{base}}}" if j <= i else w)
                elif style == "reveal":
                    parts.append(w if j <= i else f"{{\\alpha&HFF&}}{w}{{\\alpha&H00&}}")
                else:                                               # highlight, bold: az épp mondott szó
                    parts.append(f"{{\\1c{acc}}}{w}{{\\1c{base}}}" if j == i else w)
            out_lines.append(" ".join(parts))
        return "\\N".join(out_lines)

    for r in rows:
        words = str(r["text"]).split()
        if style == "bold":
            size = int(r.get("size") or round(cap * 1.5))
            up = [w.upper() for w in words]
            lines = _lines(up, "Bold", size, capW)
            x, y = W / 2, capY - (len(lines) - 1) * size * 0.59
            stroke = max(3, round(size * 0.11))
            tag = (f"{{\\an5\\pos({x:.1f},{y:.1f})\\fnInter\\b1\\fs{round(size * _ASS_SIZE)}\\bord{stroke / 2:.1f}"
                   f"\\3c&H000000&\\shad0}}")
            for a, b, i in states(r, up):
                dialogue(1, a, b, "Cap", tag + word_markup(up, lines, i, white_o))
            continue
        lines = _lines(words, "SemiBold", cap, capW)
        n = max(1, len(lines))
        bw, bh = capW + pad * 2, n * cap * 1.28 + pad * 1.4
        dialogue(0, r["t0"], r["t1"], "Bar",
                 f"{{\\an7\\pos({W / 2 - bw / 2:.1f},{capY - bh / 2:.1f})\\p1}}{_round_rect(bw, bh, pad * 0.7)}{{\\p0}}")
        tag = f"{{\\an5\\pos({W / 2:.1f},{capY + cap * 0.08:.1f})}}"
        for a, b, i in states(r, words):
            dialogue(1, a, b, "Cap", tag + word_markup(words, lines, i, white_o))
    return "\n".join(head + ev) + "\n"


# ---------- a render ----------

async def _ffmpeg(*args: str, cwd: Optional[Path] = None, timeout: Optional[float] = None) -> None:
    from app.config import settings
    from app.convert.engines import run
    await run("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args, cwd=cwd, timeout=timeout or settings.VIDEO_TIMEOUT_S)


async def base_filters(path: Path) -> list[str]:
    """A felvétel színmátrixa BT.709-re, ha nem az: a kimenet BT.709-nek jelölt (encoding.bt709_tags), a pixelek viszont
    változatlanul mennének át. BT.601-es (smpte170m, bt470bg), vagy jelöletlen SD-felvételnél átszámoljuk — a lejátszók is
    SD-nél BT.601-et feltételeznek (10-22: jelöletlen 640×360-as próbán minden kocka 24 dB-re tért el)."""
    from app.convert.engines import run
    out, _ = await run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height,color_space",
                       "-of", "json", str(path), timeout=60)
    st = (json.loads(out.decode("utf-8", "replace") or "{}").get("streams") or [{}])[0]
    cs, h = str(st.get("color_space") or "unknown"), int(st.get("height") or 0)
    if cs in ("bt470bg", "smpte170m", "bt601") or (cs in ("unknown", "") and 0 < h < 720):
        return ["scale=in_color_matrix=bt601:out_color_matrix=bt709:in_range=tv:out_range=tv"]
    return []


async def _md5s(path: Path) -> list[str]:
    from app.convert.engines import run
    out, _ = await run("ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "framemd5", "-", timeout=600)
    return [ln.rsplit(",", 1)[1].strip() for ln in out.decode("utf-8", "replace").splitlines()
            if ln and not ln.startswith("#") and "," in ln]


async def _cut_frames(src: Path, dst: Path, first: int, count: int) -> None:
    """Átlátszó ProRes 4444 klip kockapontos darabja (a minta bevezetése / köre), az alfa megmarad."""
    await _ffmpeg("-i", str(src), "-vf", f"trim=start_frame={first}:end_frame={first + count},setpts=PTS-STARTPTS",
                  "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-an", str(dst))


async def render(project: Path, out: Path, *, spec: dict[str, Any], draft: bool = False,
                 step: Optional[Callable[[str], None]] = None) -> Optional[dict[str, Any]]:
    """A projekt (a munkamappában, stage után) → out MP4 a gyors úton. None: ez a projekt a régi úton renderelendő
    (a why a naplóban); kivétel: a gyors út elakadt (a hívó a régi útra vált, és megmondja)."""
    from app.tools import encoding, footage, mediaprobe, video
    t_start = time.time()
    work = project.parent
    info = await inspect(project)
    kind = classify(info, spec)
    opts = spec.get("opts") or {}
    subs = opts.get("subtitles") or []
    W, H, fps, dur = int(info["W"]), int(info["H"]), float(info["fps"]), float(info["dur"])
    style = opts.get("caption_style") or "box"
    if kind["ok"] and kind["captions"]:
        rows = video.caption_track(subs, W, H, dur, style) if subs else []
        if not captions_match(kind["captions"], rows):
            kind = {"ok": False, "why": "the subtitles were changed by hand in the editor"}
    if not kind["ok"]:
        log.info("gyors render: nem ez az út (%s) — %s", kind["why"], project)
        return {"skipped": kind["why"]}
    say = step or (lambda s: None)
    foot = kind["footage"]
    caption_ix = [x["i"] for x in kind["captions"]]
    notes: list[str] = []
    res = ["--resolution", "half"] if draft else []
    scale = 0.5 if draft else 1.0
    ow, oh = (W // 4 * 2, H // 4 * 2) if draft else (W, H)

    # 1. a grafikás szakaszok (a végig látható rétegek nélkül számolva; a szakaszokban ők is rajta vannak)
    holes = spans([(x["a"], x["b"]) for x in kind["short"]], dur, fps)
    opaque = spans(kind["opaque"], dur, fps) if kind["opaque"] else []
    clear = [s for s in holes if not any(s[1] > a and s[0] < b for a, b in opaque)]
    merged_opaque = spans(opaque + [s for s in holes if s not in clear], dur, fps)
    alpha_proj = await variant(project, info, [foot["i"]] + caption_ix, "fr-alpha") if (clear or kind["persistent"]) else None
    opaque_proj = await variant(project, info, caption_ix, "fr-opaque") if merged_opaque else None
    most = part_seconds(W, H, fps)                              # egy darab fájlja 2 GiB alatt (a zárka korlátja)
    jobs: list[tuple[str, float, float, str, bool]] = [(alpha_proj, a, b, f"span-{k:03d}.mov", True)
                                                       for k, (a, b) in enumerate(split(clear, most, fps))]
    jobs += [(opaque_proj, a, b, f"full-{k:03d}.mov", False) for k, (a, b) in enumerate(split(merged_opaque, most, fps))]
    parts = sorted(clear + merged_opaque)                       # itt a szakasz-render a végig látható rétegeket is viszi
    gate = asyncio.Semaphore(_workers())
    base = spec.get("base") or {}
    findings: list[str] = []
    proxy = bool(base.get("proxy")) and not draft               # a projektben munkamásolat: a végső kép az eredetiből
    src = footage.original(base) if proxy else None
    if proxy and src is None:                                   # lejárt az eredeti: a munkamásolatból, és ezt megmondjuk
        proxy = False
        findings.append(footage.gone_note(base))
    plain = float((base.get("plain") or {}).get("duration") or base.get("duration") or dur)
    if proxy and opaque_proj:                                   # ahol a motor rajzolja a felvételt: teljes méretű darab
        say("full-size footage for the moved parts")
        whole = await footage.full(base, src, work) if footage.stabilized(base) else None

        async def own(k: int, a: float, b: float) -> str:
            if whole is not None:
                return await footage.swap(work / opaque_proj, info["comp"], foot["file"], whole.name, "fr-opaque-full")
            name = f"fr-foot-{k:03d}.mp4"
            async with gate:
                await footage.clip(base, src, work / name, a, b, plain, work)
            return await footage.swap(work / opaque_proj, info["comp"], foot["file"], name, f"fr-opaque-{k:03d}", start=a)
        ops = [j for j in jobs if not j[4]]
        if whole is not None:
            p_full = await own(0, 0.0, dur)
            jobs = [j if j[4] else (p_full, *j[1:]) for j in jobs]
        else:
            projs = await asyncio.gather(*(own(k, j[1], j[2]) for k, j in enumerate(ops)))
            it = iter(projs)
            jobs = [j if j[4] else (next(it), *j[1:]) for j in jobs]

    async def one(proj: str, a: float, b: float, name: str, alpha: bool) -> None:
        args = ["render", "--project", proj, "--comp", info["comp"], "--out", name, "--start", f"{a:.4f}", "--end", f"{b:.4f}",
                "--format", "prores", "--audio", "off", "--json", *res]
        args += ["--prores", "4444", "--channels", "rgba"] if alpha else ["--prores", "lt"]
        async with gate:
            await video._run(*args, cwd=work)
        if not (work / name).exists():
            raise RuntimeError(f"the graphics from {a:.1f} s to {b:.1f} s weren't rendered")

    say(f"rendering {len(jobs)} graphics part(s)")
    t_g = time.time()
    await asyncio.gather(*(one(*j) for j in jobs))
    g_secs = round(time.time() - t_g, 1)

    # 2. a végig látható rétegek: minta → ismétlődés → bevezetés + kör + kivezetés
    pers_inputs: list[dict[str, Any]] = []        # {name, a, b, enable, loop, period}: a végig látható rétegek darabjai
    if kind["persistent"]:
        say("the layers that stay on screen")
        others = [x["i"] for x in kind["gfx"] if x not in kind["persistent"]]
        p_proj = await variant(project, info, [foot["i"]] + caption_ix + others, "fr-persist")
        p0 = max(0.0, min(x["a"] for x in kind["persistent"]))
        p1 = min(dur, max(x["b"] for x in kind["persistent"]))
        probe_end = min(p1, p0 + PROBE_S)
        await one(p_proj, p0, probe_end, "persist-probe.mov", True)
        hashes = await _md5s(work / "persist-probe.mov")
        cyc = cycle(hashes) if probe_end < p1 - OUTRO_S else None
        if cyc and p1 - OUTRO_S > p0 + (cyc[0] + cyc[1]) / fps:
            t0, P = cyc
            if t0:
                await _cut_frames(work / "persist-probe.mov", work / "persist-intro.mov", 0, t0)
                pers_inputs.append({"name": "persist-intro.mov", "a": p0, "b": p0 + t0 / fps,
                                    "enable": outside(p0, p0 + t0 / fps, parts), "loop": False})
            await _cut_frames(work / "persist-probe.mov", work / "persist-cycle.mov", t0, P)
            loop_from = p0 + t0 / fps
            pers_inputs.append({"name": "persist-cycle.mov", "a": loop_from, "b": p1 - OUTRO_S, "period": P / fps,
                                "enable": outside(loop_from, p1 - OUTRO_S, parts), "loop": True})
            await one(p_proj, p1 - OUTRO_S, p1, "persist-outro.mov", True)
            pers_inputs.append({"name": "persist-outro.mov", "a": p1 - OUTRO_S, "b": p1,
                                "enable": outside(p1 - OUTRO_S, p1, parts), "loop": False})
            notes.append(f"Layers on screen all the time ({', '.join(x['name'] for x in kind['persistent'][:3])}): drawn once — "
                         f"{t0 / fps:.1f} s in, then a {P / fps:.2f} s cycle repeated.")
        elif probe_end >= p1 - 1e-3:                    # a minta az egész réteg: az lesz a rajza
            pers_inputs.append({"name": "persist-probe.mov", "a": p0, "b": p1, "enable": outside(p0, p1, parts), "loop": False})
        else:
            for k, (a, b) in enumerate(split([(p0, p1)], most, fps)):
                await one(p_proj, a, b, f"persist-full-{k:03d}.mov", True)
                pers_inputs.append({"name": f"persist-full-{k:03d}.mov", "a": a, "b": b, "enable": outside(a, b, parts),
                                    "loop": False})
            notes.append(f"Layers on screen all the time ({', '.join(x['name'] for x in kind['persistent'][:3])}) keep "
                         f"changing without repeating, so they were drawn in full ({_mins(p1 - p0)}) — a still or "
                         "looping version would render much faster.")

    # 3. a hang: az EffectCraft keveri (felvétel, bevágások, zene, csend a zárókártya alatt)
    say("mixing the sound")
    await video._run("render", "--project", project.name, "--comp", info["comp"], "--out", "fr-audio.wav", "--format", "wav",
                     "--json", cwd=work)
    has_audio = (work / "fr-audio.wav").exists() and (work / "fr-audio.wav").stat().st_size > 1024

    # 4. a felirat
    ass = None
    if subs and kind["captions"]:
        ass = work / "fr-subs.ass"
        ass.write_text(ass_subtitles(subs, style, W, H, dur, video._rgb(opts.get("accent"))), "utf-8")

    # 5. felvétel → rétegek → felirat → kódolás; hosszú videónál időszeletekben, párhuzamosan (ffmpeg 4.4-ben a sok
    #    bemenet egy szálon fűződik össze — 10-22, 30 perc: 6 mag dolgozott a 16-ból, 30 perc kódolás), a végén
    #    újrakódolás nélkül összefűzve, a hang egyben
    say("putting it together")
    items = pers_inputs + [{"name": name, "a": a, "b": b, "enable": f"between(t,{a:.4f},{b - 1e-4:.4f})", "loop": False}
                           for _, a, b, name, _ in sorted(jobs, key=lambda j: j[1])]
    base_in, pre = foot["file"], []
    if proxy and footage.stabilized(base):                      # stabilizálva: az egész felvétel teljes méretben (egyszer)
        base_in = (await footage.full(base, src, work)).name
    elif proxy:                                                 # az eredeti, ugyanazzal a lánccal, mint a munkamásolat
        base_in, pre = str(src), footage.chain(base, await mediaprobe.info(src), work)
    tl = None
    if proxy and base.get("keep") and not footage.stabilized(base):   # vágás / beszúrás: az eredetikből, forrásonként
        tl = await footage.timeline(base, src, W, H, fps, work, bt709=True)
        items = await footage.transitions(tl, work, "f", ["-c:v", "libx264", "-preset", "veryfast", "-crf",
                                                          str(encoding.intermediate_crf()), "-pix_fmt", "yuv420p"]) + items
        pre, media = [], tl.length
    else:
        path_in = work / base_in                                # (abszolút útnál maga az út)
        m_in = await mediaprobe.info(path_in)
        pre += await base_filters(path_in)
        if not draft and (m_in["display_width"], m_in["display_height"]) != (W, H):
            pre.append(f"scale={W}:{H}")                        # (lejárt eredetinél a munkamásolat, teljes méretre)
        media = float(m_in["duration"] or dur)
    if dur - media > 0.5 / fps:                                 # zárókártya: a felvétel utolsó kockája áll alatta
        pre.append(f"tpad=stop_mode=clone:stop_duration={dur - media + 1:.3f}")
    cuts = chunks(dur, fps, media=media)
    t_e = time.time()
    if len(cuts) == 1:
        ins, graph = window_graph(base_in, 0.0, dur, items, ass, (ow, oh) if draft else None, chunked=False, pre=pre, tl=tl)
        audio_map: list[str] = []
        if has_audio:
            ins += ["-i", "fr-audio.wav"]
            audio_map = ["-map", f"{ins.count('-i') - 1}:a:0", *encoding.aac()]
        await _ffmpeg(*ins, "-filter_complex", graph, "-map", "[vout]", *audio_map, "-t", f"{dur:.4f}",
                      *encoding.x264("preview" if draft else "final"), *encoding.bt709_tags(), *encoding.faststart("mp4"), str(out.resolve()), cwd=work)
    else:
        threads = str(max(2, (os.cpu_count() or 4) // len(cuts)))

        async def piece(k: int, cs: float, ce: float) -> str:
            ins, graph = window_graph(base_in, cs, ce, items, ass, (ow, oh) if draft else None, chunked=True, pre=pre, tl=tl)
            name = f"fr-piece-{k:03d}.mp4"
            await _ffmpeg(*ins, "-filter_complex", graph, "-map", "[vout]", "-an", "-t", f"{ce - cs:.4f}",
                          *encoding.x264("preview" if draft else "final"), "-threads", threads, *encoding.bt709_tags(), name, cwd=work)
            return name
        names = await asyncio.gather(*(piece(k, a, b) for k, (a, b) in enumerate(cuts)))
        (work / "fr-pieces.txt").write_text("".join(f"file '{n}'\n" for n in names), "utf-8")
        aud = ["-i", "fr-audio.wav", "-map", "0:v:0", "-map", "1:a:0", *encoding.aac()] if has_audio else ["-map", "0:v:0"]
        await _ffmpeg("-f", "concat", "-safe", "0", "-i", "fr-pieces.txt", *aud, "-c:v", "copy", "-t", f"{dur:.4f}",
                      *encoding.faststart("mp4"), str(out.resolve()), cwd=work)
        for n in names:
            (work / n).unlink(missing_ok=True)
        notes.append(f"Encoded in {len(cuts)} parts at once, joined without re-encoding.")
    e_secs = round(time.time() - t_e, 1)
    for p in work.glob("span-*.mov"):
        p.unlink(missing_ok=True)
    for pat in ("full-*.mov", "persist-*.mov", "fr-foot*.mp4", "fr-tr-*.mp4"):
        for p in work.glob(pat):
            p.unlink(missing_ok=True)
    g_len = sum(b - a for _, a, b, _, _ in jobs)
    return {"path": "fast", "seconds": round(time.time() - t_start, 1), "graphics_parts": len(jobs),
            "graphics_seconds_of_video": round(g_len, 1), "graphics_render_s": g_secs, "encode_s": e_secs,
            "notes": notes, "findings": findings, "subtitles": "libass" if ass else None, "scale": scale}


CHUNK_MIN_S = 120.0          # ennél rövidebb videó egy menetben; hosszabb: időszeletekben, párhuzamosan


def chunks(dur: float, fps: float, n: Optional[int] = None, media: Optional[float] = None) -> list[tuple[float, float]]:
    """A végső menet időszeletei, kockahatáron: rövid videónál egy; hosszúnál magonként négyenként egy (2–8). media: a
    felvétel hossza, ha a videó hosszabb (zárókártya) — a vágások a felvételen belül esnek, az utolsó szelet viszi a
    kimerevített véget (a felvétel vége utáni szeletnek nem volna bemenete)."""
    if n is None:
        n = 1 if dur < CHUNK_MIN_S else max(2, min(8, (os.cpu_count() or 4) // 4))
    span = min(dur, media) if media else dur
    cuts = [round(math.floor(span * k / n * fps + 1e-6) / fps, 4) for k in range(n)] + [dur]
    return [(a, b) for a, b in zip(cuts, cuts[1:]) if b - a > 1e-3]


def window_graph(base: str, cs: float, ce: float, items: list[dict[str, Any]], ass: Optional[Path],
                 size: Optional[tuple[int, int]], chunked: bool, pre: Optional[list[str]] = None,
                 tl: Any = None) -> tuple[list[str], str]:
    """Egy időablak [cs, ce) ffmpeg-bemenetei és szűrőgráfja. A felvétel időbélyegei abszolútak maradnak (setpts +cs), így
    a rétegek enable-kifejezése és a felirat ugyanaz, mint egy menetben; az ablak előtt induló réteg a saját közepéből
    kezd (-ss), az ismétlődő kör a fázisából; a végén a szelet 0-ról indul (setpts −cs), hogy összefűzhető legyen."""
    chain: list[str] = []
    if tl is not None:                                          # vágott / beszúrt idővonal (footage.Timeline): az ablak
        ins, cat, cur = tl.inputs(cs, ce)                       # szakaszai, forrásonként a saját láncukkal
        chain.append(cat.rstrip(";"))
    else:
        ins, cur = (["-ss", f"{cs:.4f}", "-t", f"{ce - cs:.4f}"] if chunked else []) + ["-i", base], "[0:v]"
    k = ins.count("-i")
    if pre:
        chain.append(f"{cur}{','.join(pre)}[c0]")
        cur = "[c0]"
    if chunked and cs > 0:
        chain.append(f"{cur}setpts=PTS+{cs:.4f}/TB[t0]")
        cur = "[t0]"
    if size:
        chain.append(f"{cur}scale={size[0]}:{size[1]}[b0]")
        cur = "[b0]"
    for it in items:
        if it["b"] <= cs or it["a"] >= ce:
            continue
        if it["a"] >= cs or not chunked:
            src = (["-stream_loop", "-1"] if it["loop"] else []) + ["-itsoffset", f"{it['a']:.4f}", "-i", it["name"]]
        elif it["loop"]:
            src = ["-stream_loop", "-1", "-ss", f"{(cs - it['a']) % it['period']:.4f}", "-itsoffset", f"{cs:.4f}", "-i", it["name"]]
        else:
            src = ["-ss", f"{cs - it['a']:.4f}", "-itsoffset", f"{cs:.4f}", "-i", it["name"]]
        ins += src
        chain.append(f"{cur}[{k}:v]overlay=0:0:eof_action=pass:enable='{it['enable']}'[o{k}]")
        cur, k = f"[o{k}]", k + 1
    if ass is not None:
        chain.append(f"{cur}ass={ass.name}:fontsdir={INTER_DIR}[sub]")
        cur = "[sub]"
    if chunked and cs > 0:
        chain.append(f"{cur}setpts=PTS-{cs:.4f}/TB[t1]")
        cur = "[t1]"
    chain.append(f"{cur}format=yuv420p[vout]")
    return ins, ";".join(chain)


def _mins(s: float) -> str:
    return f"{s / 60:.1f} min" if s >= 90 else f"{s:.0f} s"
