"""Szakasz-gyorsítótáras előnézet (HLS): a videó rövid szakaszokra bontva, minden szakasz a tartalma ujjlenyomatával
(sha256) — egy szerkesztés után csak a megváltozott ujjlenyomatú szakaszok készülnek újra (10-25, Kommandant: „gyorsan
akarom látni az eredményt, gyorsan akarok iterálni rajta”; terv: plan20261025_preview_cache.md).

A három réteg:
  · terv (plan / fingerprint / diff / runs) — tiszta, I/O nélküli: a rács, a kockák, az ujjlenyomatok;
  · tárolás (seg_path / ready / touch / sweep) — a fájlok a köteten (DATA_DIR/preview-cache/<source_id>/<hash>.ts), a
    nyilvántartás Postgresben (preview_segments, LRU a VIDEO_PREVIEW_CACHE_GB fölött); PG nélkül a fájlok maguk (mtime);
  · render (context / render_run / render_audio) — a fast_render elemeiből a munkamásolaton: alapkép, átlátszó grafika a
    motorból csak a futam idejére, a végig látható rétegek körből, felirat libass-szal; a hang egyben, külön HLS-sávként.

A rács (a döntés: a forrás abszolút ideje): minden rész (footage.keep egy eleme) a SAJÁT forrásának n·SEG_S másodpercein
vágódik, a rész széle mindig szakaszhatár; a 0,5 s-nál rövidebb maradék a szomszédjába olvad. Így a forrás [322, 324)
szakasza egy előtte lévő vágás után is ugyanaz (ugyanaz a hash) — csak a vágáspontot tartalmazó szakaszok újak. A
zárókártya (kimerevített vég) saját szakaszai a kimeneti időben, a kártya elejétől rácsozva.

A kockák: a k. szakasz a kimenet [round(out_start·fps), round((out_start + dur)·fps)) kockái (footage.Timeline.frame
kerekítésével) — a szakaszok hézag és átfedés nélkül fedik a videót, a rész-határok a PR #42 szabálya szerint esnek.

Az ujjlenyomat a szakasz HELYI idejében: ami a képen látszik — a tartalom azonosítója (forrás + a munkamásolat receptje +
a forrás kockái), a metsző rétegek, feliratsorok, kamerakulcsok, áttűnés; a végig látható réteg helyett a köre és a
fázisa. A kimeneti idő (out_start) nem kerül bele, ezért egy vágás utáni eltolás nem érvénytelenít. Minden időpont a
szakasz HELYI KOCKASZÁMÁVAL (az első kocka, amelyen a dolog már látszik, a szakasz első kockájához mérve), nem
másodpercben: a vágás / beszúrás a rétegek, feliratok idejét 3 tizedesre kerekítve tolja el, a szakaszokat pontosan
(4 tizedes) — a másodpercben mért helyi idő így egy beszúrás után minden későbbi feliratos szakaszt érvénytelenítene,
a kockaszám csak ott változik, ahol a kép is (egy kockával később jelenik meg valami). A kockán belüli eltolás (egy
animáció a kocka egy töredékével odébb) nem érvénytelenít. Amiről nem biztos, hogy hat a képre: benne van.
"""
from __future__ import annotations

import asyncio
import collections
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

log = logging.getLogger("aice.preview_cache")

SEG_S = float(os.getenv("VIDEO_PREVIEW_SEG_S", "2.0"))
MIN_S = 0.5                 # ennél rövidebb maradék a szomszéd szakaszba olvad (legfeljebb SEG_S + MIN_S hosszú szakasz)
PREVIEW_CACHE_VERSION = 1   # a kompozíció / kódolás változásakor emelni: minden régi szakasz érvénytelen
RUN_MAX = max(1, int(os.getenv("VIDEO_PREVIEW_RUN_MAX", "8") or 8))   # egy futam (egy ffmpeg-hívás) legfeljebb ennyi szakasz
X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p"]
AUDIO_BITRATE = "128k"
LEAD_S = 0.5                # az alapkép ennyivel a futam előtt indul (a kockarács kitöltése a szakasz elején is egyezzen)
_Z = {"fullscreen": 0, "split": 1, "waveform": 1, "chart": 2, "quote": 2, "stat": 2, "title": 3, "chapter": 3,
      "lower_third": 4, "source": 5}                        # = video._LAYER_Z (a builder rétegsorrendje)
_MEDIA_KEYS = {"credit", "credits", "image", "image_query", "image_prompt", "stock", "file", "file_id", "clip", "generate",
               "ai_image", "ai_picture", "sound_file"}


def enabled() -> bool:
    return os.getenv("VIDEO_PREVIEW_HLS", "1").strip().lower() not in ("0", "false", "no", "off")


def frame(t: float, fps: float) -> int:
    """A kimeneti idő kockája — ugyanaz a kerekítés, mint footage.Timeline.frame (a rész-határok ott esnek)."""
    return round(t * fps + 1e-6)


def _on(t: float, fps: float) -> int:
    """Az első kocka, amelyen a t-kor bekapcsoló dolog már látszik (kockaidő ≥ t)."""
    return math.ceil(t * fps - 1e-6)


def _r4(x: float) -> float:
    v = round(float(x), 4)
    return v + 0.0 if v else 0.0                            # (-0.0 → 0.0: a JSON-ban más volna)


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def _fps_q(fps: float) -> str:
    """A kockasebesség racionálisan (29.97 → 30000/1001) — az ffmpeg fps-szűrője ebben a időalapban számoz."""
    q = Fraction(fps).limit_denominator(1001)
    return f"{q.numerator}/{q.denominator}"


_ENGINE: list[str] = []


def engine_version() -> str:
    """A motor (EffectCraft CLI) azonosítója az ujjlenyomatba: a bináris mérete + ideje (újrafordítás is látszik);
    EFFECTCRAFT_VERSION felülírja."""
    v = os.getenv("EFFECTCRAFT_VERSION", "").strip()
    if v:
        return v
    if not _ENGINE:
        try:
            from app.tools import video
            exe = video.effect_cli()
            st = Path(exe).stat() if exe else None
            _ENGINE.append(f"{st.st_size}-{int(st.st_mtime)}" if st else "none")
        except Exception:
            _ENGINE.append("none")
    return _ENGINE[0]


# ---------- a terv (tiszta) ----------

def _cuts(a: float, b: float, step: float) -> list[float]:
    """Egy rész [a, b) forrás-tartománya a forrás n·step pontjain, a széleken a rész széle; a MIN_S-nél rövidebb
    első / utolsó darab a szomszédjába olvad."""
    pts = [a]
    n = math.floor(a / step + 1e-9) + 1
    while n * step < b - 1e-6:
        if n * step > a + 1e-6:
            pts.append(round(n * step, 6))
        n += 1
    pts.append(b)
    if len(pts) > 2 and pts[1] - pts[0] < MIN_S:
        del pts[1]
    if len(pts) > 2 and pts[-1] - pts[-2] < MIN_S:
        del pts[-2]
    return pts


def _strip(ly: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in ly.items() if k not in ("at", "dur", "until") and not str(k).startswith("_")}


def _walk_media(x: Any, out: list[str]) -> None:
    if isinstance(x, dict):
        for k, v in x.items():
            if k in _MEDIA_KEYS and v not in (None, "", [], {}):
                out.append(f"{k}={json.dumps(v, sort_keys=True, ensure_ascii=False)}")
            elif k == "shape" and v in ("map", "lottie"):
                out.append(f"shape={v}")
            else:
                _walk_media(v, out)
    elif isinstance(x, list):
        for v in x:
            _walk_media(v, out)


def _sounds(ly: dict[str, Any]) -> bool:
    """Ad-e hangot a réteg (bevágás, kép a képben hanggal — video._clip_sounds: sound = replace | mix, alapból off)."""
    def walk(x: Any) -> bool:
        if isinstance(x, dict):
            return bool(x.get("sound_file")) or x.get("sound") in ("replace", "mix") or any(walk(v) for v in x.values())
        if isinstance(x, list):
            return any(walk(v) for v in x)
        return False
    return walk(ly)


def _part_desc(x: dict[str, Any]) -> dict[str, Any]:
    return {"src": x.get("src") or None, "a": _r4(x["a"]), "b": _r4(x["b"]), "gain": x.get("gain"), "match": x.get("match")}


def layer_key(c: dict[str, Any], e: dict[str, Any]) -> str:
    """Egy végig látható réteg körének kulcsa: a réteg tartalma (idő nélkül) + minden, ami a rajzát adja (méret, akcentus,
    nyelv, a kezdet kockán belüli eltolása). A kör (bevezetés + periódus, kockában) ehhez tartozik, verziótól függetlenül."""
    g = c["glob"]
    return _sha({"v": PREVIEW_CACHE_VERSION, "engine": c["engine"], "desc": e["desc"], "WH": g["WH"], "size": g["size"],
                 "fps": g["fps"], "accent": g["accent"], "lang": g["lang"], "safe": g["safe"], "frac": e["frac"]})


def plan_ctx(spec: dict[str, Any], cycles: Optional[dict[str, dict[str, int]]] = None, *, engine: Optional[str] = None,
             extra: Any = None) -> dict[str, Any]:
    """A terv közös adatai (egyszer számolva, a szakaszok ujjlenyomata ebből): részek, rétegek, feliratsorok, kamera,
    áttűnések, a globális mezők. cycles = {layer_key: {"intro": kocka, "period": kocka}} (period 0: nem ismétlődik);
    extra: a projektfájl azonosítója, ha a kép nem a spec-ből jön (kézzel szerkesztett projekt, motoros út)."""
    from app.tools import footage, video
    base = spec.get("base") or {}
    if spec.get("kind") != "overlay" or not base:
        raise ValueError("the segment preview is for videos on your own footage (overlay projects)")
    story, opts = spec.get("story") or {}, spec.get("opts") or {}
    W, H = int(base["width"]), int(base["height"])
    fps = float(base.get("fps") or 30)
    fps = fps if 1 <= fps <= 120 else 30.0
    total = round(float(base["duration"]), 3)
    card = base.get("end_card")
    plain = float((base.get("plain") or {}).get("duration") or base["duration"]) if card else float(base["duration"])
    pw, ph = (int(base["proxy"]["w"]), int(base["proxy"]["h"])) if base.get("proxy") else footage.size(W, H)
    source_id = str(base.get("file_id") or base.get("media_id") or "")
    ss = footage.segs(base.get("keep")) or [{"a": 0.0, "b": plain, "src": None}]
    parts, acc = [], 0.0
    for i, x in enumerate(ss):                                  # (ugyanaz az összegzés, mint footage._offsets)
        parts.append({**x, "i": i, "O": acc, "F": frame(acc, fps)})
        acc += x["b"] - x["a"]
    end = acc                                                   # a felvétel vége a kimeneti időben
    hold_at = min(end, plain) if card and total - min(end, plain) > 0.5 / fps else None
    recipe = {"adjust": base.get("adjust") or None, "auto": base.get("auto") or None, "rotated": base.get("rotated"),
              "hdr": base.get("hdr"), "proxy": [pw, ph], "side": footage.PROXY_SIDE, "crf": footage.PROXY_CRF}
    subs = opts.get("subtitles") or story.get("subtitles") or []
    style = (opts.get("caption_style") or "box") if subs else None
    user_layers = [ly for ly in story.get("layers") or [] if isinstance(ly, dict)]
    lang = video._lang_of(video._layer_text_all({"layers": [{k: v for k, v in ly.items() if k != "name"} for ly in user_layers]})
                          + " " + (spec.get("layer_source") or spec.get("source") or ""))
    glob = {"WH": [W, H], "size": [pw, ph], "fps": fps, "accent": video._rgb(opts.get("accent")), "lang": lang,
            "safe": bool(subs), "caption_style": style}
    c: dict[str, Any] = {"W": W, "H": H, "fps": fps, "total": total, "plain": plain, "end": end, "hold_at": hold_at,
                         "pw": pw, "ph": ph, "source_id": source_id, "parts": parts, "recipe": recipe, "glob": glob,
                         "engine": engine if engine is not None else engine_version(), "extra": extra,
                         "cycles": dict(cycles or {})}
    # a rétegek, a builder sorrendjében (z, at, id), a builder vágásával (a videó vége után induló kimarad, a túllógó rövidül)
    lys = list(user_layers)
    if card:
        basis: list[str] = []
        _walk_media(user_layers, basis)
        lys.append({"id": "end_card", "type": "fullscreen", "kind": "text", "end": True, "at": hold_at if hold_at is not None
                    else plain, "dur": card.get("dur"), "text": card.get("title"), "subtitle": card.get("subtitle") or "",
                    "kicker": card.get("kicker") or "", "credits_from": sorted(set(basis))})
    out: list[dict[str, Any]] = []
    keep = base.get("keep")
    for ly in sorted(lys, key=lambda x: (_Z.get(x.get("type"), 5), float(x.get("at") or 0), str(x.get("id")))):
        at, dur = float(ly.get("at") or 0), float(ly.get("dur") or 0)
        if at >= total - 0.3:
            continue
        if at + dur > total + 0.05:
            dur = round(total - at, 3)
        a, b = round(at, 3), round(at + dur, 3)
        e: dict[str, Any] = {"id": str(ly.get("id")), "type": ly.get("type"), "a": a, "b": b, "desc": _strip(ly),
                             "auto": ly.get("type") in ("source", "lower_third") and not ly.get("position") and not ly.get("side")}
        if ly.get("type") == "waveform":                         # a sávok a felvétel hangjából: a forrás-darabok a réteg idejéhez
            e["wave"] = [[s, _r4(sa), _r4(sb)] for s, sa, sb, _o in footage.pieces_out(keep, a, b)]
        if min(total, b) - max(0.0, a) >= _persistent_s() and ly.get("id") != "end_card":
            e["persist"] = True
            e["ps"] = _on(a, fps)                                # az első kocka, amin látszik: a kör innen számol
            e["frac"] = _r4(e["ps"] / fps - a)
            e["lb"] = _on(min(total, b) - _outro_s(), fps)       # a kivezetés első kockája (a motor rajzolja)
            e["key"] = layer_key(c, e)
            e["timed"] = _timed(ly)
        out.append(e)
    for i, e in enumerate(out):                                  # automatikus elhelyezés: a korábban lerakott, vele egy időben látszók
        if not e["auto"]:
            continue
        nb = []
        for n in out[:i]:
            ov = _on(min(e["b"], n["b"]), fps) - _on(max(e["a"], n["a"]), fps)    # a közös idő (kockában: a takarás súlya)
            if min(e["b"], n["b"]) > max(e["a"], n["a"]):
                nb.append([n.get("place") or _sha(n["desc"]), ov])
        e["place"] = _sha({"desc": e["desc"], "nb": nb})
    c["layers"] = out
    c["rows"] = video.caption_track(subs, W, H, total, style) if subs else []
    cam = opts.get("camera")
    c["camera"] = video._camera_keys(cam, total, W, H) if cam else None
    joins = []
    for i in range(1, len(parts)):
        nxt, prev = parts[i], parts[i - 1]
        if nxt.get("join") == "dissolve":
            n = max(2, round(min(footage.DISSOLVE_S, (nxt["b"] - nxt["a"]) / 2) * fps))
            joins.append({"F": nxt["F"], "n": n, "prev": _part_desc(prev), "next": _part_desc(nxt)})
    c["dissolves"] = joins
    return c


def _timed(ly: dict[str, Any]) -> bool:
    """Az ábra a réteg hosszához időzített (verseny, megadott tempójú kibontás): a vége a közepén is számít."""
    if not isinstance(ly.get("chart"), dict):
        return False
    try:
        from app.tools import video
        return video._chart_seconds(ly["chart"]) is not None
    except Exception:
        return True


def _persistent_s() -> float:
    from app.tools import fast_render
    return fast_render.PERSISTENT_S


def _outro_s() -> float:
    from app.tools import fast_render
    return fast_render.OUTRO_S


def _grid(c: dict[str, Any]) -> list[dict[str, Any]]:
    """A szakaszok (k, out_start, dur, f0, frames, src, src_a, src_b, part, hold) — a kockák hézagmentesen."""
    fps, total = c["fps"], c["total"]
    raw: list[tuple[float, Optional[int], Optional[str], Optional[float], Optional[float]]] = []   # (kimeneti kezdet, rész, forrás, a, b)
    for p in c["parts"]:
        pts = _cuts(p["a"], p["b"], SEG_S)
        for g0, g1 in zip(pts, pts[1:]):
            raw.append((p["O"] + (g0 - p["a"]), p["i"], p["src"], g0, g1))
    tail = c["hold_at"]
    if tail is not None:                                         # a zárókártya: a kártya elejétől rácsozva (kimeneti idő)
        pts = _cuts(0.0, total - tail, SEG_S)
        raw += [(tail + g0, None, None, None, None) for g0 in pts[:-1]]
    bounds = [x[0] for x in raw] + [total]
    segs = []
    for j, (o, part, src, ga, gb) in enumerate(raw):
        f0, f1 = frame(bounds[j], fps), frame(bounds[j + 1], fps)
        segs.append({"out_start": round(bounds[j], 6), "dur": round(bounds[j + 1] - bounds[j], 6), "f0": f0, "frames": f1 - f0,
                     "src": src, "src_a": None if ga is None else round(ga, 6), "src_b": None if gb is None else round(gb, 6),
                     "part": part, "hold": part is None})
    out: list[dict[str, Any]] = []
    for s in segs:                                               # kocka nélküli szakasz (egy kockánál rövidebb rész): az előzőbe
        if s["frames"] <= 0 and out:
            out[-1]["dur"] = round(out[-1]["dur"] + s["dur"], 6)
            continue
        if s["frames"] <= 0:
            continue
        out.append(s)
    for k, s in enumerate(out):
        s["k"] = k
    return out


def fingerprint(spec: dict[str, Any], seg: dict[str, Any], plan_ctx: dict[str, Any]) -> dict[str, Any]:
    """Egy szakasz kanonikus ujjlenyomat-objektuma (hash = sha256 a rendezett, tömör JSON-ján). Minden időpont a HELYI
    kockaszámával (a szakasz első kockájához mérve); a végig látható réteg ismert körnél a köre és a fázisa."""
    c = plan_ctx
    fps, o, f0, n = c["fps"], seg["out_start"], seg["f0"], seg["frames"]
    f1 = f0 + n
    t0, t1 = f0 / fps, f1 / fps
    g = c["glob"]
    fp: dict[str, Any] = {"v": PREVIEW_CACHE_VERSION, "engine": c["engine"], "enc": X264[1:6:2], "source_id": c["source_id"],
                          "recipe": c["recipe"], "frames": n, "fps": fps, "size": g["size"], "WH": g["WH"],
                          "accent": g["accent"], "lang": g["lang"], "safe": g["safe"], "caption_style": g["caption_style"]}
    if c.get("extra") is not None:
        fp["x"] = c["extra"]
    if seg.get("hold"):
        last = c["parts"][-1]                                    # a kimerevített kocka: a felvétel utolsó kockája (forráskocka)
        m_last = round(last["a"] * fps) + (frame(c["hold_at"], fps) - 1 - last["F"])
        fp["src"] = {"hold_of": [last.get("src") or None, m_last], "match": last.get("match")}
    else:
        p = c["parts"][seg["part"]]
        fp["src"] = [_r4(seg["src_a"]), _r4(seg["src_b"])]
        fp["sf"] = round(p["a"] * fps) + (f0 - p["F"])          # a forrás első kockája (a forrás saját kockarácsán)
        fp["part"] = {"src": p.get("src") or None, "gain": p.get("gain"), "match": p.get("match")}
    lys = []
    for e in c["layers"]:                                        # a szakasz kockáin látszó rétegek (be: az első kocka ≥ at)
        if _on(e["b"], fps) <= f0 or _on(e["a"], fps) >= f1:
            continue
        lys.append(_layer_fp(c, e, o, f0, f1))
    fp["layers"] = lys
    subs = []
    cs = lambda t: round(t * 100) / 100                                              # noqa: E731  (az ASS centiszekundumai)
    for r in c["rows"]:
        if _on(cs(r["t1"]), fps) <= f0 or _on(cs(r["t0"]), fps) >= f1:
            continue
        ws = [min(max(float(w), r["t0"]), r["t1"]) for w in r.get("words") or []]
        subs.append({"text": r["text"], "lines": r.get("lines"), "size": r.get("size"), "words": len(ws),
                     "fr": [_on(cs(r["t0"]), fps) - f0, _on(cs(r["t1"]), fps) - f0] + [_on(cs(w), fps) - f0 for w in ws]})
    fp["subs"] = subs
    fp["camera"] = _camera_fp(c["camera"], fps, f0, f1)
    dis = [{"j0": d["F"] - f0, "n": d["n"], "prev": d["prev"], "next": d["next"]} for d in c["dissolves"]
           if d["F"] < f1 and d["F"] + d["n"] > f0]
    if dis:
        fp["dissolve"] = dis
    return fp


def _layer_fp(c: dict[str, Any], e: dict[str, Any], o: float, f0: int, f1: int) -> dict[str, Any]:
    fps = c["fps"]
    cyc = c["cycles"].get(e.get("key")) if e.get("persist") else None
    if cyc and int(cyc.get("period") or 0) > 0:                  # körből: a kör + a fázis; a bevezetés / kivezetés helyben
        P, la = int(cyc["period"]), e["ps"] + int(cyc.get("intro") or 0)
        d: dict[str, Any] = {"id": e["id"], "key": e["key"], "P": P, "intro": int(cyc.get("intro") or 0),
                             "phase": (f0 - la) % P}
        if f0 < la:                                              # a bevezetésbe ér: a réteg eleje helyben
            d["fa"] = e["ps"] - f0
        if f1 > e["lb"]:                                         # a kivezetésbe ér: a vége helyben
            d.update(fb=e["lb"] - f0, fe=_on(e["b"], fps) - f0)
        return d
    d = {"id": e["id"], "desc": e["desc"], "fr": [_on(e["a"], fps) - f0, _on(e["b"], fps) - f0]}
    if e.get("persist") and not e.get("timed") and f1 <= e["lb"]:   # kör nélkül, a kivezetés előtt: a messzi vége nem látszik
        d["fr"] = d["fr"][:1]
    if e.get("place"):
        d["place"] = e["place"]
    if e.get("wave"):
        d["wave"] = e["wave"]
    return d


def _camera_fp(keys: Optional[list[list[float]]], fps: float, f0: int, f1: int) -> Any:
    """A szakaszra ható kamerakulcsok (az előtte álló, a benne lévők, az utána következő) a helyi kockaszámukkal; ha
    ezek mind ugyanazt az állást adják, csak az állás (a kamera ott áll — az idő nem számít)."""
    if not keys:
        return None
    ks = sorted(keys, key=lambda k: k[0])
    t0, te = f0 / fps, (f1 - 1) / fps                            # az első és az utolsó kocka ideje
    i0 = max([i for i, k in enumerate(ks) if k[0] <= t0 + 1e-9] or [0])
    i1 = min([i for i, k in enumerate(ks) if k[0] >= te - 1e-9] or [len(ks) - 1])
    sub = ks[i0:max(i0, i1) + 1]
    vals = {json.dumps([float(v) for v in k[1:]]) for k in sub}
    if len(vals) == 1:
        return {"static": [float(v) for v in sub[0][1:]]}
    return {"keys": [[_on(float(k[0]), fps) - f0] + [float(v) for v in k[1:]] for k in sub]}


def audio_fingerprint(spec: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    """Amitől a hang függ: a felvétel (forrás, a részek: vágás, beszúrás, illesztés, hangerő), a hossz (csend a kártya
    alatt), a zene, a hangot adó rétegek (bevágás, kép a képben) — a teljes idejükkel (a hang egyetlen folytonos sáv)."""
    base, opts = spec.get("base") or {}, spec.get("opts") or {}
    return {"v": PREVIEW_CACHE_VERSION, "engine": c["engine"], "source_id": c["source_id"], "audio": base.get("audio"),
            "parts": [{**_part_desc(p), "join": p.get("join")} for p in c["parts"]], "plain": c["plain"], "total": c["total"],
            "music": {k: opts.get(k) for k in ("music", "music_src", "music_name", "music_start", "music_level", "music_file_id")},
            "clips": [ly for ly in (spec.get("story") or {}).get("layers") or [] if isinstance(ly, dict) and _sounds(ly)],
            "hls": SEG_S, "aac": AUDIO_BITRATE, "x": c.get("extra")}


def plan(spec: dict[str, Any], cycles: Optional[dict[str, dict[str, int]]] = None, *, engine: Optional[str] = None,
         extra: Any = None) -> dict[str, Any]:
    """A spec (egy overlay-verzió leírása) → a szakaszok terve. Tiszta függvény (I/O nélkül; a motor azonosítója a
    folyamatban egyszer olvasva, ha nincs megadva)."""
    c = plan_ctx(spec, cycles, engine=engine, extra=extra)
    segs = _grid(c)
    for s in segs:
        s["hash"] = _sha(fingerprint(spec, s, c))
    return {"version": PREVIEW_CACHE_VERSION, "source_id": c["source_id"], "recipe": c["recipe"], "fps": c["fps"],
            "W": c["W"], "H": c["H"], "pw": c["pw"], "ph": c["ph"], "total": c["total"], "plain": c["end"],
            "frames": frame(c["total"], c["fps"]), "seg_s": SEG_S, "audio_hash": _sha(audio_fingerprint(spec, c)),
            "persistent": [{"id": e["id"], "key": e["key"], "ps": e["ps"], "lb": e["lb"], "a": e["a"], "b": e["b"]}
                           for e in c["layers"] if e.get("persist")],
            "segments": segs}


def diff(old: Optional[dict[str, Any]], new: dict[str, Any]) -> dict[str, Any]:
    """Az új terv szakaszai közül melyik nincs a régiben (hash szerint). → {dirty: [k…], reused, total}"""
    have = {s["hash"] for s in (old or {}).get("segments") or []}
    dirty = [s["k"] for s in new["segments"] if s["hash"] not in have]
    return {"dirty": dirty, "reused": len(new["segments"]) - len(dirty), "total": len(new["segments"])}


def runs(plan: dict[str, Any], ks: list[int]) -> list[list[int]]:
    """A kért szakaszok összefüggő futamokba (egy részen belül — a zárókártya egy külön rész —, legfeljebb RUN_MAX)."""
    segs = plan["segments"]
    out: list[list[int]] = []
    for k in sorted(set(int(k) for k in ks if 0 <= int(k) < len(segs))):
        if out and k == out[-1][-1] + 1 and segs[k]["part"] == segs[out[-1][-1]]["part"] and len(out[-1]) < RUN_MAX:
            out[-1].append(k)
        else:
            out.append([k])
    return out


# ---------- tárolás: fájlok a köteten, nyilvántartás Postgresben (PG nélkül: a fájlok maguk) ----------

def cache_root() -> Path:
    from app.config import settings
    return Path(settings.DATA_DIR) / "preview-cache"


def _safe(name: str) -> str:
    if not name or "/" in name or "\\" in name or name.startswith(".") or name in ("_work", "_cycles"):
        raise ValueError(f"invalid cache name: {name!r}")
    return name


def seg_path(source_id: str, h: str) -> Path:
    """<DATA_DIR>/preview-cache/<source_id>/<hash>.ts"""
    return cache_root() / _safe(source_id) / f"{_safe(h)}.ts"


def audio_dir(source_id: str, audio_hash: str) -> Path:
    """A hang-rendition darabjai: <…>/<source_id>/a-<audio_hash>/ (index.m3u8 + a-00000.ts…)."""
    return cache_root() / _safe(source_id) / f"a-{_safe(audio_hash)}"


def cycle_path(key: str) -> Path:
    """Egy végig látható réteg körének klipje (átlátszó ProRes, period kocka)."""
    return cache_root() / "_cycles" / f"{_safe(key)}.mov"


def _path_of(h: str, source_id: str) -> Path:
    if h.startswith("a-"):
        return audio_dir(source_id, h[2:])
    if h.startswith("c-"):
        return cycle_path(h[2:])
    return seg_path(source_id, h)


def _size(p: Path) -> int:
    if p.is_dir():
        return sum(x.stat().st_size for x in p.iterdir() if x.is_file())
    return p.stat().st_size if p.exists() else 0


_SWEPT = [0.0]


async def register(h: str, source_id: str, nbytes: int) -> None:
    from app import db
    if db.enabled():
        try:
            await db.preview_seg_put(h, source_id, int(nbytes))
        except Exception as e:
            log.warning("előnézet-szakasz nyilvántartása (%s): %s", h[:12], e)
    if time.time() - _SWEPT[0] > 60:                            # percenként legfeljebb egyszer: a méretkorlát
        _SWEPT[0] = time.time()
        try:
            await sweep()
        except Exception as e:
            log.warning("előnézet-gyorsítótár takarítása: %s", e)


def _scan() -> dict[str, tuple[str, Path]]:
    """PG nélkül: a köteten lévő szakaszok és hangok → {hash: (source_id, út)}."""
    out: dict[str, tuple[str, Path]] = {}
    root = cache_root()
    if not root.exists():
        return out
    for d in root.iterdir():
        if not d.is_dir() or d.name.startswith("_"):
            continue
        for p in d.iterdir():
            if p.suffix == ".ts" and p.is_file():
                out[p.stem] = (d.name, p)
            elif p.is_dir() and p.name.startswith("a-") and (p / "index.m3u8").exists():
                out[p.name] = (d.name, p)
    return out


def _done(p: Path) -> bool:
    return (p / "index.m3u8").exists() if p.name.startswith("a-") else p.is_file()


async def ready(hashes: list[str], source_id: Optional[str] = None) -> set[str]:
    """A kész szakaszok (a fájl megvan; PG-vel a nyilvántartás is). source_id-vel a köteten lévő, de nem nyilvántartott
    fájl is kész (és bekerül)."""
    from app import db
    hs = [h for h in dict.fromkeys(hashes) if h]
    if not hs:
        return set()
    ok: set[str] = set()
    if db.enabled():
        try:
            rows = await db.preview_seg_get(hs)
            stale = []
            for r in rows:
                (ok.add if _done(_path_of(r["hash"], r["source_id"])) else stale.append)(r["hash"])
            if stale:
                await db.preview_seg_delete(stale)
            if source_id:
                for h in hs:
                    if h not in ok and _done(_path_of(h, source_id)):
                        ok.add(h)
                        await db.preview_seg_put(h, source_id, _size(_path_of(h, source_id)))
            return ok
        except Exception as e:
            log.warning("előnézet-szakaszok lekérdezése (PG): %s — a köteten nézem", e)
    if source_id:
        return {h for h in hs if _done(_path_of(h, source_id))}
    have = _scan()
    return {h for h in hs if h in have}


async def touch(hashes: list[str], source_id: Optional[str] = None) -> None:
    """Használat (LRU): PG-ben last_used_at, PG nélkül a fájl ideje."""
    from app import db
    hs = [h for h in dict.fromkeys(hashes) if h]
    if not hs:
        return
    if db.enabled():
        try:
            await db.preview_seg_touch(hs)
            return
        except Exception as e:
            log.warning("előnézet-szakaszok használata (PG): %s", e)
    have = _scan() if not source_id else {}
    now = time.time()
    for h in hs:
        p = _path_of(h, source_id) if source_id else (have.get(h) or (None, None))[1]
        try:
            if p is not None and p.exists():
                os.utime(p, (now, now))
        except OSError:
            pass


def limit_bytes() -> int:
    try:
        gb = float(os.getenv("VIDEO_PREVIEW_CACHE_GB", "20") or 20)
    except ValueError:
        gb = 20.0
    return int(gb * 1024 ** 3)


def _remove(p: Path) -> None:
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    else:
        p.unlink(missing_ok=True)


async def sweep() -> dict[str, Any]:
    """LRU: VIDEO_PREVIEW_CACHE_GB fölött a legrégebben használt szakaszok (fájl + sor) törlődnek, a korlát 90 %-áig;
    a régi munkamappák (egy napnál régebbi, nem használt) is. → {bytes, removed, freed, limit}"""
    from app import db
    lim = limit_bytes()
    removed, freed = 0, 0
    _sweep_work()
    if db.enabled():
        total = await db.preview_seg_total()
        while total > lim:
            rows = await db.preview_seg_oldest(200)
            if not rows:
                break
            gone = []
            for r in rows:
                if total <= lim * 0.9:
                    break
                _remove(_path_of(r["hash"], r["source_id"]))
                gone.append(r["hash"])
                total -= int(r["bytes"] or 0)
                freed += int(r["bytes"] or 0)
            await db.preview_seg_delete(gone)
            removed += len(gone)
            if total <= lim * 0.9:
                break
        return {"bytes": total, "removed": removed, "freed": freed, "limit": lim}
    items = []
    root = cache_root()
    for d in (root.iterdir() if root.exists() else []):
        if not d.is_dir() or d.name == "_work":
            continue
        for p in d.iterdir():
            try:
                items.append((p.stat().st_mtime, _size(p), p))
            except OSError:
                pass
    total = sum(x[1] for x in items)
    for _, n, p in sorted(items, key=lambda x: x[0]):
        if total <= lim:
            break
        _remove(p)
        total -= n
        freed += n
        removed += 1
    return {"bytes": total, "removed": removed, "freed": freed, "limit": lim}


def _sweep_work(max_age_s: float = 86400) -> None:
    d = cache_root() / "_work"
    if not d.exists():
        return
    live = {str(c.get("work")) for c in _CTX.values()}
    for p in d.iterdir():
        try:
            if str(p) not in live and time.time() - p.stat().st_mtime > max_age_s:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
