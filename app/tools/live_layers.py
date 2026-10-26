"""Élő előnézet, L2 sáv: a grafikai rétegek klipjei (plan20261026_live_preview.md).

A böngésző maga komponálja a képet (felvétel + rétegek + felirat); a grafikát a motor rajzolja, de a motor nem fut a
böngészőben — ezért minden grafikai réteg EGYSZER készül el egy rövid, átlátszó klipként, a tartalma szerint
gyorsítótárazva, és a böngésző élőben teszi a felvételre.

A klip (a böngésző ezt rajzolja, L1: out = mix(base, rgb, a)):
  · MP4, H.264 yuv420p, GOP ≤ 1 s, +faststart, BT.709, korlátozott tartomány (16–235);
  · W × 2H: fölül a réteg színe EGYENES (nem előszorzott) alfával — a motor ProRes 4444-e ilyen (mérve, 10-26: egy
    elhalványuló fehér szöveg Y-ja 230 marad, miközben az alfája 0,9 → 0,1), a Y'CbCr-t úgy, ahogy a motor írta (a
    szakasz-előnézet és a végső render is így teszi a felvételre: ugyanazt a színt kapja a böngésző); alul az alfa
    luma-ként (Y = 16 + 219·a, Cb = Cr = 128 — BT.709-cel dekódolva R = G = B = a);
  · teljes kép: a réteg pixelei a helyükön, a többi átlátszó; a klip 0. kockája a réteg első kockája (t0, kockahatár).
  · Végig látható réteg (vízjel, logó): bevezetés + egy kör egy klipben, loop = {intro, period} — a böngésző a bevezetést
    egyszer, utána a [intro, intro + period) részt ismételgeti; a kivezetés (az elhalványulás a végén) külön bejegyzés.

A tartalom-hash (a klip neve) a MOTOR PROJEKTJÉBŐL: a réteg saját kompozíciója (szöveg, értékek, akcentus, stílus, képek
— a médiafájlok TARTALMA, nem a fájl-azonosítója: minden szerkesztés új azonosítóval másolja őket —, animációk, a beágyazott
kompozíciók), a fő kompozícióbeli rétegsora a saját kezdetéhez mérve (helyzet, méret, átlátszóság, kulcsok, kapcsolók), a
kép mérete, a kockasebesség, a motor verziója, a render minősége és a kódolás. A réteg abszolút kezdete NINCS benne:
egy időben eltolt réteg ugyanazt a klipet kapja. Ami a motorban mégis az abszolút időtől függ (mérve a 30 perces
bemutató projektjén): a motor a réteg kezdetét és a fő kompozícióban rá tett kulcsokat (be- / kiúszás) a kompozíció
kockarácsára kerekíti, egymástól függetlenül — egy nem egész kockányi eltolás után egy kulcs a réteg elejéhez mérve egy
kockával odébb eshet. Ezt a hash nem követi (a kulcs ideje a builder szándékára kerekítve kerül bele: 0, be, ki, vég —
ha 1,5 kockán belül van), különben egy eltolás az esetek felében újrarajzolna; az eltérés legfeljebb egy kocka egy
be- / kiúszás idejében. A réteg kompozícióján belül minden idő a réteg saját ideje (a kifejezések `time`-ja is) —
ott nincs abszolút függés. Ami 1,5 kockánál többel eltér (kézzel átidőzített kulcs), az benne van.

Tárolás: a klipek a preview-cache gyökere alatt (<DATA_DIR>/preview-cache/_layers/<hash>.mp4), a nyilvántartás a
Postgres preview_segments táblájában („v-<hash>”, a szakasz-előnézet LRU-ja: register / touch / sweep); a végig látható
réteg köre a preview_cycles táblában (Postgres; más tárolót nem használunk).

Sor: elsőbbség a lejátszófejtől mért távolság szerint (ami a fej alatt van: 0; előtte: a távolság; mögötte: háromszoros
távolság; akire a klip-végpont vár: −1). Saját, kicsi dolgozókészlet: LIVE_LAYERS_WORKERS (alap 2) = egyszerre ennyi
motor-render; a video_preview.gate()-et NEM kérjük. Miért (mérve, 10-26, S4): a kapu sima FIFO-szemafor 2 hellyel, és egy
szerkesztés után a szakasz-előnézet (#44) háttér-feltöltése 7–9 s-os futamokkal tartja mindkét helyét — a megváltozott
réteg klipje mögéjük állna. A feltöltés nice 10-zel fut (engines.background), ezek a renderek nem: a CPU-t a réteg-klip
kapja. Kettőnél több dolgozó nem ér semmit: egy motor-render ~10 magot visz el a 16-ból, két egyidejű render együtt csak
~5 %-kal gyorsabb, mint egymás után — több csak a terhelést emelné. A dolgozók üres kontextusban indulnak (a hívó nice-a,
heavy-helye nem öröklődik), a munka tenancyje a sorba álláskor rögzül.
"""
from __future__ import annotations

import asyncio
import collections
import contextvars
import hashlib
import json
import logging
import math
import os
import re
import shutil
import time
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("aice.live_layers")

LIVE_LAYERS_VERSION = 1      # a klip formátumának / a hash szerkezetének változásakor emelni
TPS = 254_016_000_000        # az .ecproj időegysége (= video.TICKS_PER_S)
CRF, PRESET = 18, "veryfast"
QUALITY = "draft"            # = preview_cache._engine (a szakasz-előnézet és ez ugyanazt a képet adja)
PART_S = 60.0                # a kör nélküli végig látható réteg ennyi másodperces darabokban
SNAP_FRAMES = 1.5            # a fő kompozícióbeli kulcs ennyi kockán belül a builder szándéka (lásd fent)
FAIL_HOLD_S = 15.0
TOUCH_EVERY_S = 2.0
_CAPTION = re.compile(r"^Caption (box )?\d+$")
_LID = re.compile(r"^Layer (\S+)")
_HASH = re.compile(r"^[0-9a-f]{64}$")


def _env_num(name: str, default: float, lo: float) -> float:
    try:
        return max(lo, float(os.getenv(name, "") or default))
    except ValueError:
        return default


def n_workers() -> int:
    return int(_env_num("LIVE_LAYERS_WORKERS", 2, 1))


def wait_s() -> float:
    """Ennyit vár a klip-végpont egy még nem kész klipre, utána 503 + Retry-After."""
    return _env_num("LIVE_LAYERS_WAIT_S", 20.0, 0.0)


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def _fps_q(fps: float) -> str:
    q = Fraction(fps).limit_denominator(1001)
    return f"{q.numerator}/{q.denominator}"


def _pc():
    from app.tools import preview_cache
    return preview_cache


def clip_path(h: str) -> Path:
    """<DATA_DIR>/preview-cache/_layers/<hash>.mp4 (= preview_cache.layer_path)."""
    return _pc().layer_path(h)


# ---------- a projekt rétegei (a projektfájlból, motor nélkül) ----------

_PROJ: "collections.OrderedDict[str, tuple[tuple[int, int], dict[str, Any]]]" = collections.OrderedDict()
_CONTENT: dict[str, str] = {}          # file_id → a fájl tartalmának sha256-ja (a fájl-azonosító sosem kap új tartalmat)
_SCENES: "collections.OrderedDict[tuple, dict[str, Any]]" = collections.OrderedDict()
BIG_FILE = 64 * 1024 * 1024           # ennél nagyobb médiafájl (a felvétel maga): az azonosítója + mérete (nem olvassuk végig)


def _project_file(pid: str, meta: dict[str, Any]) -> tuple[Path, str]:
    """A verzió motor-projektje: a felirat nélküli előnézet-projekt (preview.ecproj), ha van és nem szerkesztették kézzel;
    különben maga a projekt (= preview_cache._build választása). → (út, név a munkamappában)"""
    from app.tools import crafts, video
    assets = video.assets_of(pid)
    if "preview.ecproj" in assets and not meta.get("hand_edited"):
        try:
            return crafts.existing(assets["preview.ecproj"]), "preview.ecproj"
        except FileNotFoundError:
            pass
    return crafts.existing(pid), "project.ecproj"


def _load(path: Path) -> tuple[tuple[int, int], dict[str, Any]]:
    st = path.stat()
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _PROJ.get(str(path))
    if hit and hit[0] == stamp:
        _PROJ.move_to_end(str(path))
        return hit
    d = json.loads(path.read_text("utf-8"))
    _PROJ[str(path)] = (stamp, d)
    while len(_PROJ) > 8:
        _PROJ.popitem(last=False)
    return stamp, d


def _content_id(name: str, assets: dict[str, str]) -> str:
    """Egy médiafájl azonossága a tartalmából (a szerkesztés új file_id-vel másolja a réteg képeit / klipjeit — mérve:
    S3 → S4 minden gfx-fájl új azonosítót kapott, bájtra ugyanazzal a tartalommal)."""
    from app.tools import crafts
    fid = assets.get(Path(str(name).replace("\\", "/")).name)
    if not fid:
        return f"name:{name}"
    if fid in _CONTENT:
        return _CONTENT[fid]
    try:
        p = crafts.existing(fid)
    except FileNotFoundError:
        return f"missing:{name}"
    size = p.stat().st_size
    if size > BIG_FILE:
        cid = f"fid:{fid}:{size}"
    else:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        cid = f"sha:{h.hexdigest()}"
    _CONTENT[fid] = cid
    return cid


_DROP = {"id", "uid", "label", "comment"}


class _Canon:
    """Egy projekt elemeinek (kompozíció, felvétel) kanonikus alakja: azonosítók nélkül, a hivatkozott elem helyén
    annak hash-e, a médiafájl helyén a tartalma; a rétegek szülője (parent) a kompozíción belüli sorszámával."""

    def __init__(self, items: dict[str, Any], assets: dict[str, str]):
        self.items, self.assets, self.memo = items, assets, {}

    def item(self, iid: Any) -> str:
        key = str(iid)
        if key in self.memo:
            return self.memo[key]
        self.memo[key] = "cycle"                              # (önhivatkozás ellen)
        it = self.items.get(key)
        if it is None:
            out = f"missing:{key}"
        else:
            kind = dict(it.get("kind") or {})
            if kind.get("type") == "Footage":
                kind.pop("missing", None)
                if kind.get("path"):
                    kind["path"] = _content_id(kind["path"], self.assets)
            out = _sha(self.value(kind))
        self.memo[key] = out
        return out

    def value(self, o: Any) -> Any:
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if k in _DROP:
                    continue
                if k == "layers" and isinstance(v, list):
                    out[k] = self.layers(v)
                elif k == "source" and isinstance(v, dict) and "item" in v:
                    out[k] = {**{kk: vv for kk, vv in v.items() if kk != "item"}, "item": self.item(v["item"])}
                else:
                    out[k] = self.value(v)
            return out
        if isinstance(o, list):
            return [self.value(v) for v in o]
        return o

    def layers(self, ls: list[Any]) -> list[Any]:
        pos = {x.get("id"): i for i, x in enumerate(ls) if isinstance(x, dict)}
        out = []
        for x in ls:
            y = self.value(x)
            if isinstance(x, dict) and x.get("parent") is not None:
                y["parent"] = pos.get(x["parent"], "outside")
            out.append(y)
        return out


def _expected(N: int, fps: float) -> list[float]:
    """A builder (video_builder.jsx overlayLayers) kulcsai a réteg saját idejében, kockában: 0, a beúszás vége, a
    kiúszás eleje, a vég (be: min(0,45 s, dur/4); ki: min(0,35 s, dur/4))."""
    d = N / fps
    return [0.0, min(0.45, d / 4) * fps, N - min(0.35, d / 4) * fps, float(N)]


def _snap_keys(o: Any, exp: list[float], fps: float) -> Any:
    """A fő kompozícióbeli rétegsor kulcsainak ideje: a builder szándéka szerint (1,5 kockán belül), különben kockában."""
    if isinstance(o, dict):
        out = {k: _snap_keys(v, exp, fps) for k, v in o.items()}
        if isinstance(o.get("keys"), list):
            ks = []
            for kf in out["keys"]:
                if isinstance(kf, dict) and isinstance(kf.get("time"), (int, float)):
                    f = float(kf["time"]) / TPS * fps
                    j = min(range(len(exp)), key=lambda i: abs(exp[i] - f))
                    kf = {**kf, "time": f"e{j}" if abs(exp[j] - f) <= SNAP_FRAMES else round(f)}
                ks.append(kf)
            out["keys"] = ks
        return out
    if isinstance(o, list):
        return [_snap_keys(v, exp, fps) for v in o]
    return o


def _late_key(canon: _Canon, item: Any, fps: float, offset: float = 0.0, depth: int = 0) -> float:
    """A réteg kompozíciójának (és a beágyazottakénak) legkésőbbi kulcsa a réteg saját idejében, kockában — a kör csak
    ennél hosszabb mintából érvényes."""
    it = canon.items.get(str(item)) or {}
    kind = it.get("kind") or {}
    best = 0.0
    if depth > 8:
        return best

    def keys(o: Any) -> None:
        nonlocal best
        if isinstance(o, dict):
            for kf in o.get("keys") or [] if isinstance(o.get("keys"), list) else []:
                if isinstance(kf, dict) and isinstance(kf.get("time"), (int, float)):
                    best = max(best, offset + float(kf["time"]) / TPS * fps)
            for k, v in o.items():
                if k != "keys":
                    keys(v)
        elif isinstance(o, list):
            for v in o:
                keys(v)

    for L in kind.get("layers") or []:
        st = offset + float(L.get("start_time") or 0) / TPS * fps
        keys(L.get("props"))
        src = L.get("source") or {}
        if src.get("type") == "Comp" and "item" in src:
            best = max(best, _late_key(canon, src["item"], fps, st, depth + 1))
    return best


def _frames(ticks: Any, fps: float) -> int:
    return round(float(ticks or 0) / TPS * fps)


def scene(pid: str) -> dict[str, Any]:
    """A verzió grafikai rétegei a motor projektjéből (I/O: a meta, a projektfájl és a médiafájlok olvasása; motor
    nélkül), a projektfájl idejéhez kötve gyorsítótárban → scene_from(…) + {pid, project, project_name, stamp}."""
    from app.storage import read_meta
    from app.tools import footage, video
    meta = read_meta(pid) or {}
    spec = meta.get("spec") or {}
    base = spec.get("base") or {}
    if spec.get("kind") != "overlay" or not base:
        raise LookupError("the live preview's graphics are for videos on your own footage (overlay projects)")
    path, pname = _project_file(pid, meta)
    stamp, d = _load(path)
    key = (pid, str(path), stamp, bool(meta.get("hand_edited")), json.dumps(base.get("proxy"), sort_keys=True))
    if key in _SCENES:
        _SCENES.move_to_end(key)
        return _SCENES[key]
    W, H = int(base.get("width") or 0), int(base.get("height") or 0)
    proxy = (int(base["proxy"]["w"]), int(base["proxy"]["h"])) if base.get("proxy") else footage.size(W, H) if W and H else None
    sc = scene_from(d, proxy=proxy, assets=video.assets_of(pid))
    sc.update(pid=pid, project=str(path), project_name=pname, stamp=stamp)
    _SCENES[key] = sc
    while len(_SCENES) > 16:
        _SCENES.popitem(last=False)
    return sc


def _main_comp_of(d: dict[str, Any]) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """A fő kompozíció (= video._main_comp: „Video”, különben a legtöbb rétegű / leghosszabb) → (elem-azonosító, elem)."""
    comps = [(k, it) for k, it in (d.get("items") or {}).items() if (it.get("kind") or {}).get("type") == "Comp"]
    hit = [c for c in comps if c[1].get("name") == "Video"]
    if hit:
        return hit[0]
    if not comps:
        return None, None
    return max(comps, key=lambda c: (len(c[1]["kind"].get("layers") or []), c[1]["kind"].get("duration") or 0))


def scene_from(d: dict[str, Any], *, proxy: Optional[tuple[int, int]], assets: dict[str, str]) -> dict[str, Any]:
    """Egy motor-projekt (.ecproj JSON) grafikai rétegei → {fps, fps_q, W, H, pw, ph, scale, frames, comp, main_id,
    n_main, layers}; a rétegek a fő kompozíció sorrendjében, alulról fölfelé (z = 0 a legalsó). Réteg: {id, name, i (a
    motor sorszáma, 1 = legfelső), z, Fa, Fb, N, hash, persist, unsupported, late}. assets: a projekt médiafájljai (név →
    file_id) — a képek / klipek tartalma kerül a hash-be. proxy: a munkamásolat mérete (ebben a méretben rajzol a motor)."""
    from app.tools import fast_render
    items = d.get("items") or {}
    main_id, main = _main_comp_of(d)
    if main is None:
        raise LookupError("the project has no main composition")
    comp_name = main.get("name")
    mk = main["kind"]
    W, H = int(mk["width"]), int(mk["height"])
    fr = mk.get("frame_rate") or {}
    fps = float(fr.get("num", 30)) / float(fr.get("den", 1) or 1)
    frames = _frames(mk.get("duration"), fps)
    pw, ph = proxy or (W, H)
    canon = _Canon(items, assets)
    glob = {"v": LIVE_LAYERS_VERSION, "engine": _pc().engine_version(), "settings": d.get("settings"),
            "main": canon.value({k: v for k, v in mk.items() if k not in ("layers", "duration", "work_area", "markers",
                                                                          "poster_time")}),
            "size": [pw, ph], "quality": QUALITY, "enc": [CRF, PRESET, "stacked-alpha-709-tv"]}
    gh = _sha(glob)
    L = mk.get("layers") or []
    out: list[dict[str, Any]] = []
    for pos, x in enumerate(L):
        sw = x.get("switches") or {}
        src = x.get("source") or {}
        name = str(x.get("name") or "")
        if not sw.get("video", True) or sw.get("guide"):
            continue
        if src.get("type") == "Footage" and name == "Footage":          # a felvétel (L1 rajzolja)
            continue
        if _CAPTION.match(name):                                          # a felirat (L3 rajzolja)
            continue
        if src.get("type") in ("Null",) or x.get("null"):
            continue
        why = None
        if sw.get("adjustment"):
            why = "an adjustment layer (it changes the picture under it)"
        elif str(x.get("blend_mode") or "Normal") != "Normal":
            why = f"a blend mode ({x.get('blend_mode')})"
        elif x.get("track_matte"):
            why = "a track matte"
        elif x.get("preserve_transparency"):
            why = "preserve transparency (it depends on the layers under it)"
        elif x.get("parent") is not None:
            why = "a parent layer"
        st = float(x.get("start_time") or 0)
        Fa = max(0, _frames(x.get("in_point"), fps))
        Fb = min(frames, _frames(x.get("out_point"), fps))
        if Fb <= Fa:
            continue
        N = Fb - Fa
        rel = canon.value({k: v for k, v in x.items() if k not in ("name", "start_time", "in_point", "out_point", "markers")})
        rel["in"] = _frames(float(x.get("in_point") or 0) - st, fps)
        rel["out"] = _frames(float(x.get("out_point") or 0) - st, fps)
        sf = _frames(st, fps)
        rel["vis"] = [Fa - sf, Fb - sf]                                   # a videó széle levágja (0 előtt / a vége után)
        rel = _snap_keys(rel, _expected(N, fps), fps)
        m = _LID.match(name)
        a, b = Fa / fps, Fb / fps
        late = _late_key(canon, src["item"], fps) if src.get("type") == "Comp" and "item" in src else 0.0
        late = max([late] + [float(k["time"]) / TPS * fps for k in _keys_of(x.get("props"))
                             if float(k["time"]) / TPS * fps < N - fast_render.OUTRO_S * fps - 1])
        out.append({"id": m.group(1) if m else name, "name": name, "i": pos + 1, "pos": pos, "Fa": Fa, "Fb": Fb, "N": N,
                    "hash": _sha({"g": gh, "layer": rel}), "unsupported": why, "late": late,
                    "persist": min(frames / fps, b) - max(0.0, a) >= fast_render.PERSISTENT_S})
    out.reverse()                                                          # alulról fölfelé
    for z, e in enumerate(out):
        e["z"] = z
    return {"fps": fps, "fps_q": _fps_q(fps), "W": W, "H": H, "pw": pw, "ph": ph, "scale": pw / W, "frames": frames,
            "comp": comp_name, "main_id": main_id, "layers": out, "n_main": len(L)}


def _keys_of(o: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(o, dict):
        if isinstance(o.get("keys"), list):
            out += [k for k in o["keys"] if isinstance(k, dict) and isinstance(k.get("time"), (int, float))]
        for k, v in o.items():
            if k != "keys":
                out += _keys_of(v)
    elif isinstance(o, list):
        for v in o:
            out += _keys_of(v)
    return out


# ---------- a bejegyzések (a scene JSON `layers` része) ----------

def _outro_frames(fps: float) -> int:
    from app.tools import fast_render
    return max(1, round(fast_render.OUTRO_S * fps))


def _probe_frames(fps: float) -> int:
    from app.tools import fast_render
    return max(2, round(fast_render.PROBE_S * fps))


def entries(sc: dict[str, Any], cycles: dict[str, Optional[dict[str, int]]]) -> list[dict[str, Any]]:
    """A scene rétegei → a klipek (belső alak): {id, name, z, f0, f1, hash, kind (clip | loop | outro | part), a, b (a
    réteg saját kockái), layer (a scene-réteg), loop}. cycles: {loop-hash: {intro, period} | None (még nem mért)}."""
    fps = sc["fps"]
    out: list[dict[str, Any]] = []
    for e in sc["layers"]:
        base = {"id": e["id"], "name": e["name"], "z": e["z"], "layer": e}
        if not e["persist"]:
            out.append({**base, "kind": "clip", "hash": e["hash"], "a": 0, "b": e["N"], "loop": None})
            continue
        N, lb = e["N"], max(1, e["N"] - _outro_frames(fps))
        hl = _sha({"of": e["hash"], "kind": "loop", "probe": _probe_frames(fps)})
        cyc = cycles.get(hl)
        if cyc is not None and int(cyc.get("period") or 0) == 0:            # nem ismétlődik: darabokban, végig
            step = max(1, round(PART_S * fps))
            for a in range(0, lb, step):
                b = min(lb, a + step)
                out.append({**base, "kind": "part", "hash": _sha({"of": e["hash"], "kind": "part", "a": a, "b": b}), "a": a,
                            "b": b, "loop": None})
        else:
            loop = None
            if cyc is not None:
                i, P = int(cyc["intro"]), int(cyc["period"])
                loop = {"intro": round(i / fps, 6), "period": round(P / fps, 6), "intro_frames": i, "period_frames": P,
                        "still": P == 1}
            out.append({**base, "kind": "loop", "hash": hl, "a": 0, "b": lb, "loop": loop, "probe": min(lb, _probe_frames(fps))})
        out.append({**base, "kind": "outro", "hash": _sha({"of": e["hash"], "kind": "outro", "a": lb, "b": N}), "a": lb, "b": N,
                    "loop": None})
    for x in out:
        x["f0"], x["f1"] = x["layer"]["Fa"] + x["a"], x["layer"]["Fa"] + x["b"]
    return out


def _sig_key(h: str) -> str:
    return "L:" + h


def clip_url(pid: str, h: str) -> str:
    """A klip aláírt címe. A lejárat órára kerekítve: egy órán belül ugyanaz a cím (a böngésző gyorsítótára)."""
    from app.config import settings
    from app.tools import preview_serve as ps
    uid = ps._workspace(pid)
    exp = (int(time.time()) // 3600 + 1) * 3600 + settings.FILE_TTL_HOURS * 3600
    return f"{settings.PUBLIC_BASE_URL.rstrip('/')}/api/video/live/layer/{h}.mp4{ps._query(pid, _sig_key(h), exp, uid)}"


def verify(pid: str, h: str, token: str, uid: str) -> bool:
    from app.tools import preview_serve as ps
    return ps.verify(pid, _sig_key(h), token, uid)


def _prio(x: dict[str, Any], playhead: float, fps: float) -> float:
    t0, t1 = x["f0"] / fps, x["f1"] / fps
    if t0 <= playhead < t1:
        return 0.0
    return t0 - playhead if t0 >= playhead else 3.0 * (playhead - t1)


async def _cycles_of(sc: dict[str, Any]) -> dict[str, Optional[dict[str, int]]]:
    out: dict[str, Optional[dict[str, int]]] = {}
    for x in entries(sc, {}):
        if x["kind"] == "loop":
            out[x["hash"]] = await _pc()._cycle_get(x["hash"])
    return out


async def layers_part(pid: str, spec: Optional[dict[str, Any]] = None, *, playhead: Optional[float] = None,
                      enqueue: bool = True) -> list[dict[str, Any]]:
    """A scene JSON `layers` része: [{id, name, z, t0, t1, hash, url|null, status, loop|null, f0, frames, kind}] a
    rajzolás sorrendjében (z növekvő: a nagyobb van fölül). status: ready | queued | rendering | failed | missing |
    unsupported; url csak a kész klipé (aláírt). enqueue: a hiányzók sorba állnak, a lejátszófej (playhead, s) szerinti
    elsőbbséggel; playhead=None: a már sorban állók elsőbbsége marad, az újak a videó elejétől mért távolsággal.
    spec: nem kell (a verzió metájából olvassuk; a hívó átadhatja, ha már nála van)."""
    sc = await asyncio.to_thread(scene, pid)
    cycles = await _cycles_of(sc)
    xs = entries(sc, cycles)
    st = _state()
    fps = sc["fps"]
    out = []
    for x in xs:
        h = x["hash"]
        job = st.jobs.get(h)
        if x["layer"]["unsupported"]:
            status = "unsupported"
        elif clip_path(h).exists():
            status = "ready"
        elif job is not None and not job.fut.done():
            status = "rendering" if job.started else "queued"
        elif _held(st, h):
            status = "failed"
        else:
            status = "missing"
        if enqueue and status in ("missing", "queued"):
            pr = _prio(x, playhead if playhead is not None else 0.0, fps)
            if status == "missing":
                _enqueue(st, pid, sc, x, pr)
                status = "queued"
            elif playhead is not None:
                _bump(st, job, pr, force=True)
        y = {"id": x["id"], "name": x["name"], "z": x["z"], "t0": round(x["f0"] / fps, 6), "t1": round(x["f1"] / fps, 6),
             "hash": h, "url": clip_url(pid, h) if status == "ready" else None, "status": status, "loop": x["loop"],
             "f0": x["f0"], "frames": x["f1"] - x["f0"], "kind": x["kind"]}
        if x["kind"] == "loop" and x["loop"]:
            y["clip_frames"] = x["loop"]["intro_frames"] + x["loop"]["period_frames"]
        if status == "unsupported":
            y["reason"] = x["layer"]["unsupported"]
        if status == "failed":
            y["error"] = _held(st, h)
        out.append(y)
    return out


# ---------- a render ----------

_CTX: "collections.OrderedDict[tuple, dict[str, Any]]" = collections.OrderedDict()
_CTX_MAX = 6
_LOCKS: dict[str, tuple[Any, asyncio.Lock]] = {}


def _lock(key: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    hit = _LOCKS.get(key)
    if hit is None or hit[0] is not loop:
        hit = (loop, asyncio.Lock())
        _LOCKS[key] = hit
    return hit[1]


async def _context(pid: str, sc: dict[str, Any]) -> dict[str, Any]:
    """A verzió munkamappája (a projekt + a média linkként), a projektfájl idejéhez kötve; a motort itt nem hívjuk."""
    from app.tools import video
    key = (pid, sc["project"], sc["stamp"])
    hit = _CTX.get(key)
    if hit is not None and (hit["work"] / hit["project"]).exists():
        _CTX.move_to_end(key)
        return hit
    async with _lock("ctx:" + pid):
        hit = _CTX.get(key)
        if hit is not None and (hit["work"] / hit["project"]).exists():
            return hit
        work = _pc().cache_root() / "_work" / f"live-{Path(pid).stem[:16]}-{_sha(key)[:8]}"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        if sc["project_name"] == "preview.ecproj":
            project = await asyncio.to_thread(_pc()._stage_file, Path(sc["project"]), video.assets_of(pid), work, "preview.ecproj")
        else:
            project = await asyncio.to_thread(video.stage, pid, work)
        ctx = {"pid": pid, "work": work, "project": project.name, "comp": sc["comp"], "fps": sc["fps"], "fps_q": sc["fps_q"],
               "scale": sc["scale"], "pw": sc["pw"], "ph": sc["ph"]}
        _CTX[key] = ctx
        while len(_CTX) > _CTX_MAX:
            _, old = _CTX.popitem(last=False)
            shutil.rmtree(old["work"], ignore_errors=True)
        return ctx


async def _solo(ctx: dict[str, Any], sc: dict[str, Any], e: dict[str, Any], tag: str) -> str:
    """A projekt egy változata: csak ez a réteg látszik (a felvétel, a felirat, a többi réteg ki)."""
    from app.tools import fast_render
    off = [k for k in range(1, sc["n_main"] + 1) if k != e["i"]]
    return await fast_render.variant(ctx["work"] / ctx["project"], {"comp": ctx["comp"]}, off, tag)


def stack_args(src: str, dst: str, fps_q: str, first: int = 0, count: Optional[int] = None) -> list[str]:
    """ffmpeg-argumentumok: átlátszó (egyenes alfájú) ProRes 4444 → a böngésző klipje (W × 2H, fölül a Y'CbCr ahogy van,
    alul az alfa luma-ként 16–235 között, Cb = Cr = 128), H.264 yuv420p BT.709 tv, GOP ≤ 1 s, +faststart."""
    fps = float(Fraction(fps_q))
    trim = f"trim=start_frame={first}" + (f":end_frame={first + count}" if count is not None else "") + ",setpts=PTS-STARTPTS,"
    graph = (f"[0:v]{trim}scale=flags=accurate_rnd+full_chroma_int:sws_dither=none,format=yuva444p,split=2[c][a];"
             "[c]format=yuv444p[top];"
             "[a]extractplanes=a,lut=c0='16+val*219/255',split=3[y][u0][v0];[u0]lut=c0=128[u];[v0]lut=c0=128[v];"
             "[y][u][v]mergeplanes=0x001020:yuv444p[bot];"
             "[top][bot]vstack,scale=flags=accurate_rnd:sws_dither=none,format=yuv420p[out]")
    gop = max(1, math.floor(fps))
    return ["-i", src, "-filter_complex", graph, "-map", "[out]", "-an", "-r", fps_q,
            "-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF), "-pix_fmt", "yuv420p", "-g", str(gop),
            "-keyint_min", "1", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-color_range", "tv", "-video_track_timescale", fps_q.split("/")[0], "-movflags", "+faststart", "-f", "mp4", dst]


async def _ffmpeg(*args: str, cwd: Path) -> bytes:
    from app.config import settings
    from app.convert.engines import run
    out, _ = await run("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args, cwd=cwd, timeout=settings.VIDEO_TIMEOUT_S)
    return out


async def _publish(ctx: dict[str, Any], mov: str, h: str, first: int = 0, count: Optional[int] = None) -> Path:
    dst = clip_path(h)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{h}.tmp-{uuid.uuid4().hex[:6]}.mp4")
    try:
        await _ffmpeg(*stack_args(mov, str(tmp), ctx["fps_q"], first, count), cwd=ctx["work"])
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    await _pc().register("v-" + h, "_layers", dst.stat().st_size)
    return dst


async def render(pid: str, sc: dict[str, Any], x: dict[str, Any]) -> dict[str, Any]:
    """Egy bejegyzés (entries) klipje: a réteg egyedül, a motorból [f0, f1) — a kör (loop) mintából: bevezetés + egy
    kör, a kör a preview_cycles-ba. A dolgozó hívja (egyszerre LIVE_LAYERS_WORKERS). → mérés {engine_s, encode_s, …}"""
    from app.tools import fast_render
    e, h = x["layer"], x["hash"]
    ctx = await _context(pid, sc)
    tag = f"ll-{e['i']}-{uuid.uuid4().hex[:6]}"
    tmp = [f"{tag}.ecproj", f"fr-{tag}.jsx", f"{tag}.mov"]
    stat: dict[str, Any] = {"hash": h, "id": x["id"], "kind": x["kind"]}
    try:
        t0 = time.monotonic()
        proj = await _solo(ctx, sc, e, tag)
        t1 = time.monotonic()
        if x["kind"] == "loop":
            n = x["probe"]
            if e["late"] >= n - 1:                       # a mintán túl is van kulcs: a kör nem bizonyítható
                await _pc()._cycle_put(h, {"intro": 0, "period": 0})
                stat.update(cycle=None, why="keys after the probe")
                return stat
            await _pc()._engine(ctx, proj, x["f0"], x["f0"] + n, f"{tag}.mov", alpha=True)
            t2 = time.monotonic()
            cyc = fast_render.cycle(await fast_render._md5s(ctx["work"] / f"{tag}.mov"))
            if not cyc or x["b"] <= cyc[0] + cyc[1]:
                await _pc()._cycle_put(h, {"intro": 0, "period": 0})
                stat.update(cycle=None, engine_s=round(t2 - t1, 3))
                return stat
            await _publish(ctx, f"{tag}.mov", h, 0, cyc[0] + cyc[1])
            await _pc()._cycle_put(h, {"intro": int(cyc[0]), "period": int(cyc[1])})
            stat.update(cycle={"intro": cyc[0], "period": cyc[1]}, frames=cyc[0] + cyc[1])
        else:
            await _pc()._engine(ctx, proj, x["f0"], x["f1"], f"{tag}.mov", alpha=True)
            t2 = time.monotonic()
            await _publish(ctx, f"{tag}.mov", h)
            stat["frames"] = x["f1"] - x["f0"]
        t3 = time.monotonic()
        stat.update(variant_s=round(t1 - t0, 3), engine_s=round(t2 - t1, 3), encode_s=round(t3 - t2, 3),
                    bytes=clip_path(h).stat().st_size)
        return stat
    finally:
        for n in tmp:
            (ctx["work"] / n).unlink(missing_ok=True)


# ---------- a sor (eseményhurkonként) ----------

class _Job:
    __slots__ = ("key", "pid", "sc", "x", "prio", "seq", "uid", "shared", "fut", "started", "t_enq")

    def __init__(self, st: "_State", pid: str, sc: dict[str, Any], x: dict[str, Any], prio: float):
        from app import tenancy
        self.key, self.pid, self.sc, self.x, self.prio, self.seq = x["hash"], pid, sc, x, prio, next(st.seq)
        self.uid, self.shared = tenancy.current_user_id(), tenancy.shared()
        self.fut: asyncio.Future = st.loop.create_future()
        self.started = False
        self.t_enq = time.monotonic()


class _State:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        import itertools
        self.loop = loop
        self.seq = itertools.count()
        self.jobs: dict[str, _Job] = {}
        self.failed: dict[str, tuple[float, str]] = {}
        self.workers: list[asyncio.Task] = []
        self.wake = asyncio.Event()
        self.touched: set[str] = set()
        self.touch_task: Optional[asyncio.Task] = None
        self.stats: dict[str, Any] = {"renders": 0, "failed": 0, "engine_s": 0.0, "encode_s": 0.0, "last": []}


_S: Optional[_State] = None


def _state() -> _State:
    global _S
    loop = asyncio.get_running_loop()
    if _S is None or _S.loop is not loop:
        _S = _State(loop)
    return _S


def _reset() -> None:
    """Tesztekhez: a sor és a gyorsítótárak eldobása."""
    global _S
    st, _S = _S, None
    _SCENES.clear()
    _CTX.clear()
    if st is not None and not st.loop.is_closed():
        for t in st.workers:
            try:
                st.loop.call_soon_threadsafe(t.cancel)
            except RuntimeError:
                pass


def stats() -> dict[str, Any]:
    st = _S
    if st is None:
        return {"queued": 0, "running": 0}
    q = [j for j in st.jobs.values() if not j.started and not j.fut.done()]
    r = [j for j in st.jobs.values() if j.started and not j.fut.done()]
    return {**{k: v for k, v in st.stats.items() if k != "last"}, "queued": len(q), "running": len(r),
            "workers": n_workers(), "last": st.stats["last"][-40:]}


def _held(st: _State, h: str) -> Optional[str]:
    bad = st.failed.get(h)
    if bad and time.monotonic() - bad[0] < FAIL_HOLD_S:
        return bad[1]
    return None


def _ensure_workers(st: _State) -> None:
    st.workers = [w for w in st.workers if not w.done()]
    while len(st.workers) < n_workers():
        st.workers.append(contextvars.Context().run(st.loop.create_task, _worker(st)))


def _enqueue(st: _State, pid: str, sc: dict[str, Any], x: dict[str, Any], prio: float) -> _Job:
    job = st.jobs.get(x["hash"])
    if job is not None and not job.fut.done():
        _bump(st, job, prio)
        return job
    job = _Job(st, pid, sc, x, prio)
    st.jobs[x["hash"]] = job
    st.wake.set()
    _ensure_workers(st)
    return job


def _bump(st: _State, job: Optional[_Job], prio: float, force: bool = False) -> None:
    """Új elsőbbség egy még el nem kezdett munkának (force: a lejátszófej mozgott — hátrébb is kerülhet)."""
    if job is None or job.started or job.fut.done():
        return
    if force or prio < job.prio:
        job.prio = prio
        st.wake.set()


def _next(st: _State) -> Optional[_Job]:
    q = [j for j in st.jobs.values() if not j.started and not j.fut.done()]
    return min(q, key=lambda j: (j.prio, j.seq)) if q else None


async def _worker(st: _State) -> None:
    from app import tenancy
    while True:
        job = _next(st)
        if job is None:
            st.wake.clear()
            await st.wake.wait()
            continue
        job.started = True
        t0 = time.monotonic()
        t_user = tenancy.set_user(job.uid)
        t_sh = tenancy.enter_shared(job.shared)
        try:
            res = await render(job.pid, job.sc, job.x)
            res.update(wall_s=round(time.monotonic() - t0, 3), waited_s=round(t0 - job.t_enq, 3), prio=round(job.prio, 2),
                       load=round(os.getloadavg()[0], 2))
            st.stats["renders"] += 1
            st.stats["engine_s"] = round(st.stats["engine_s"] + float(res.get("engine_s") or 0), 3)
            st.stats["encode_s"] = round(st.stats["encode_s"] + float(res.get("encode_s") or 0), 3)
            st.stats["last"].append(res)
            del st.stats["last"][:-200]
            log.info("élő réteg-klip %s %s %s: %.2f s (motor %.2f, kódolás %.2f, elsőbbség %.1f)", job.pid, job.x["id"],
                     job.x["kind"], res["wall_s"], res.get("engine_s") or 0, res.get("encode_s") or 0, job.prio)
            st.failed.pop(job.key, None)
            if not job.fut.done():
                job.fut.set_result(res)
            if job.x["kind"] == "loop" and not res.get("cycle"):
                await _follow_parts(st, job)
        except asyncio.CancelledError:
            if not job.fut.done():
                job.fut.cancel()
            raise
        except Exception as e:
            st.stats["failed"] += 1
            log.warning("élő réteg-klip %s %s elakadt: %s", job.pid, job.x["id"], e)
            st.failed[job.key] = (time.monotonic(), str(e)[:300])
            if not job.fut.done():
                job.fut.set_exception(e)
                job.fut.exception()
        finally:
            tenancy.leave_shared(t_sh)
            tenancy.reset(t_user)
            if st.jobs.get(job.key) is job:
                del st.jobs[job.key]


async def _follow_parts(st: _State, job: _Job) -> None:
    """A végig látható réteg nem ismétlődik: a darabjai sorba (ugyanazzal az elsőbbséggel)."""
    try:
        for x in entries(job.sc, await _cycles_of(job.sc)):
            if x["kind"] == "part" and x["layer"]["hash"] == job.x["layer"]["hash"] and not clip_path(x["hash"]).exists():
                _enqueue(st, job.pid, job.sc, x, job.prio)
    except Exception as e:
        log.warning("élő réteg-klip darabjai (%s): %s", job.x["id"], e)


async def clip(pid: str, h: str, *, timeout: Optional[float] = None) -> Path:
    """A klip fájlja: kész → azonnal; különben (a verzió bejegyzései közt van) elsőbbséggel (−1) sorba, és legfeljebb
    wait_s()-ig vár. LookupError: a hash nincs a verzióban; TimeoutError: még készül; RuntimeError: elakadt."""
    p = clip_path(h)
    if p.exists():
        _touch(h)
        return p
    st = _state()
    job = st.jobs.get(h)
    if job is None or job.fut.done():
        bad = _held(st, h)
        if bad:
            raise RuntimeError(bad)
        sc = await asyncio.to_thread(scene, pid)
        x = next((x for x in entries(sc, await _cycles_of(sc)) if x["hash"] == h), None)
        if x is None or x["layer"]["unsupported"]:
            raise LookupError("that layer clip isn't in this version (the project changed — reload the scene)")
        job = _enqueue(st, pid, sc, x, -1.0)
    else:
        _bump(st, job, -1.0)
    try:
        await asyncio.wait_for(asyncio.shield(job.fut), wait_s() if timeout is None else timeout)
    except asyncio.TimeoutError:
        raise TimeoutError("the layer clip is still rendering") from None
    if not p.exists():
        raise TimeoutError("the layer clip is still rendering")           # (a kör: a darabjai készülnek)
    return p


async def prefetch(pid: str, playhead: float) -> list[dict[str, Any]]:
    """A lejátszófej új helye: a hiányzó klipek sorba / átrendezve a fejtől mért távolság szerint → a bejegyzések."""
    return await layers_part(pid, playhead=float(playhead))


def _touch(h: str) -> None:
    """Használat (LRU, preview_segments.last_used_at) kötegben."""
    try:
        st = _state()
    except RuntimeError:
        return
    st.touched.add("v-" + h)
    if st.touch_task is None or st.touch_task.done():
        async def flush():
            await asyncio.sleep(TOUCH_EVERY_S)
            todo, st.touched = sorted(st.touched), set()
            try:
                await _pc().touch(todo, "_layers")
            except Exception as e:
                log.debug("élő réteg-klip touch: %s", e)
        st.touch_task = st.loop.create_task(flush())
