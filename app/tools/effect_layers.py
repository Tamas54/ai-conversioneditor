"""Effekt-rétegek a saját felvételen (overlay): {"type": "effect", "effect": "lightning" | "fireball" | …} — a család
regisztere és az új effekt receptje: app/tools/fx/__init__.py; elsőként a villám a kézből (app/tools/fx/lightning.py, a
10-25-i próba, amit a Kommandant hangolt), másodikként a tűzgolyó (fx/fireball.py).

Réteg: {type: "effect", effect, at | after_said: "kifejezés" | at_speech_end_after: t, dur?, on?: hand | body | [x, y] |
[x, y, w, h], seed?, + az effekt saját mezői (PARAMS — a villámé: target?: arm | right | left | up | down | [x, y],
strands?: 1–16 (6), intensity?: 0.3–2 (1))}.
  · at: mikor indul (mp / időkód); after_said: amikor a kifejezés elhangzott — az átirat szóideje adja a kifejezés végét,
    onnan a hang energiája (10 ms-os felbontás) a mondat végét; at_speech_end_after: t: a t utáni első mondatvég (csak a
    hang energiájából). Mindkettő a kérés idején fordul at-ra (a réteg a feloldott időt tárolja, + trigger: honnan jött).
  · dur: meddig szól (alap 4 s); utána a jelenet színezése még ~1 s alatt lecseng (az effekt Grade-jének lecsengése, fx.common.tail_frames) — a klip ezt is
    tartalmazza.

Hogyan kerül a képre — ELŐRE KOMPONÁLVA. A villám nem sima alfás réteg (screen keverés, a jelenet sötétítése és
kékítése a felvétel fényességéből): a szerver a verzió felvételéből (az eredetiből, a verzió láncával — vágás, felállítás,
HDR → SDR, színkorrekció —, teljes méretben, ugyanazokkal a pixelekkel, amiket a végső render is kap) EGYSZER megrajzolja
a szakaszt a villámmal együtt, és ez a klip a projektben egy átlátszatlan felvétel-réteg a felvétel FÖLÖTT, a
grafikai rétegek alatt („Layer L3 – lightning”), a szakasz idejére. A felvétel mozgását (ráközelítés, osztott kép)
ugyanúgy kapja, mint a felvétel. Így minden út változtatás nélkül viszi:
  · a végső render: a gyors út (fast_render) grafikás szakaszként rajzolja (normál keverés), a régi út a motorban;
  · az élő előnézet: a réteg-klip (live_layers) a motorból, mint bármelyik réteg — a böngésző out = mix(base, rgb, 1);
  · a szakasz-előnézet (#44) és a böngészős szerkesztő is.
A klip a tartalma szerint gyorsítótárazva (<DATA_DIR>/preview-cache/_effects/<kulcs>.mp4, a preview_segments LRU-ja,
„e-<kulcs>”), és a verzió saját fájljaként is (asset) — egy szerkesztés, ami nem érinti, nem rajzolja újra.

A követés (kéz + póz) a dekódolt teljes méretű kockák ≤ 1280-as kicsinyítésén fut (a hálók 192–256 px-en dolgoznak, a
nagyobb kép nem pontosít; a próba is ekkorán futott), a pontjai Postgresben (ocr_cache: a közös tartós gyorsítótár) — a
háló nem bájtra determinisztikus, ezért egy felvétel-szakaszt egyszer követünk, és a rajz innen már determinisztikus.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("aice.effect_layers")

LEAD_S = 0.5                 # a követés ennyivel a villám előtt indul (a simítás és a háló követése bemelegszik)
HOP_S = 0.01                 # a mondatvég felbontása (10 ms)
PAUSE_S = 0.6                # ennél hosszabb csend: vége a mondatnak (a próba is 0,6 s-on vonta össze a beszédet)
SEARCH_S = 4.0               # a kifejezés vége után legfeljebb ennyit keresünk mondatvéget
EXAMPLE = ('{"type": "effect", "effect": "lightning", "after_said": "now you will die", "dur": 4, "target": "right"}')


def effects() -> tuple[str, ...]:
    from app.tools import fx
    return fx.names()


def fields() -> tuple[str, ...]:
    """Minden effekt-mező (a réteg mezőinek ellenőrzéséhez: a közösek + az összes effekt PARAMS-a)."""
    from app.tools import fx
    out: list[str] = []
    for n in fx.names():
        out += [f for f in fx.fields(n) if f not in out]
    return tuple(out)


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     default=str).encode()).hexdigest()


# ---------- a réteg alakja ----------

def normalize(r: dict[str, Any], what: str) -> dict[str, Any]:
    """A réteg effekt-mezői tisztítva (video._layer_from hívja; az at / dur már megvan): az effekt neve, a horgony (on),
    az effekt saját mezői a sémája szerint (fx.params), a mag. ValueError: rossz alak."""
    from app.tools import fx
    eff = str(r.get("effect") or "lightning").strip().lower().replace(" ", "_").replace("-", "_")
    if eff not in fx.names():
        soon = f" ({', '.join(fx.PLANNED)} are planned)" if eff in fx.PLANNED else ""
        raise ValueError(f"{what} (effect): effect {r.get('effect')!r} — {' | '.join(fx.names())}{soon}, e.g. {EXAMPLE}.")
    try:
        seed = int(r["seed"]) if r.get("seed") not in (None, "") else None
    except (TypeError, ValueError):
        raise ValueError(f"{what} (effect): seed = a whole number.") from None
    blend = str(r.get("blend") or "over").strip().lower()
    if r.get("on") not in (None, "") and fx.is_layer(fx.anchor(r["on"], what)):
        blend = "screen"                                   # egy grafikai réteg fölött csak a fény (screen) lehet
    if blend not in fx.BLENDS:
        raise ValueError(f"{what} (effect): blend {r.get('blend')!r} — over (default: drawn into the footage, the scene darkens "
                         "and takes the effect's colour) | screen (only the light, screen-blended — no darkening).")
    out: dict[str, Any] = {"effect": eff, **({"on": fx.anchor(r["on"], what)} if r.get("on") not in (None, "", "hand") else {}),
                           **({"blend": "screen"} if blend == "screen" else {}), **fx.params(eff, r, what)}
    if seed is not None:
        out["seed"] = seed
    if isinstance(r.get("trigger"), dict):
        out["trigger"] = r["trigger"]
    return out


def is_effect(ly: Any) -> bool:
    return isinstance(ly, dict) and str(ly.get("type") or "").strip().lower() == "effect"


def pending(ly: Any) -> bool:
    """A réteg ideje még a hangból jön (after_said / at_speech_end_after). Típus nélkül is: a szerkesztés változása
    ({"id": "L1", "after_said": …}) a meglévő effekt-rétegé — a típusát a _layer_from a régi rétegből veszi."""
    return isinstance(ly, dict) and (is_effect(ly) or not ly.get("type")) and \
        (ly.get("after_said") not in (None, "") or ly.get("at_speech_end_after") not in (None, ""))


def placeholder(ly: dict[str, Any]) -> dict[str, Any]:
    """Az előzetes (szerkezeti) ellenőrzéshez: a hangból jövő idő helyén 0."""
    return {**{k: v for k, v in ly.items() if k not in ("after_said", "at_speech_end_after")}, "at": ly.get("at") or 0.0}


def label(ly: dict[str, Any]) -> str:
    tg = ly.get("target")
    where = "" if tg is None else f" towards [{tg[0]:g}, {tg[1]:g}]" if isinstance(tg, list) else " along the arm" \
        if tg == "arm" else f" towards the {tg}"
    on = ly.get("on")
    frm = f" from [{', '.join(f'{v:g}' for v in on)}]" if isinstance(on, list) else " from the body" if on == "body" else ""
    trig = ly.get("trigger") or {}
    said = f", after “{trig['after_said']}”" if trig.get("after_said") else ""
    return f"{str(ly.get('effect', 'lightning')).replace('_', ' ')}{frm}{where}{said}"


# ---------- a mondat vége (10 ms) ----------

def levels(path: Path, stream: Optional[int] = None) -> list[float]:
    """A hang RMS-e 10 ms-onként (20 ms-os ablak), 16 kHz mono — üres, ha nincs hang."""
    import numpy as np
    amap = ["-map", f"0:{stream}"] if stream is not None else ["-map", "0:a:0?"]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), *amap, "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, timeout=600).stdout
    x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
    hop = int(16000 * HOP_S)
    if len(x) < 2 * hop:
        return []
    n = len(x) // hop
    e = np.square(x[: n * hop]).reshape(n, hop).mean(1)
    win = np.convolve(e, [0.5, 0.5])[:n]                       # a k. érték: [k−1, k+1) hop (20 ms-os ablak)
    return [float(v) for v in np.sqrt(win + 1e-12)]


def _threshold(rms: list[float]) -> float:
    import numpy as np
    a = np.asarray(rms, np.float32)
    return float(max(np.percentile(a, 30) * 3, np.percentile(a, 95) * 0.25))


def speech_end(rms: list[float], after: float, limit: float = SEARCH_S, pause: float = PAUSE_S) -> Optional[float]:
    """Az `after` utáni első mondatvég: ahol a hang a küszöb alá esik, és legalább `pause` másodpercig ott marad (a
    szavak közti rövid szünet nem vég). Ha `after` csendre esik, a következő beszéd vége. → s (10 ms-ra), vagy None, ha
    `limit` másodpercen belül nincs."""
    if not rms:
        return None
    thr = _threshold(rms)
    on = [v > thr for v in rms]
    n, need = len(on), max(1, round(pause / HOP_S))
    k = max(0, int(math.floor(after / HOP_S)))
    end = min(n, k + round(limit / HOP_S))
    spoke = any(on[max(0, k - 3):k + 1])
    while k < end:
        if on[k]:
            spoke = True
            k += 1
            continue
        if spoke and not any(on[k:k + need]):
            return round(k * HOP_S, 2)
        k += 1
    return None


async def resolve(raw: Any, footage: Path, notes: list[str], heard: Callable[[], Any]) -> Any:
    """after_said / at_speech_end_after → at (a többi réteg érintetlen). heard: async () → az átirat (csak ha kell).
    A kifejezés nem hangzik el / nincs mondatvég: a réteg kimarad, a jegyzet megmondja, miért."""
    if not isinstance(raw, list) or not any(pending(ly) for ly in raw):
        return raw
    from app.tools import transcript
    rms = await asyncio.to_thread(levels, footage)
    tr = None
    out = []
    for n, ly in enumerate(raw, 1):
        if not pending(ly):
            out.append(ly)
            continue
        ly = dict(ly)
        name = ly.get("id") or f"new layer {n}"
        phrase = str(ly.pop("after_said", None) or "").strip()
        after = ly.pop("at_speech_end_after", None)
        if phrase:
            if tr is None:
                tr = await heard()
            hit = transcript.find(tr, phrase)
            if hit is None:
                close = transcript.nearest(tr, phrase)
                notes.append(f"⚠ {name}: “{phrase}” is not said in the footage" + (f" (closest: “{close}”)" if close else
                             " (no speech was heard)") + " — the effect was left out. Give the time instead (at: \"00:06\").")
                continue
            # a kifejezés utolsó szavának vége (a felismerő ideje ±0,1–0,2 s) — a mondatvéget a hang adja, egy kicsit
            # korábbról keresve (a felismerő a szó végét néha a csend közepére teszi)
            end = speech_end(rms, max(0.0, hit["end"] - 0.3)) if rms else None
            at = end if end is not None else hit["end"]
            ly["at"] = at
            ly["trigger"] = {"after_said": phrase, "heard": hit["said"], "word_end": round(hit["end"], 3),
                             "speech_end": end}
            notes.append(f"{name} ({label({**ly, 'target': ly.get('target') or 'arm', 'trigger': ly['trigger']})}) starts at "
                         f"{at:.2f} s — the end of the sentence"
                         + (f" (the words end at {hit['end']:.2f} s, the voice at {end:.2f} s)" if end is not None
                            else " (the words' end; the sound has no clear pause after it)") + ".")
        else:
            from app.tools.video import timecode
            t = timecode(after, f"{name} at_speech_end_after")
            end = speech_end(rms, t) if rms else None
            if end is None:
                notes.append(f"⚠ {name}: no sentence ends within {SEARCH_S:g} s after {t:.2f} s — the effect was left out. "
                             "Give at instead.")
                continue
            ly["at"] = end
            ly["trigger"] = {"at_speech_end_after": round(t, 3), "speech_end": end}
            notes.append(f"{name} starts at {end:.2f} s — the first end of speech after {t:.2f} s.")
        out.append(ly)
    return out


# ---------- a klip ----------

def span(ly: dict[str, Any], fps: float, total: float) -> tuple[int, int, int]:
    """(f0: az effekt első kockája, active: ennyi kockán szól, f1: a klip vége, kizárólagos — a lecsengéssel)."""
    from app.tools import fx
    frames = int(math.floor(total * fps + 1e-6))
    f0 = int(math.ceil(float(ly["at"]) * fps - 1e-6))
    active = max(1, int(round(float(ly["dur"]) * fps)))
    f1 = min(frames, f0 + active + fx.get(ly.get("effect") or "lightning").tail())
    return f0, active, f1


def source_id(base: dict[str, Any]) -> dict[str, Any]:
    """A verzió felvételének képét meghatározó recept (az eredeti azonosítója — egy file_id tartalma sosem változik)."""
    return {k: base.get(k) for k in ("file_id", "adjust", "auto", "keep", "rotated", "hdr", "width", "height", "fps")}


def fx_params(ly: dict[str, Any]) -> dict[str, Any]:
    """Ami a rajzot meghatározza: az effekt, a horgony, a saját mezői, a mag (az id, az idő, a kiváltó NEM)."""
    from app.tools import fx
    eff = ly.get("effect") or "lightning"
    return {"effect": eff, "on": ly.get("on") or "hand", "seed": ly.get("seed"), "blend": ly.get("blend") or "over",
            **{k: ly.get(k) for k in fx.get(eff).PARAMS}}


def clip_key(ly: dict[str, Any], base: dict[str, Any], fps: float, f0: int, active: int, f1: int, from_proxy: bool) -> str:
    from app.tools import fx
    return _sha({"v": fx.version(ly.get("effect") or "lightning"), "src": source_id(base),
                 "proxy": base.get("media_id") if from_proxy else None, "fps": round(float(fps), 6), "span": [f0, active, f1],
                 "lead": LEAD_S, "crf": clip_crf(), "fx": fx_params(ly)})


def track_key(base: dict[str, Any], fps: float, a: int, b: int, from_proxy: bool) -> str:
    from app.tools.fx import common
    return _sha({"src": source_id(base), "proxy": base.get("media_id") if from_proxy else None, "fps": round(float(fps), 6),
                 "frames": [a, b], "tracker": common.tracker_id()})


def clip_crf() -> int:
    """A klip minősége: a köztes fájloké (VIDEO_CRF_INTERMEDIATE) − 5 — rövid szakasz, a végső kép része."""
    from app.tools import encoding
    return max(0, encoding.intermediate_crf() - 5)


def clip_path(key: str) -> Path:
    from app.tools import preview_cache
    return preview_cache.effect_path(key)


def _seed(ly: dict[str, Any]) -> int:
    return int(ly["seed"]) if ly.get("seed") is not None else 7          # a próba magja


async def _track_get(key: str) -> Optional[list[dict[str, Any]]]:
    from app import db
    from app.tools.fx import common
    try:
        hit = await db.ocr_cache_get(key, f"fx-track:{common.TRACK_VERSION}", "hand+pose")
    except Exception:  # noqa: BLE001
        return None
    return json.loads(hit) if hit else None


async def _track_put(key: str, track: list[dict[str, Any]]) -> None:
    from app import db
    from app.tools.fx import common
    try:
        await db.ocr_cache_put(key, f"fx-track:{common.TRACK_VERSION}", "hand+pose", json.dumps(track, separators=(",", ":")))
    except Exception:  # noqa: BLE001
        log.warning("effekt: a követés eltárolása nem sikerült", exc_info=True)


def _decode_args(src: str, W: int, H: int, fps: float, vf: list[str], ss: Optional[float], t: Optional[float]) -> list[str]:
    """ffmpeg: a forrás [ss, ss + t) szakasza → BGR kockák a videó kockarácsán (W × H, BT.709 → RGB). Az ss egy negyed
    kockával a kívánt kocka elé esik: a -ss az első olyan kockától ad, amelynek ideje ≥ ss (4 tizedesre kerekítve egy
    hajszállal a kocka mögé eshetne, és egy kockát elnyelne)."""
    from app.convert.engines import niced
    from app.tools import live_layers
    tm = (["-ss", f"{max(0.0, ss - 0.25 / fps):.4f}"] if ss else []) + (["-t", f"{t:.4f}"] if t else [])
    graph = ",".join(vf + [f"fps={live_layers._fps_q(fps)}",
                           f"scale={W}:{H}:in_color_matrix=bt709:in_range=tv:out_range=full:flags=bicubic", "format=bgr24"])
    return niced(["ffmpeg", "-v", "error", "-nostdin", *tm, "-i", src, "-map", "0:v:0", "-vf", graph, "-an",
                  "-f", "rawvideo", "-pix_fmt", "bgr24", "-"])


def _encode_args(dst: str, W: int, H: int, fps: float) -> list[str]:
    from app.convert.engines import niced
    from app.tools import encoding, live_layers
    gop = max(1, math.floor(fps))
    return niced(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
                  "-r", live_layers._fps_q(fps), "-i", "-",
                  "-vf", "scale=out_color_matrix=bt709:out_range=tv:in_range=full:flags=bicubic,format=yuv420p",
                  "-c:v", "libx264", "-preset", encoding.preset(), "-crf", str(clip_crf()), "-pix_fmt", "yuv420p", "-g", str(gop),
                  "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
                  "-movflags", "+faststart", "-an", dst])


def _run_clip(dec: list[str], enc: list[str], W: int, H: int, fps: float, ly: dict[str, Any], start: int, active: int,
              n: int, track: Optional[list[dict[str, Any]]], stat: dict[str, Any]) -> list[dict[str, Any]]:
    """A szálban: dekódolás → (követés) → rajz → kódolás. n kocka (a bevezetés + a klip); a kimenet csak a start-tól.
    → a követés (kockánként)."""
    import numpy as np
    from app.tools import fx
    from app.tools.fx import common
    fsz = W * H * 3
    pd = subprocess.Popen(dec, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pe = subprocess.Popen(enc, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    tk = common.Tracker() if track is None and common.needs_tracking(ly.get("on")) else None
    out_track: list[dict[str, Any]] = []
    st = fx.get(ly.get("effect") or "lightning")(W, H, fps, fx_params(ly), _seed(ly))
    t_track = t_draw = 0.0
    last = None
    try:
        for i in range(n):
            buf = pd.stdout.read(fsz)
            if len(buf) < fsz:                                        # a forrás rövidebb: az utolsó kocka áll
                if last is None:
                    raise RuntimeError("the footage couldn't be read for the effect")
                img = last
            else:
                img = np.frombuffer(buf, np.uint8).reshape(H, W, 3)
                last = img
            t0 = time.monotonic()
            tr = track[i] if track is not None and i < len(track) else (tk(img, int(round(i / fps * 1000))) if tk else {"h": [], "p": None})
            out_track.append(tr)
            t1 = time.monotonic()
            frame = st.frame(img, tr, (i - start) / fps, start <= i < start + active)
            if i >= start:
                pe.stdin.write(np.ascontiguousarray(frame).tobytes())
            t2 = time.monotonic()
            t_track += t1 - t0
            t_draw += t2 - t1
        pe.stdin.close()
        rc = pe.wait(timeout=600)
        if rc != 0:
            raise RuntimeError("the effect clip couldn't be encoded: " + pe.stderr.read().decode("utf-8", "replace")[-300:])
    finally:
        if tk is not None:
            tk.close()
        for p in (pd, pe):
            if p.poll() is None:
                p.kill()
        pd.wait()
    stat.update(track_s=round(t_track, 2), draw_s=round(t_draw, 2), frames=n - start, lead=start,
                casting=sum(1 for p in st.powers[start:] if p > 0), tracked=track is None)
    return out_track


async def render(ly: dict[str, Any], base: dict[str, Any], *, fps: float, total: float, work: Path,
                 W: int, H: int) -> tuple[Path, dict[str, Any]]:
    """Egy effekt-réteg klipje (gyorsítótárból, vagy most rajzolva) → (a fájl, mérés)."""
    from app.tools import footage, preview_cache
    f0, active, f1 = span(ly, fps, total)
    if f1 - f0 < 2:
        raise ValueError("the effect starts at the very end of the video")
    src = footage.original(base)
    from_proxy = src is None
    key = clip_key(ly, base, fps, f0, active, f1, from_proxy)
    dst = clip_path(key)
    stat: dict[str, Any] = {"key": key, "f0": f0, "f1": f1, "active": active, "from": "proxy" if from_proxy else "original"}
    if dst.exists():
        await preview_cache.touch(["e-" + key], "_effects")
        stat["cached"] = True
        return dst, stat
    lead = min(f0, int(round(LEAD_S * fps)))
    a = f0 - lead
    n = f1 - a
    tkey = track_key(base, fps, a, f1, from_proxy)
    from app.tools.fx import common
    track = await _track_get(tkey) if common.needs_tracking(ly.get("on")) else [{"h": [], "p": None}] * n
    if track is not None and len(track) < n:
        track = None
    if track is None:
        why = common.available()
        if why:
            raise RuntimeError(why)
    t_a, t_b = a / fps, f1 / fps
    plain = float((base.get("plain") or {}).get("duration") or base.get("duration") or total)
    tmp_src = None
    from app.tools import fast_render, crafts
    if from_proxy:                                    # az eredeti lejárt: a munkamásolatból (a check megmondja)
        p = crafts.existing(base.get("media_id"))
        dec = _decode_args(str(p), W, H, fps, [], t_a, t_b - t_a)
    elif base.get("keep") or t_b > plain:             # vágott / beszúrt / a kimerevített végbe lóg: a köztes darab
        tmp_src = work / f"fx-src-{key[:10]}.mp4"
        await footage.clip(base, src, tmp_src, t_a, t_b, plain, work)
        dec = _decode_args(str(tmp_src), W, H, fps, [], None, None)
    else:                                             # a lánc egyenesen az eredetiből (nincs köztes kódolás)
        m_vf = footage.chain(base, await _info(src), work) + await fast_render.base_filters(src)
        dec = _decode_args(str(src), W, H, fps, m_vf, t_a, t_b - t_a)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{key}.tmp-{uuid.uuid4().hex[:6]}.mp4")
    enc = _encode_args(str(tmp), W, H, fps)
    t0 = time.monotonic()
    try:
        got = await asyncio.to_thread(_run_clip, dec, enc, W, H, fps, ly, f0 - a, active, n, track, stat)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
        if tmp_src is not None:
            tmp_src.unlink(missing_ok=True)
    if track is None and common.needs_tracking(ly.get("on")):
        await _track_put(tkey, got)
    secs = time.monotonic() - t0
    stat.update(seconds=round(secs, 2), ms_per_frame=round(secs / max(1, n) * 1000, 1),
                s_per_effect_s=round(secs / max(1e-6, (f1 - f0) / fps), 2), load=round(os.getloadavg()[0], 2),
                bytes=dst.stat().st_size, size=[W, H])
    await preview_cache.register("e-" + key, "_effects", dst.stat().st_size)
    log.info("effekt-klip %s (%s): %s", ly.get("id"), ly.get("effect"), stat)
    return dst, stat


async def _info(src: Path) -> dict[str, Any]:
    from app.tools import mediaprobe
    return await mediaprobe.info(src)


def on_layers(layers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Az egy másik grafikai rétegen ülő effektek (on: "L17" — a „tüzes grafikon”): a projekt első építése UTÁN készülnek
    (a cél-réteg alfáját a motor rajzolja), lásd prepare_on_layers."""
    from app.tools import fx
    return [ly for ly in layers if is_effect(ly) and fx.is_layer(ly.get("on"))]


def _run_light(frames_alpha: list[Any], enc: list[str], W: int, H: int, fps: float, ly: dict[str, Any], active: int,
               stat: dict[str, Any]) -> None:
    """A szálban: a cél-réteg alfája kockánként → csak a fény (fekete alapon) → kódolás."""
    import numpy as np
    from app.tools import fx
    pe = subprocess.Popen(enc, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    st = fx.get(ly.get("effect") or "flames")(W, H, fps, {**fx_params(ly), "blend": "screen"}, _seed(ly))
    black = np.zeros((H, W, 3), np.uint8)
    t0 = time.monotonic()
    try:
        for i, m in enumerate(frames_alpha):
            pe.stdin.write(np.ascontiguousarray(st.frame(black, {"mask": m}, i / fps, i < active)).tobytes())
        pe.stdin.close()
        if pe.wait(timeout=600) != 0:
            raise RuntimeError("the effect clip couldn't be encoded: " + pe.stderr.read().decode("utf-8", "replace")[-300:])
    finally:
        if pe.poll() is None:
            pe.kill()
    stat.update(draw_s=round(time.monotonic() - t0, 2), frames=len(frames_alpha), casting=sum(1 for p in st.powers if p > 0))


async def prepare_on_layers(fxs: list[dict[str, Any]], D: dict[str, Any], *, W: int, H: int, fps: float, total: float,
                            work: Path, project: str = "project.ecproj",
                            step: Callable[..., None] = lambda *a, **k: None) -> tuple[list[dict[str, Any]], list[str],
                                                                                     list[str], list[str]]:
    """A rétegen ülő effektek (on: "L17"): a cél-réteg EGYEDÜL, átlátszóan a motorból (fél méretben, a hatás szakaszára)
    → az alfája a sugárzó maszk kockánként → csak a fény (screen) → a builder a cél-réteg FÖLÉ teszi (above).
    → (D.effects-elemek, fájlnevek, verified, findings) — a hívó utána újraépíti a projektet."""
    import numpy as np
    from app.tools import fast_render, fx as fxreg, preview_cache, video
    items, names, verified, findings = [], [], [], []
    by_id = {x["id"]: x for x in D.get("layers") or []}
    for ly in fxs:
        tgt = by_id.get(ly["on"])
        if tgt is None:
            findings.append(f"Layer {ly['id']} (effect, {ly.get('effect')}): on {ly['on']!r} — there is no such graphic layer "
                            "on the timeline (effect layers can't be a target); it was left out.")
            continue
        f0, active, f1 = span(ly, fps, total)
        if f1 - f0 < 2:
            continue
        key = _sha({"v": fxreg.version(ly.get("effect") or "flames"),
                    "on": {k: v for k, v in tgt.items() if k != "name"}, "fps": round(float(fps), 6), "span": [f0, active, f1],
                    "size": [W, H], "crf": clip_crf(), "fx": fx_params(ly)})
        dst = clip_path(key)
        stat: dict[str, Any] = {"key": key, "f0": f0, "f1": f1, "on": ly["on"]}
        step(3, 4, f"rendering the {ly.get('effect')} effect on {ly['on']} ({ly['id']})")
        try:
            if dst.exists():
                await preview_cache.touch(["e-" + key], "_effects")
                stat["cached"] = True
            else:
                t0 = time.monotonic()
                info = await fast_render.inspect(work / project)
                me = next((x for x in info["layers"] if x["name"] == tgt["name"]), None)
                if me is None:
                    raise RuntimeError(f"{ly['on']} isn't in the project")
                off = [x["i"] for x in info["layers"] if x["i"] != me["i"]]
                solo = await fast_render.variant(work / project, info, off, f"fx-solo-{ly['id']}")
                mov = f"fx-alpha-{ly['id']}.mov"
                await video._run("render", "--project", solo, "--comp", info["comp"], "--out", mov,
                                 "--start", f"{max(0.0, (f0 - 0.5) / fps):.6f}", "--end", f"{(f1 - 0.5) / fps:.6f}",
                                 "--format", "prores", "--prores", "4444", "--channels", "rgba", "--audio", "off",
                                 "--resolution", "0.5", "--quality", "draft", cwd=work)
                raw = await asyncio.to_thread(lambda: subprocess.run(
                    ["ffmpeg", "-v", "error", "-i", str(work / mov), "-vf", "alphaextract", "-f", "rawvideo", "-pix_fmt", "gray",
                     "-"], capture_output=True, timeout=600).stdout)
                mi = await _info(work / mov)
                mw, mh = int(mi.get("display_width") or mi["width"]), int(mi.get("display_height") or mi["height"])
                n = len(raw) // (mw * mh)
                masks = [np.frombuffer(raw, np.uint8, mw * mh, k * mw * mh).reshape(mh, mw) for k in range(n)]
                masks += [masks[-1]] * max(0, (f1 - f0) - n) if masks else [np.zeros((mh, mw), np.uint8)] * (f1 - f0)
                t1 = time.monotonic()
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(f"{key}.tmp-{uuid.uuid4().hex[:6]}.mp4")
                try:
                    await asyncio.to_thread(_run_light, masks[: f1 - f0], _encode_args(str(tmp), W, H, fps), W, H, fps, ly,
                                            active, stat)
                    os.replace(tmp, dst)
                finally:
                    tmp.unlink(missing_ok=True)
                    for f_ in (mov, solo):
                        (work / f_).unlink(missing_ok=True)
                await preview_cache.register("e-" + key, "_effects", dst.stat().st_size)
                stat.update(alpha_s=round(t1 - t0, 2), seconds=round(time.monotonic() - t0, 2), load=round(os.getloadavg()[0], 2))
                log.info("effekt-klip %s (%s, on %s): %s", ly["id"], ly.get("effect"), ly["on"], stat)
        except Exception as e:  # noqa: BLE001
            log.warning("effekt %s: %s", ly.get("id"), e)
            findings.append(f"Layer {ly['id']} (effect, {ly.get('effect')}, on {ly['on']}): it couldn't be drawn — {str(e)[:200]}.")
            continue
        name = f"effect-{ly['id']}-{key[:12]}.mp4"
        try:
            os.link(dst, work / name)
        except OSError:
            shutil.copyfile(dst, work / name)
        names.append(name)
        items.append({"id": ly["id"], "name": f"Layer {ly['id']} – {ly.get('effect')} effect", "file": name,
                      "at": round(f0 / fps, 6), "dur": round((f1 - f0) / fps, 6), "k": 1.0, "blend": "screen",
                      "above": tgt["name"]})
        verified.append(f"Layer {ly['id']}: {label(ly)} on {ly['on']} — {video._tc(f0 / fps)}–{video._tc(f1 / fps)}, from "
                        f"{ly['on']}'s own shape" + (" (drawn before, from the cache)" if stat.get("cached") else
                                                      f", drawn in {stat.get('seconds')} s") + ".")
        if stat.get("casting") == 0:
            findings.append(f"Layer {ly['id']} (effect): {ly['on']} shows nothing in its time — move it (at) into {ly['on']}'s time.")
    return items, names, verified, findings


async def prepare(layers: list[dict[str, Any]], base: dict[str, Any], *, W: int, H: int, fps: float, total: float,
                  work: Path, step: Callable[..., None] = lambda *a, **k: None) -> tuple[list[dict[str, Any]], list[str],
                                                                                         list[str], list[str]]:
    """A verzió effekt-rétegei → (a builder D.effects-e, a munkamappába tett fájlok nevei, verified, findings)."""
    from app.tools import fx as fxreg, video
    fx = [ly for ly in layers if is_effect(ly) and not fxreg.is_layer(ly.get("on"))]     # (a rétegen ülők: on_layers)
    items: list[dict[str, Any]] = []
    names: list[str] = []
    verified: list[str] = []
    findings: list[str] = []
    for k, ly in enumerate(sorted(fx, key=lambda x: (float(x["at"]), x["id"]))):
        if float(ly["at"]) >= total - 0.3:                     # (layer_comps már szólt róla)
            continue
        step(2, 4, f"rendering the {ly.get('effect', 'lightning')} effect ({ly['id']})")
        try:
            async with video._render_gate():
                path, st = await render(ly, base, fps=fps, total=total, work=work, W=W, H=H)
        except Exception as e:  # noqa: BLE001
            log.warning("effekt %s: %s", ly.get("id"), e)
            findings.append(f"Layer {ly['id']} (effect, {ly.get('effect')}): it couldn't be drawn — {str(e)[:200]}.")
            continue
        f0, f1 = st["f0"], st["f1"]
        name = f"effect-{ly['id']}-{st['key'][:12]}.mp4"
        try:
            os.link(path, work / name)
        except OSError:
            shutil.copyfile(path, work / name)
        names.append(name)
        items.append({"id": ly["id"], "name": f"Layer {ly['id']} – {ly.get('effect', 'lightning')} effect", "file": name,
                      "at": round(f0 / fps, 6), "dur": round((f1 - f0) / fps, 6), "k": 1.0,        # (a klip a kompozíció méretében)
                      **({"blend": "screen"} if ly.get("blend") == "screen" else {})})
        casting = st.get("casting")
        verified.append(f"Layer {ly['id']}: {label(ly)} — {video._tc(f0 / fps)}–{video._tc((f1) / fps)}"
                        + (" (drawn before, from the cache)" if st.get("cached") else
                           f", drawn on your footage in {st.get('seconds')} s") + ".")
        if casting == 0:
            findings.append(f"Layer {ly['id']} (effect, {ly.get('effect')}): no raised hand was found in its time — it doesn't "
                            "show. Move it (at) to where a hand is raised towards the camera or the side, or anchor it "
                            "(on: [x, y]).")
    return items, names, verified, findings
