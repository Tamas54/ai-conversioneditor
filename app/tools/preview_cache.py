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


class Unavailable(ValueError):
    """Ennek a verziónak nincs szakaszos előnézete, és újrapróbálva sem lesz (nem saját felvételes projekt, vagy a
    felvétel mindkét példánya — munkamásolat és eredeti — elveszett). Minden más hiba átmeneti."""


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
        raise Unavailable("the segment preview is for videos on your own footage (overlay projects)")
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
    for j, (o, part, src, ga, gb) in enumerate(raw):                # out_start / dur a KOCKARÁCSON (f0/fps, frames/fps): a
        f0, f1 = frame(bounds[j], fps), frame(bounds[j + 1], fps)  # lejátszási lista ideje = a kockák ideje, csúszás nélkül
        segs.append({"out_start": round(f0 / fps, 6), "dur": round((f1 - f0) / fps, 6), "f0": f0, "frames": f1 - f0,
                     "src": src, "src_a": None if ga is None else round(ga, 6), "src_b": None if gb is None else round(gb, 6),
                     "part": part, "hold": part is None})
    out = [s for s in segs if s["frames"] > 0]                   # (egy kockánál rövidebb rész: nincs saját kockája)
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
    val = lambda k: [float(v) for v in k[1:]]                                        # noqa: E731
    if len({json.dumps(val(k)) for k in sub}) == 1:
        return {"static": val(sub[0])}
    out = [[_on(float(k[0]), fps) - f0] + val(k) for k in sub]
    # a szakaszon kívüli szélső kulcs ideje nem számít, ha ugyanazt az állást adja, mint a szomszédja (a kamera addig áll):
    # különben a videó eleji nyugalmi kulcs (t = 0) helyi kockaszáma minden mozgás-szakaszt egy előtte lévő vágáshoz kötne
    if len(sub) > 1 and val(sub[0]) == val(sub[1]) and out[0][0] < 0:
        out[0][0] = None
    if len(sub) > 1 and val(sub[-1]) == val(sub[-2]) and out[-1][0] >= f1 - f0:
        out[-1][0] = None
    return {"keys": out}


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
    n, fps = frame(c["total"], c["fps"]), c["fps"]
    hold = c["hold_at"] if c["hold_at"] is not None else c["end"]
    return {"version": PREVIEW_CACHE_VERSION, "source_id": c["source_id"], "recipe": c["recipe"], "fps": fps,
            "W": c["W"], "H": c["H"], "pw": c["pw"], "ph": c["ph"], "total": round(n / fps, 6), "frames": n,
            "plain": round(frame(hold, fps) / fps, 6), "seg_s": SEG_S, "audio_hash": _sha(audio_fingerprint(spec, c)),
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


# ---------- a verzió előnézet-környezete (context) ----------
# Egy verzió (pid) egyszer áll össze, és a folyamat memóriájában marad (a spec változása — pl. a háttérben elkészült
# munkamásolat, proxy_pending ki — új környezetet ad): a munkamappa a köteten (DATA_DIR/preview-cache/_work/…), benne a
# projekt (preview.ecproj, ha a verziónak van; különben a teljes projekt) a médiával, a változatai (grafika felvétel és
# felirat nélkül; ahol a felvétel mozog, felirat nélkül), a végig látható rétegek köre, a feliratok ASS-e, a terv.
# context(pid) a projekt munkaterületén hívandó (tenancy: mint a kérés), és nem a video_preview.gate() alatt (maga kéri).

_CTX: "collections.OrderedDict[str, dict[str, Any]]" = collections.OrderedDict()
_CTX_MAX = 6
_LOCKS: dict[str, tuple[Any, asyncio.Lock]] = {}


def _lock(key: str) -> asyncio.Lock:
    """Kulcsonkénti zár, az éppen futó eseményhurokhoz kötve (Python 3.10: az asyncio-zár az első hurkához kötődik)."""
    loop = asyncio.get_running_loop()
    hit = _LOCKS.get(key)
    if hit is None or hit[0] is not loop:
        hit = (loop, asyncio.Lock())
        _LOCKS[key] = hit
    return hit[1]


def _stage_file(src: Path, assets: dict[str, str], work: Path, out_name: str) -> Path:
    """Mint video.stage, egy tetszőleges projektfájllal (a verzió preview.ecproj-ja): a médiafájlok linkként a
    munkamappába, a projekt médiahivatkozásai a fájlnévre."""
    from app.tools import crafts
    for name, fid in assets.items():
        dst = work / name
        if dst.exists() or name == out_name:
            continue
        try:
            p = crafts.existing(fid)
        except FileNotFoundError:
            log.warning("hiányzó videó-médiafájl: %s (%s)", name, fid)
            continue
        try:
            os.symlink(p, dst)
        except OSError:
            shutil.copyfile(p, dst)
    data = json.loads(src.read_text("utf-8"))
    for item in (data.get("items") or {}).values():
        kind = item.get("kind") or {}
        if kind.get("type") == "Footage" and kind.get("path"):
            b = Path(str(kind["path"]).replace("\\", "/")).name
            if b in assets:
                kind["path"] = b
    out = work / out_name
    out.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    return out


def _engine_index(info: dict[str, Any], lid: str) -> Optional[int]:
    """A spec rétegének párja a motor projektjében (a builder neve: „Layer <id> – …”)."""
    for x in info["layers"]:
        if x["name"] == f"Layer {lid}" or str(x["name"]).startswith(f"Layer {lid} "):
            return int(x["i"])
    return None


async def _cycle_get(key: str) -> Optional[dict[str, int]]:
    from app import db
    if db.enabled():
        try:
            return await db.preview_cycle_get(key)
        except Exception as e:
            log.warning("előnézet: a kör lekérdezése (PG): %s", e)
    j = cycle_path(key).with_suffix(".json")
    try:
        d = json.loads(j.read_text("utf-8"))
        return {"intro": int(d["intro"]), "period": int(d["period"])}
    except (OSError, ValueError, KeyError):
        return None


async def _cycle_put(key: str, cyc: dict[str, int]) -> None:
    from app import db
    if db.enabled():
        try:
            await db.preview_cycle_put(key, cyc["intro"], cyc["period"])
            return
        except Exception as e:
            log.warning("előnézet: a kör mentése (PG): %s", e)
    j = cycle_path(key).with_suffix(".json")              # PG nélkül: a klip mellé (fájl, nem adatbázis)
    j.parent.mkdir(parents=True, exist_ok=True)
    j.write_text(json.dumps(cyc), "utf-8")


async def cycles_known(spec: dict[str, Any]) -> dict[str, dict[str, int]]:
    """A spec végig látható rétegeinek már ismert köre (render nélkül) — egy régi verzió tervéhez (diff), hogy ugyanazt
    a hash-t kapja, mint amikor elkészült."""
    out = {}
    for e in plan_ctx(spec)["layers"]:
        if e.get("persist"):
            cyc = await _cycle_get(e["key"])
            if cyc:
                out[e["key"]] = cyc
    return out


async def _engine(ctx: dict[str, Any], project: str, fa: int, fb: int, name: str, alpha: bool) -> str:
    """A motor a kimenet [fa, fb) kockáira, a munkamásolat méretében, vázlatminőségben: átlátszó ProRes 4444 (grafika)
    vagy ProRes LT (ahol a motor rajzolja a felvételt is) → fb − fa kocka, 1/fps időalap, 0-tól. A motor a kompozíció
    kockarácsán rajzol: a [start, end) időbe eső i/fps kockákat — a kockaidő 6 tizedesre írva hol a kocka elé, hol mögé
    esik (13.966667 > 419/30: a 419. kocka kimaradt, és a darab egy kockával elcsúszott — 10-25, mérve), ezért fél
    kockával a kockák előtt kérjük: (fa − 0,5)/fps … (fb − 0,5)/fps pontosan az [fa, fb) kockák."""
    from app.tools import video
    fps = ctx["fps"]
    args = ["render", "--project", project, "--comp", ctx["comp"], "--out", name, "--start", f"{max(0.0, (fa - 0.5) / fps):.6f}",
            "--end", f"{(fb - 0.5) / fps:.6f}", "--format", "prores", "--audio", "off", "--json", "--resolution", f"{ctx['scale']:.6f}",
            "--quality", "draft"]
    args += ["--prores", "4444", "--channels", "rgba"] if alpha else ["--prores", "lt"]
    await video._run(*args, cwd=ctx["work"])
    if not (ctx["work"] / name).exists():
        raise RuntimeError(f"the preview graphics from frame {fa} to {fb} weren't rendered")
    return name


async def _cycles(c: dict[str, Any], info: dict[str, Any], project: Path, ctx: dict[str, Any]) -> dict[str, dict[str, int]]:
    """A végig látható rétegek köre (fast_render 2. lépése, rétegenként): ismert kulcsra a tárolt kör; különben minta
    (csak ez a réteg látszik) → kockánkénti md5 → bevezetés + periódus → a kör klipje (cycle_path) a köteten."""
    from app.tools import fast_render
    out: dict[str, dict[str, int]] = {}
    fps = c["fps"]
    for e in c["layers"]:
        if not e.get("persist"):
            continue
        key = e["key"]
        known = await _cycle_get(key)
        if known and (known["period"] == 0 or cycle_path(key).exists()):
            out[key] = known
            continue
        idx = _engine_index(info, e["id"])
        if idx is None:
            continue
        off = [int(x["i"]) for x in info["layers"] if x["on"] and int(x["i"]) != idx]
        p_proj = await fast_render.variant(project, info, off, f"pc-p{idx}")
        ps = e["ps"]
        end = min(_on(e["b"], fps), ps + round(fast_render.PROBE_S * fps))
        probe = f"pc-probe-{idx}.mov"
        await _engine(ctx, p_proj, ps, end, probe, alpha=True)
        cyc = fast_render.cycle(await fast_render._md5s(ctx["work"] / probe))
        if cyc and e["lb"] > ps + cyc[0] + cyc[1]:
            t0, P = cyc
            clip = cycle_path(key)
            clip.parent.mkdir(parents=True, exist_ok=True)
            tmp = clip.with_name(f"{key}.tmp-{uuid.uuid4().hex[:6]}.mov")
            await fast_render._cut_frames(ctx["work"] / probe, tmp, t0, P)
            os.replace(tmp, clip)
            await register("c-" + key, "_cycles", clip.stat().st_size)
            known = {"intro": int(t0), "period": int(P)}
        else:
            known = {"intro": 0, "period": 0}
        (ctx["work"] / probe).unlink(missing_ok=True)
        await _cycle_put(key, known)
        out[key] = known
    return out


async def context(pid: str) -> dict[str, Any]:
    """A verzió előnézet-környezete: a munkamappa (projekt + média + változatok), a munkamásolat (vagy proxy_pending
    esetén az eredeti + footage.chain), a spec, a terv (a végig látható rétegek körével). Verziónként egyszer épül."""
    from app.storage import read_meta
    meta = read_meta(pid) or {}
    spec = meta.get("spec") or {}
    if spec.get("kind") != "overlay" or not spec.get("base"):
        raise Unavailable("the segment preview is for videos on your own footage (overlay projects)")
    hand = bool(meta.get("hand_edited"))
    key = f"{pid}:{_sha(spec)[:16]}:{int(hand)}"
    if key in _CTX:
        _CTX.move_to_end(key)
        return _CTX[key]
    async with _lock("ctx:" + key):
        if key in _CTX:
            return _CTX[key]
        ctx = await _build(pid, spec, meta, key, hand)
        _CTX[key] = ctx
        while len(_CTX) > _CTX_MAX:
            _, old = _CTX.popitem(last=False)
            shutil.rmtree(old["work"], ignore_errors=True)
        return ctx


async def _build(pid: str, spec: dict[str, Any], meta: dict[str, Any], key: str, hand: bool) -> dict[str, Any]:
    from app.tools import crafts, fast_render, footage, mediaprobe, video, video_preview
    t_start = time.time()
    base = spec["base"]
    work = cache_root() / "_work" / f"{Path(pid).stem[:16]}-{_sha(key)[:8]}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    assets = video.assets_of(pid)
    notes: list[str] = []
    if "preview.ecproj" in assets and not hand:              # a verzió felirat nélküli előnézet-projektje (gyors építés)
        project = _stage_file(crafts.existing(assets["preview.ecproj"]), assets, work, "preview.ecproj")
    else:                                                     # a teljes projekt (a feliratok a változatokban kikapcsolva)
        project = video.stage(pid, work)
    c0 = plan_ctx(spec, {})
    W, H, fps, total = c0["W"], c0["H"], c0["fps"], c0["total"]
    ctx: dict[str, Any] = {"pid": pid, "key": key, "spec": spec, "work": work, "project": project.name, "W": W, "H": H,
                           "fps": fps, "fps_q": _fps_q(fps), "pw": c0["pw"], "ph": c0["ph"], "scale": c0["pw"] / W,
                           "total": total, "source_id": c0["source_id"], "notes": notes}
    async with video_preview.gate():
        info = await fast_render.inspect(project)
        ctx["comp"], ctx["info"] = info["comp"], info
        kind = fast_render.classify(info, spec)
        opts = spec.get("opts") or {}
        subs = opts.get("subtitles") or (spec.get("story") or {}).get("subtitles") or []
        style = opts.get("caption_style") or "box"
        if kind["ok"] and kind["captions"]:
            rows = video.caption_track(subs, W, H, float(info["dur"]), style) if subs else []
            if not fast_render.captions_match(kind["captions"], rows):
                kind = {"ok": False, "why": "the subtitles were changed by hand in the editor"}
        ctx["path"] = "fast" if kind["ok"] else "engine"
        ctx["why"] = kind.get("why") or ""
        extra = {"project": hashlib.sha256(project.read_bytes()).hexdigest()} if (hand or not kind["ok"]) else None
        cycles: dict[str, dict[str, int]] = {}
        ctx["alpha_proj"] = ctx["opaque_proj"] = None
        caps = [int(x["i"]) for x in kind.get("captions") or []]
        if kind["ok"]:
            foot = int(kind["footage"]["i"])
            ctx["caption_ix"] = caps
            if kind["gfx"]:
                ctx["alpha_proj"] = await fast_render.variant(project, info, [foot] + caps, "pc-alpha")
            cycles = await _cycles(plan_ctx(spec, {}, extra=extra), info, project, ctx)
        ctx["plan"] = plan(spec, cycles, extra=extra)
        c = plan_ctx(spec, cycles, extra=extra)
        # a motorral rajzolandó részek forrásai: a rövid rétegek, a végig láthatók bevezetése / kivezetése (kör nélkül az
        # egészük), és ahol a felvétel maga mozog (ráközelítés, osztott kép) — ott a motor a felvétellel együtt
        persist, matched = [], set()
        for e in c["layers"]:
            if not e.get("persist"):
                continue
            idx = _engine_index(info, e["id"])
            matched.add(idx)
            cyc = cycles.get(e["key"]) or {}
            P = int(cyc.get("period") or 0)
            persist.append({"id": e["id"], "i": idx, "key": e["key"], "ps": e["ps"], "la": e["ps"] + int(cyc.get("intro") or 0),
                            "lb": e["lb"], "end": _on(e["b"], fps), "P": P, "clip": cycle_path(e["key"]) if P else None})
        ctx["persist"] = sorted(persist, key=lambda p: -(p["i"] or 0))   # alulról fölfelé (a motor 1. rétege van legfelül)
        ctx["short"] = [(float(x["a"]), float(x["b"])) for x in kind.get("short") or []] + \
            [(float(x["a"]), float(x["b"])) for x in kind.get("persistent") or [] if int(x["i"]) not in matched]
        ctx["opaque"] = list(kind.get("opaque") or [])
        has_caps = any(fast_render._CAPTION.match(str(x["name"])) for x in info["layers"] if x["on"])
        ctx["ass"] = None
        if subs and (kind["ok"] or not has_caps):           # a felirat libass-szal (a projekt feliratrétegei helyett)
            (work / "pc-subs.ass").write_text(fast_render.ass_subtitles(subs, style, W, H, total, video._rgb(opts.get("accent"))),
                                              "utf-8")
            ctx["ass"] = "pc-subs.ass"
        # az alapkép: a munkamásolat (kimeneti idő = a munkamásolat ideje), vagy amíg az új recept munkamásolata készül
        # (proxy_pending), az eredeti a lánccal, a munkamásolat méretére
        pending, media, src = bool(base.get("proxy_pending")), None, None
        if not pending:
            try:
                media = crafts.existing(base.get("media_id") or base["file_id"])
            except FileNotFoundError:
                pending = True
                notes.append("The editor's copy of the footage is gone — the preview is drawn from the original.")
        if pending:
            src = footage.original(base)
            if src is None:
                try:
                    media = crafts.existing(base.get("media_id") or "-")
                    pending = False
                    notes.append("The original footage is no longer stored — the preview uses the editor's older copy.")
                except FileNotFoundError:
                    raise Unavailable("Neither the editor's copy nor the original footage is stored any more — upload it again.")
        ctx.update(pending=pending, media=media, src=src, tl=None, tl_items=[])
        if pending:
            m = await mediaprobe.info(src)
            if base.get("keep"):                                 # vágott / beszúrt: forrásonként a saját láncával
                tl = await footage.timeline(base, src, W, H, fps, work, size=(c0["pw"], c0["ph"]))
                ctx["tl"] = tl
                ctx["tl_items"] = await footage.transitions(tl, work, "pc", X264)
                ctx["pre"] = []
            else:
                mw, mh = int(m["display_width"] or W), int(m["display_height"] or H)
                ctx["pre"] = ([f"scale={c0['pw']}:{c0['ph']}"] if (c0["pw"], c0["ph"]) != (mw, mh) else []) + \
                    footage.chain(base, m, work)
            if footage.stabilized(base):
                notes.append("Stabilizing is in the final video; this quick preview is drawn before it.")
        else:
            ctx["pre"] = await fast_render.base_filters(media)
    ctx["seconds"] = round(time.time() - t_start, 2)
    log.info("előnézet-környezet: %s — %s út, %d szakasz, %.1f s", pid, ctx["path"], len(ctx["plan"]["segments"]), ctx["seconds"])
    return ctx


# ---------- egy futam: egy ffmpeg-hívás, szakaszonként egy MPEG-TS ----------

def _merge(iv: list[tuple[int, int]], gap: int) -> list[list[int]]:
    out: list[list[int]] = []
    for a, b in sorted(tuple(x) for x in iv if x[1] > x[0]):
        if out and a - out[-1][1] < gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _between(fa: int, fb: int, fps: float) -> str:
    """ffmpeg enable-kifejezés: a kimenet [fa, fb) kockái (fél kocka tűréssel a kockaidőkön)."""
    return f"between(t,{(fa - 0.5) / fps:.6f},{(fb - 0.5) / fps:.6f})"


async def _opaque_proj(ctx: dict[str, Any]) -> str:
    from app.tools import fast_render
    async with _lock("opq:" + ctx["key"]):
        if not ctx.get("opaque_proj"):
            ctx["opaque_proj"] = await fast_render.variant(ctx["work"] / ctx["project"], ctx["info"], ctx.get("caption_ix") or [],
                                                           "pc-opaque")
    return ctx["opaque_proj"]


async def _compose(ctx: dict[str, Any], F0: int, F1: int, tag: str) -> tuple[list[str], str, list[str]]:
    """A kimenet [F0, F1) kockái: ffmpeg-bemenetek, szűrőgráf ([vout], 0-tól), a futam ideiglenes fájljai. A gráfban az
    időbélyegek ABSZOLÚTAK (a kockaszám / fps), mint egy egymenetes rendernél — a rétegek enable-je, a felirat, a kör
    fázisa ugyanaz, akárhol kezdődik a futam (ezért egyezik kockára a szakaszonkénti és az egymenetes render)."""
    from app.tools import fast_render
    fps, q, N = ctx["fps"], ctx["fps_q"], F1 - F0
    args: list[str] = []
    chain: list[str] = []
    tmp: list[str] = []
    if ctx["path"] == "engine":                                # a motor rajzol mindent (vázlatminőség), a felirat ha kell
        name = await _engine(ctx, ctx["project"], F0, F1, f"{tag}-eng.mov", alpha=False)
        tmp.append(name)
        args += ["-i", name]
        chain.append(f"[0:v]fps={q},setpts=PTS-STARTPTS+{F0}[b0]")
        cur, k = "[b0]", 1
    else:
        hold_f = frame(ctx["plan"]["plain"], fps)            # az első kimerevített kocka (a felvétel vége után)
        lead = max(1, round(LEAD_S * fps))
        Sf = max(0, (hold_f if F0 >= hold_f else F0) - lead)
        L, stop = F0 - Sf, max(0, hold_f - Sf)
        pad = (F1 - hold_f) / fps + 1.0 if F1 > hold_f else 1.0
        span_end = min(F1, hold_f)
        if ctx.get("tl") is not None:                            # proxy_pending + vágás / beszúrás: az eredetikből
            ins, prefix, lab = ctx["tl"].inputs(Sf / fps, max(span_end, Sf + 1) / fps)
            args += ins
            head = prefix + lab
            k = ins.count("-i")
        else:
            args += ["-ss", f"{Sf / fps:.6f}", "-t", f"{(span_end - Sf) / fps + 1.0:.6f}", "-i", str(ctx["media"] or ctx["src"])]
            head, k = "[0:v]", 1
        pre = list(ctx.get("pre") or [])
        chain.append(head + ",".join(pre + [f"fps={q}", f"trim=end_pts={stop}", f"tpad=stop_mode=clone:stop_duration={pad:.4f}",
                                            f"trim=start_pts={L}:end_pts={L + N}", f"setpts=PTS+{Sf}"]) + "[b0]")
        cur = "[b0]"
        for it in ctx.get("tl_items") or []:                    # áttűnés (az eredetiből, a munkamásolat méretén)
            if it["b"] <= F0 / fps or it["a"] >= F1 / fps:
                continue
            if it["a"] >= F0 / fps - 1e-9:
                args += ["-itsoffset", f"{it['a']:.6f}", "-i", it["name"]]
            else:
                args += ["-ss", f"{F0 / fps - it['a']:.6f}", "-itsoffset", f"{(F0 - 0.25) / fps:.6f}", "-i", it["name"]]
            chain.append(f"{cur}[{k}:v]overlay=0:0:eof_action=pass:enable='{it['enable']}'[o{k}]")
            cur, k = f"[o{k}]", k + 1
        # a motorral rajzolt darabok: rövid rétegek, a végig láthatók be- / kivezetése (vagy körük nélkül az egészük)
        eng: list[tuple[int, int]] = [(_on(a, fps), _on(b, fps)) for a, b in ctx["short"]]
        loops = []
        for p in ctx["persist"]:
            if p["clip"] is not None and p["clip"].exists():
                eng += [(p["ps"], p["la"]), (p["lb"], p["end"])]
                loops.append(p)
            else:
                eng.append((p["ps"], p["end"]))
        gap = round(fast_render.MERGE_GAP_S * fps)
        clip = lambda iv: [(max(F0, a), min(F1, b)) for a, b in iv if min(F1, b) > max(F0, a)]   # noqa: E731
        opq = _merge(clip([(math.floor(a * fps + 1e-6), math.ceil(b * fps - 1e-6)) for a, b in ctx["opaque"]]), 1)
        alpha = _merge(clip(eng), gap)
        opq = _merge(opq + [x for x in map(tuple, alpha) if any(x[1] > o[0] and x[0] < o[1] for o in opq)], 1) if opq else []
        alpha = [x for x in alpha if not any(x[1] > o[0] and x[0] < o[1] for o in opq)]
        holes = [tuple(x) for x in opq + alpha]
        for p in loops:                                          # a végig látható réteg a körből, a fázisával
            fa, fb = max(F0, p["la"]), min(F1, p["lb"])
            if fb <= fa:
                continue
            phase = (fa - p["la"]) % p["P"]
            args += ["-stream_loop", "-1", "-ss", f"{phase / fps:.6f}", "-itsoffset", f"{(fa - 0.25) / fps:.6f}", "-i", str(p["clip"])]
            en = _between(fa, fb, fps) + "".join(f"*not({_between(a, b, fps)})" for a, b in holes if b > fa and a < fb)
            chain.append(f"{cur}[{k}:v]overlay=0:0:eof_action=pass:enable='{en}'[o{k}]")
            cur, k = f"[o{k}]", k + 1
        spans = [(a, b, False) for a, b in opq] + [(a, b, True) for a, b in alpha]
        for j, (a, b, is_alpha) in enumerate(sorted(spans)):
            proj = ctx["alpha_proj"] if is_alpha else await _opaque_proj(ctx)
            name = await _engine(ctx, proj, a, b, f"{tag}-{'g' if is_alpha else 'f'}{j:02d}.mov", alpha=is_alpha)
            tmp.append(name)
            args += ["-itsoffset", f"{(a - 0.25) / fps:.6f}", "-i", name]
            chain.append(f"{cur}[{k}:v]overlay=0:0:eof_action=pass:enable='{_between(a, b, fps)}'[o{k}]")
            cur, k = f"[o{k}]", k + 1
    if ctx.get("ass"):
        chain.append(f"{cur}ass={ctx['ass']}:fontsdir={fast_render.INTER_DIR}[sub]")
        cur = "[sub]"
    chain.append(f"{cur}setpts=PTS-STARTPTS,format=yuv420p[vout]")
    return args, ";".join(chain), tmp


async def _ffmpeg(*args: str, cwd: Path, capture: bool = False) -> bytes:
    from app.config import settings
    from app.convert.engines import run
    out, _ = await run("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args, cwd=cwd, timeout=settings.VIDEO_TIMEOUT_S)
    return out


def _segs_of(ctx: dict[str, Any], ks: list[int]) -> list[dict[str, Any]]:
    segs = ctx["plan"]["segments"]
    ks = sorted(set(int(k) for k in ks))
    if not ks or ks[0] < 0 or ks[-1] >= len(segs) or ks != list(range(ks[0], ks[-1] + 1)):
        raise ValueError(f"a run is a contiguous list of segment numbers inside the plan, not {ks[:5]}…")
    return [segs[k] for k in ks]


async def render_run(ctx: dict[str, Any], ks: list[int]) -> list[str]:
    """Egy összefüggő futam (runs() egy eleme) → szakaszonként egy MPEG-TS a seg_path-on (0-tól, kulcskockával indul),
    a PG-ben nyilvántartva; → a kész hash-ek (a ks sorrendjében). A video_preview.gate() alatt fut (maga kéri — a hívó
    ne tartsa); sosem a jobs.heavy() alatt (PR #41: az előnézet nem vár a nehéz munkákra, a végleges renderre)."""
    from app.tools import encoding, video_preview
    segs = _segs_of(ctx, ks)
    fps = ctx["fps"]
    F0, F1 = segs[0]["f0"], segs[-1]["f0"] + segs[-1]["frames"]
    tag = f"r{F0:07d}-{uuid.uuid4().hex[:6]}"
    starts = [s["f0"] - F0 for s in segs]
    t0 = time.time()
    async with video_preview.gate():
        t_gate = time.time()
        args, graph, tmp = await _compose(ctx, F0, F1, tag)
        t_comp = time.time()
        kf = "expr:" + "+".join(f"eq(n,{x})" for x in starts)
        try:
            await _ffmpeg(*args, "-filter_complex", graph, "-map", "[vout]", "-an", "-frames:v", str(F1 - F0), *X264,
                          "-g", str(max(s["frames"] for s in segs)), "-force_key_frames", kf, *encoding.bt709_tags(),
                          "-f", "segment", "-segment_format", "mpegts", "-segment_frames",
                          ",".join(str(x) for x in starts[1:]) or str(F1 - F0 + 1), "-reset_timestamps", "1",
                          f"{tag}-%03d.ts", cwd=ctx["work"])
            files = sorted(ctx["work"].glob(f"{tag}-[0-9][0-9][0-9].ts"))
            if len(files) != len(segs):
                raise RuntimeError(f"the preview run gave {len(files)} segment file(s) for {len(segs)} segments")
            done = []
            for s, f in zip(segs, files):
                dst = seg_path(ctx["source_id"], s["hash"])
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(f, dst)
                await register(s["hash"], ctx["source_id"], dst.stat().st_size)
                done.append(s["hash"])
        finally:
            for n in tmp:
                (ctx["work"] / n).unlink(missing_ok=True)
            for f in ctx["work"].glob(f"{tag}-*"):
                f.unlink(missing_ok=True)
    log.info("előnézet-futam %s: %d szakasz (%d kocka) %.2f s (kapura várt %.2f, motor %.2f)", ctx["pid"], len(segs), F1 - F0,
             time.time() - t0, t_gate - t0, t_comp - t_gate)
    ctx.setdefault("stats", []).append({"k": segs[0]["k"], "n": len(segs), "frames": F1 - F0, "s": round(time.time() - t_gate, 3),
                                        "engine_s": round(t_comp - t_gate, 3), "wait_s": round(t_gate - t0, 3)})
    return done


async def render_audio(ctx: dict[str, Any]) -> Path:
    """A hang egyben (a motor keveri, mint a fast_render 3. lépése) → AAC → HLS (folytonos időbélyeg, EGY kódolás) az
    audio_dir(source_id, audio_hash) alá: index.m3u8 + a-00000.ts…; ha már megvan, csak a használatát jegyzi."""
    from app.tools import video, video_preview
    h = ctx["plan"]["audio_hash"]
    d = audio_dir(ctx["source_id"], h)
    if _done(d):
        await touch(["a-" + h], ctx["source_id"])
        return d
    async with _lock("aud:" + h):
        if _done(d):
            return d
        async with video_preview.gate():
            name = f"pc-audio-{uuid.uuid4().hex[:6]}.wav"
            await video._run("render", "--project", ctx["project"], "--comp", ctx["comp"], "--out", name, "--format", "wav",
                             "--json", cwd=ctx["work"])
            wav = ctx["work"] / name
            tmp = d.with_name(f"{d.name}.tmp-{uuid.uuid4().hex[:6]}")
            tmp.mkdir(parents=True)
            try:
                ins = ["-i", str(wav)] if wav.exists() and wav.stat().st_size > 1024 else \
                    ["-f", "lavfi", "-t", f"{ctx['total']:.4f}", "-i", "anullsrc=r=48000:cl=stereo"]
                await _ffmpeg(*ins, "-af", "aresample=48000,apad", "-t", f"{ctx['total']:.4f}", "-ac", "2", "-c:a", "aac",
                              "-b:a", AUDIO_BITRATE, "-f", "hls", "-hls_time", f"{SEG_S:g}", "-hls_playlist_type", "vod",
                              "-hls_segment_filename", "a-%05d.ts", "index.m3u8", cwd=tmp)
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)
                os.replace(tmp, d)
            finally:
                wav.unlink(missing_ok=True)
                shutil.rmtree(tmp, ignore_errors=True)
            await register("a-" + h, ctx["source_id"], _size(d))
    return d


# ---------- a hang darabokban (a lejátszó első kockája nem várja meg az egész keverést) ----------
# P2 mérése (10-25): egy 10 perces projekt egész hangja (motor-keverés + egy AAC/HLS-kódolás) 13,4 s — az hls.js a hangsáv
# nélkül nem indul, így az első kocka is ennyit várt (30 percen ~40 s). Most: a hang AUDIO_CHUNK videószakaszonként egy
# darab, a darab a motorból csak a saját idejére keveredik (render --start --end --format wav: mérve mintapontos, 15 s
# hang 0,13 s), AAC, MPEG-TS 0-tól — a lejátszási listában a darabok között #EXT-X-DISCONTINUITY. A darab hossza
# mintára a videó kockarácsán (round(f·48000/fps)): a darabok összege a videó hossza, csúszás nélkül. Az AAC előtöltése
# (1024 minta) a darab elején marad: minden darab hangja ~21 ms-mal a kép mögött (állandó, nem halmozódik — a darab a
# lista szerinti helyén indul); a darab vége legfeljebb ~21 ms-mal hosszabb (az utolsó AAC-keret), a következő darab
# ráíródik. VIDEO_PREVIEW_AUDIO_CHUNK=1: szakaszonként egy darab (a kép discontinuity-szerkezete pontosan; a prototípus
# A2-je), ha egy lejátszó (iOS Safari) az eltérő szakaszolást nem viselné.

AUDIO_CHUNK = max(1, int(os.getenv("VIDEO_PREVIEW_AUDIO_CHUNK", "15") or 15))
AUDIO_SR = 48000
AUDIO_MARGIN_S = 0.05        # a motor ennyivel bővebben keveri a darabot (a vágás mintára a köteten történik)


def _sample(f: int, fps: float) -> int:
    return round(f * AUDIO_SR / fps)


def audio_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """A hang darabjai (tiszta): AUDIO_CHUNK egymást követő videószakasz egy darab. → [{k, seg0, seg1, f0, frames,
    out_start, dur, samples, hash}] — a hash a hang ujjlenyomatából (audio_hash) + a darab kockáiból; a fájl a
    seg_path(source_id, hash)-en (ugyanaz a tár, ugyanaz az LRU, ugyanaz a ready / touch, mint a képszakaszoké)."""
    segs, fps = plan["segments"], plan["fps"]
    out = []
    for j, i in enumerate(range(0, len(segs), AUDIO_CHUNK)):
        grp = segs[i:i + AUDIO_CHUNK]
        f0, n = grp[0]["f0"], sum(s["frames"] for s in grp)
        out.append({"k": j, "seg0": grp[0]["k"], "seg1": grp[-1]["k"] + 1, "f0": f0, "frames": n,
                    "out_start": round(f0 / fps, 6), "dur": round(n / fps, 6), "samples": _sample(f0 + n, fps) - _sample(f0, fps),
                    "hash": _sha({"v": PREVIEW_CACHE_VERSION, "kind": "audio", "audio": plan["audio_hash"], "f0": f0, "frames": n,
                                  "fps": fps, "sr": AUDIO_SR, "aac": AUDIO_BITRATE})})
    return out


async def render_audio_chunk(ctx: dict[str, Any], k: int) -> str:
    """A hang k. darabja (audio_plan) → MPEG-TS (AAC, 0-tól, pontosan a darab mintái) a seg_path(source_id, hash)-en,
    nyilvántartva; ha már kész, csak a használatát jegyzi. → a hash. A video_preview.gate() alatt (maga kéri)."""
    from app.tools import video, video_preview
    if "audio_plan" not in ctx:
        ctx["audio_plan"] = audio_plan(ctx["plan"])
    ch = ctx["audio_plan"][int(k)]
    h, sid = ch["hash"], ctx["source_id"]
    dst = seg_path(sid, h)
    if dst.exists():
        await touch([h], sid)
        return h
    async with _lock("ach:" + h):
        if dst.exists():
            return h
        fps = ctx["fps"]
        s0 = _sample(ch["f0"], fps)
        a = max(0.0, s0 / AUDIO_SR - AUDIO_MARGIN_S)
        b = (s0 + ch["samples"]) / AUDIO_SR + AUDIO_MARGIN_S
        sa, sb = f"{a:.6f}", f"{b:.6f}"
        first = math.floor(float(sa) * AUDIO_SR + 1e-6)           # a motor keverésének első mintája (mérve: a start lefelé)
        tag = uuid.uuid4().hex[:6]
        wav, tmp = ctx["work"] / f"pc-a{ch['f0']}-{tag}.wav", dst.with_name(f"{h}.tmp-{tag}.ts")
        async with video_preview.gate():
            try:
                await video._run("render", "--project", ctx["project"], "--comp", ctx["comp"], "--out", wav.name, "--format", "wav",
                                 "--start", sa, "--end", sb, "--json", cwd=ctx["work"])
                ins = ["-i", str(wav)] if wav.exists() and wav.stat().st_size > 1024 else \
                    ["-f", "lavfi", "-t", f"{b - a:.6f}", "-i", f"anullsrc=r={AUDIO_SR}:cl=stereo"]   # néma projekt: csend
                x = max(0, s0 - first)
                dst.parent.mkdir(parents=True, exist_ok=True)
                await _ffmpeg(*ins, "-af", f"aresample={AUDIO_SR},atrim=start_sample={x}:end_sample={x + ch['samples']},"
                              f"asetpts=PTS-STARTPTS,apad=whole_len={ch['samples']}", "-ac", "2", "-c:a", "aac", "-b:a", AUDIO_BITRATE,
                              "-f", "mpegts", str(tmp), cwd=ctx["work"])
                os.replace(tmp, dst)
            finally:
                wav.unlink(missing_ok=True)
                tmp.unlink(missing_ok=True)
        await register(h, sid, dst.stat().st_size)
    return h


# ---------- kockapontosság (elfogadás): szakaszonként vs. egy menetben ----------

async def _md5(ctx: dict[str, Any], F0: int, F1: int, tag: str) -> list[str]:
    """A kimenet [F0, F1) kockáinak md5-je a kódolás ELŐTT (ugyanaz a gráf, mint a render_run-é)."""
    args, graph, tmp = await _compose(ctx, F0, F1, tag)
    try:
        out = await _ffmpeg(*args, "-filter_complex", graph, "-map", "[vout]", "-an", "-frames:v", str(F1 - F0), "-f", "framemd5",
                            "-", cwd=ctx["work"])
    finally:
        for n in tmp:
            (ctx["work"] / n).unlink(missing_ok=True)
    return [ln.rsplit(",", 1)[1].strip() for ln in out.decode("utf-8", "replace").splitlines()
            if ln and not ln.startswith("#") and "," in ln]


async def frame_check(ctx: dict[str, Any], n: int = 20, seed: int = 0, ks: Optional[list[int]] = None) -> dict[str, Any]:
    """n véletlen szakaszhatár (vagy a ks-ben megadottak): a két szomszéd szakasz külön-külön (ahogy a render_run
    darabolja) vs. EGY egymenetes render ugyanarra a tartományra, ugyanazzal a kompozícióval → kockánkénti md5-egyezés.
    → {boundaries, frames, equal, ok, mismatches: [...], seconds}"""
    import random
    from app.tools import video_preview
    segs = ctx["plan"]["segments"]
    picks = sorted(ks) if ks else sorted(random.Random(seed).sample(range(1, len(segs)), min(n, len(segs) - 1)))
    t0 = time.time()
    lo, hi = segs[picks[0] - 1]["f0"], segs[picks[-1]]["f0"] + segs[picks[-1]]["frames"]
    async with video_preview.gate():
        one = await _md5(ctx, lo, hi, f"chk1-{uuid.uuid4().hex[:6]}")
        t_one = time.time() - t0
        frames = equal = 0
        bad = []
        for k in picks:
            for s in (segs[k - 1], segs[k]):
                per = await _md5(ctx, s["f0"], s["f0"] + s["frames"], f"chk-{uuid.uuid4().hex[:6]}")
                ref = one[s["f0"] - lo:s["f0"] - lo + s["frames"]]
                same = sum(a == b for a, b in zip(per, ref))
                frames += s["frames"]
                equal += same if len(per) == len(ref) == s["frames"] else 0
                if same != s["frames"] or len(per) != s["frames"]:
                    bad.append({"k": s["k"], "frames": s["frames"], "got": len(per), "equal": same,
                                "first_bad": next((i for i, (a, b) in enumerate(zip(per, ref)) if a != b), None)})
    return {"boundaries": picks, "frames": frames, "equal": equal, "ok": not bad and frames == equal, "mismatches": bad,
            "one_pass_s": round(t_one, 2), "seconds": round(time.time() - t0, 2)}
