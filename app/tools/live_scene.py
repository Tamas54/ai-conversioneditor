"""Live preview scene (L1 of plan20261026_live_preview.md): the server gives ASSETS, the browser composes them.

A version's scene JSON tells app/static/live-player.js what to play at every output time:
  · footage — RANGES of browser-playable working copies (H.264 MP4: the editor's copies of the footage, made by
    app/tools/footage.py), each mapped from output time [t0, t1) to the copy's own time `src`. When the version's
    own copy exists, ONE range on it. While a cut or an insert is pending (spec.base.proxy_pending: the new copy is
    still being made in the background), the ranges point into the copies that ALREADY exist (the parent's copy, any
    other copy of the same upload) — built with footage.segs / _offsets, the same arithmetic as footage.reuse — so a
    cut is visible at once, without waiting for any new copy.
  · hold_from — after this output time the last frame stands (the end card's frozen footage, D.base.hold).
  · colour — the shader recipe for each copy. A copy is made WITH its colour recipe baked in (footage.chain →
    video_fx.chain, the recipe is part of the copy's key, footage._key). A NEUTRAL copy (adjust {}) of the same
    timeline is preferred when it exists, and the shader applies the version's recipe on it: a colour change then
    never needs a new copy. Without a neutral copy, the baked copy is used and the shader applies the DIFFERENCE
    (the baked recipe undone, the new one applied). The eq part (brightness, contrast, gamma, saturation) is an exact
    port of ffmpeg's vf_eq (two 8-bit lookup tables — luma and chroma — like process_c / create_lut, on the copy's
    BT.709 limited-range YUV codes); see `color_params` for what is approximated.
  · transform — the footage layer's Transform keys (zoom = the camera, split = the base shrunk to a half) read from
    the engine project itself (the .ecproj is JSON: the same keys, interpolation and temporal ease the engine renders),
    evaluated in the browser with the engine's interpolation (linear / hold / bezier speed+influence).
  · layers / captions — lanes L2 / L3 (live_layers.layers_part, live_captions.captions_part), optional imports.

`build(pid)` assembles it; everything else here is pure (no I/O) and tested in tests/test_live_scene.py.
"""
from __future__ import annotations

import inspect
import json
import logging
import math
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("aice.live_scene")

SCENE_V = 1
TICKS = 254016000000            # the engine project's time unit (ticks per second; .ecproj stores every time in them)
_BIG = 1e12
EQ_KEYS = ("brightness", "contrast", "gamma", "saturation")
POINTWISE = ("lut", "levels", "shadows", "highlights", "highlight_rolloff", "temperature", "vibrance")   # not in eq
SPATIAL = ("denoise", "sharpen", "stabilize")       # the shader doesn't redo these (the server render has them)

try:                                                 # lanes L2 / L3 merge independently: without them, no layers / captions
    from app.tools import live_layers as _live_layers  # type: ignore
except ImportError:                                  # pragma: no cover - depends on the merge order
    _live_layers = None
try:
    from app.tools import live_captions as _live_captions  # type: ignore
except ImportError:                                  # pragma: no cover
    _live_captions = None


# ---------- the colour recipe (ffmpeg vf_eq, exactly) ----------

def _cdiv(a: int, b: int) -> int:
    """C integer division (truncates toward zero)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def _f32(x: float) -> float:
    """The value as ffmpeg's eq holds it: the parameters arrive in single precision (measured: brightness −0.2 gives
    int(100·b + 100) = 79 in ffmpeg 4.4, which only the float −0.2f does — the double would give 80)."""
    import struct
    return struct.unpack("f", struct.pack("f", float(x)))[0]


def eq_plane_lut(contrast: float = 1.0, brightness: float = 0.0, gamma: float = 1.0, weight: float = 1.0) -> list[int]:
    """ffmpeg vf_eq on one 8-bit plane, as a 256-entry table: check_values picks the path — identity, process_c (gamma 1,
    |contrast| < 7.9: integer multiply-add on the code value) or apply_lut (create_lut: contrast, brightness, gamma
    with gamma_weight, in 0…1 with the 0.5 pivot)."""
    contrast, brightness, gamma, weight = _f32(contrast), _f32(brightness), _f32(gamma), _f32(weight)
    if contrast == 1.0 and brightness == 0.0 and gamma == 1.0:
        return list(range(256))
    if gamma == 1.0 and abs(contrast) < 7.9:
        ci = int(contrast * 256 * 16)
        bi = _cdiv(int(100.0 * brightness + 100.0) * 511, 200) - 128 - _cdiv(ci, 32)
        return [max(0, min(255, ((i * ci) >> 12) + bi)) for i in range(256)]
    g, lw = 1.0 / gamma, 1.0 - weight
    out = []
    for i in range(256):
        v = contrast * (i / 255.0 - 0.5) + 0.5 + brightness
        if v <= 0.0:
            out.append(0)
            continue
        v = v * lw + math.pow(v, g) * weight
        out.append(255 if v >= 1.0 else int(256.0 * v))
    return out


def eq_luts(recipe: dict[str, Any]) -> Optional[dict[str, list[int]]]:
    """The eq part of a recipe (video_fx.chain: eq=brightness:contrast:gamma:saturation, gamma_weight 1, gamma_r/g/b 1)
    → {"y": luma table, "c": chroma table (U and V: contrast = saturation, gamma sqrt(1/1) = 1)}; None: no eq."""
    if not any(k in recipe for k in EQ_KEYS):
        return None
    c, b = float(recipe.get("contrast", 1.0)), float(recipe.get("brightness", 0.0))
    g, s = float(recipe.get("gamma", 1.0)), float(recipe.get("saturation", 1.0))
    return {"y": eq_plane_lut(c, b, g), "c": eq_plane_lut(s, 0.0, 1.0)}


def invert_lut(lut: list[int]) -> list[float]:
    """A (non-decreasing) table's inverse on the 256 codes: the mean input of each output code; codes the table never
    gives (gaps) interpolated, the clipped ends at the last unclipped input."""
    acc: dict[int, list[int]] = {}
    for i, v in enumerate(lut):
        acc.setdefault(int(v), []).append(i)
    pts = sorted((v, sum(xs) / len(xs)) for v, xs in acc.items())
    if len(pts) == 1:
        return [pts[0][1]] * 256
    out = []
    for y in range(256):
        if y <= pts[0][0]:
            out.append(pts[0][1])
        elif y >= pts[-1][0]:
            out.append(pts[-1][1])
        else:
            k = next(j for j in range(1, len(pts)) if pts[j][0] >= y)
            (y0, x0), (y1, x1) = pts[k - 1], pts[k]
            out.append(x0 + (x1 - x0) * (y - y0) / (y1 - y0))
    return [round(x, 3) for x in out]


def recipe_of(adjust: Optional[dict[str, Any]], auto: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The effective point-wise recipe of an adjust dict (video_fx.chain: the auto measurement first, the user's own
    values over it), without the spatial / temporal parts."""
    adj = {k: v for k, v in (adjust or {}).items() if k != "stabilize"}
    return {**((auto or {}) if adj.get("auto") else {}), **{k: v for k, v in adj.items() if k != "auto"}}


def color_params(target: Optional[dict[str, Any]], baked: Optional[dict[str, Any]], *, target_auto: Any = None,
                 baked_auto: Any = None) -> dict[str, Any]:
    """The shader's colour step for one copy: the copy has `baked` in it, the version wants `target`.
      mode none  — the copy already looks like the version (same recipe): nothing to do, exact;
      mode apply — a neutral copy: the target's eq tables on its YUV codes;
      mode diff  — another recipe baked in: its eq undone (inverse tables), then the target's.
    exact: the result is the server's picture up to rounding (eq only, no clipping lost); approx: what isn't redone
    (point-wise steps other than eq — LUT, levels / curves, temperature, vibrance — and the spatial ones)."""
    t, b = recipe_of(target, target_auto), recipe_of(baked, baked_auto)
    spatial = sorted({k for k in SPATIAL if (target or {}).get(k)} | {k for k in SPATIAL if (baked or {}).get(k)})
    if t == b and (target or {}).get("stabilize") == (baked or {}).get("stabilize"):
        return {"mode": "none", "exact": True, "approx": [], "pre": None, "eq": None}
    approx = sorted({k for k in POINTWISE if k in t or k in b}) + spatial
    pre = None
    if b:
        lb = eq_luts(b)
        pre = {"y": invert_lut(lb["y"]), "c": invert_lut(lb["c"])} if lb else None
    exact = not approx and (not b or not _clips(b))
    return {"mode": "diff" if b else "apply", "exact": exact, "approx": approx, "pre": pre, "eq": eq_luts(t)}


def _clips(recipe: dict[str, Any]) -> bool:
    """Does the baked eq clip (a code of the video range — luma 16…235, chroma 16…240 — pushed to 0 or 255)? There the
    undo can't bring the picture back; elsewhere it is within half a code."""
    lb = eq_luts(recipe)
    if not lb:
        return False
    return any(v in (0, 255) for v in lb["y"][16:236]) or any(v in (0, 255) for v in lb["c"][16:241])


# ---------- the copies and the ranges ----------

def _norm_keep(keep: Any, dur: float) -> list[tuple[Any, ...]]:
    """A timeline's picture identity: the pieces (source, a, b, colour match, dissolve-in) — None = the whole footage."""
    from app.tools import footage
    ss = footage.segs(keep) or [{"a": 0.0, "b": dur, "src": None}]
    if footage.whole(keep, dur) or not keep:
        ss = [{"a": 0.0, "b": dur, "src": None}]
    return [(x["src"], round(x["a"], 3), round(x["b"], 3), json.dumps(x.get("match") or {}, sort_keys=True),
             x.get("join") == "dissolve" and i > 0) for i, x in enumerate(ss)]


def _offsets(keep: Any, dur: float) -> list[tuple[float, dict[str, Any]]]:
    from app.tools import footage
    out, acc = [], 0.0
    for x in footage.segs(keep) or [{"a": 0.0, "b": dur, "src": None}]:
        out.append((acc, x))
        acc += x["b"] - x["a"]
    return out


def _mkey(x: dict[str, Any]) -> str:
    return json.dumps(x.get("match") or {}, sort_keys=True)


def _covers(copy: dict[str, Any], src: Optional[str], match: str, a: float, b: float, dur: float) -> list[tuple[float, float, float]]:
    """Which parts of the source span [a, b) the copy holds (same source, same colour match — a clip's picture depends
    on it): [(a', b', the copy's time of a')], in source order."""
    out = []
    for off, y in _offsets(copy.get("keep"), dur):
        if y["src"] != src or (src is not None and _mkey(y) != match):
            continue
        lo, hi = max(a, y["a"]), min(b, y["b"])
        if hi - lo > 1e-6:
            out.append((lo, hi, off + lo - y["a"]))
    return sorted(out)


def footage_plan(base: dict[str, Any], copies: list[dict[str, Any]], *, prefer: str = "own") -> dict[str, Any]:
    """The footage ranges of a version, from the copies that exist (pure).

    base: spec.base (keep, adjust, auto, plain, duration); copies: [{"id": file id, "keep", "adjust", "auto", "w", "h",
    "own": bool (the version's own, finished copy)}]. → {"ranges": [{"t0", "t1", "copy": index | None, "src"}],
    "copies": [the copies used, in index order], "end": the footage's end in output time, "missing": [[t0, t1, src]] (no
    copy holds it — e.g. an inserted clip while its copy is pending)}.

    One range when a copy of exactly this timeline exists (preference `own`, the default: the one baked with the
    version's own recipe — the server's own picture, exact colour and timing; else a neutral one (adjust {}) the shader
    colours; else any (the shader undoes its recipe). prefer="neutral" puts the neutral copy first. Measured 10-26 on
    S4: the neutral copy there — a smart cut of the 30 fps uncut copy — runs 54–85 ms EARLY against the original
    (by content), the version's own copy +13…+45 ms; so the own copy is the default). Otherwise piece by piece from the copies that hold
    the most of the new timeline (the parent's copy after a pending cut), adjacent pieces of one copy merged."""
    from app.tools import footage
    dur = float((base.get("plain") or {}).get("duration") or base.get("duration") or 0)
    keep = base.get("keep")
    end = footage.length(keep, dur) if keep else dur
    want = _norm_keep(keep, dur)
    target = recipe_of(base.get("adjust"), base.get("auto"))

    def rank(c: dict[str, Any]) -> tuple[int, int]:
        r = recipe_of(c.get("adjust"), c.get("auto"))
        neutral, own = not r and not (c.get("adjust") or {}).get("stabilize"), r == target
        first = neutral if prefer == "neutral" else own
        return (0 if first else 1 if (own or neutral) else 2, 0 if c.get("own") else 1)
    exact = sorted((c for c in copies if _norm_keep(c.get("keep"), dur) == want), key=rank)
    pieces = [(x["src"], _mkey(x), x["a"], x["b"], o) for o, x in _offsets(keep, dur)] if keep else [(None, "{}", 0.0, end, 0.0)]
    if exact:                    # the copy of this very timeline: one range (an inserted clip's part apart: see "clip")
        copies = exact[:1]
    # piece by piece: the copies in order of how much of the new timeline they hold (then the colour preference)

    def alone(c: dict[str, Any]) -> tuple[float, int]:
        """How much of the new timeline this copy holds, and in how many runs (each run = one range; fewer = fewer
        switches between the two video elements)."""
        got, runs, last = 0.0, 0, None
        for src, mk, a, b, o in pieces:
            for lo, hi, cs in _covers(c, src, mk, a, b, dur):
                got += hi - lo
                t0 = o + lo - a
                if last is None or abs(last[0] - t0) > 1e-4 or abs(last[1] - cs) > 1e-4:
                    runs += 1
                last = (o + hi - a, cs + hi - lo)
        return round(got, 3), runs
    score = {id(c): alone(c) for c in copies}
    order = sorted(copies, key=lambda c: (-score[id(c)][0], score[id(c)][1], rank(c)))
    used: list[dict[str, Any]] = []
    raw: list[dict[str, Any]] = []
    missing: list[list[Any]] = []
    for src, mk, a, b, o in pieces:
        todo = [(a, b)]
        for c in order:
            nxt = []
            for lo0, hi0 in todo:
                cur = lo0
                for lo, hi, cs in _covers(c, src, mk, lo0, hi0, dur):
                    if lo - cur > 1e-6:
                        nxt.append((cur, lo))
                    if c not in used:
                        used.append(c)
                    raw.append({"t0": o + lo - a, "t1": o + hi - a, "copy": used.index(c), "src": cs, "clip": src})
                    cur = hi
                if hi0 - cur > 1e-6:
                    nxt.append((cur, hi0))
            todo = nxt
            if not todo:
                break
        for lo, hi in todo:
            missing.append([round(o + lo - a, 6), round(o + hi - a, 6), src])
            raw.append({"t0": o + lo - a, "t1": o + hi - a, "copy": None, "src": lo, "clip": src})
    raw.sort(key=lambda r: r["t0"])
    ranges: list[dict[str, Any]] = []
    for r in raw:                                   # one copy, continuing where the last range stopped: one range
        p = ranges[-1] if ranges else None
        if p and p["copy"] is not None and p["copy"] == r["copy"] and p.get("clip") == r.get("clip") \
                and abs(p["t1"] - r["t0"]) < 1e-4 and abs(p["src"] + (p["t1"] - p["t0"]) - r["src"]) < 1e-4:
            p["t1"] = r["t1"]
            continue
        ranges.append(dict(r))
    for r in ranges:
        for k in ("t0", "t1", "src"):
            if isinstance(r.get(k), float):
                r[k] = round(r[k], 6)
        if r.get("clip") is None:
            r.pop("clip", None)          # (an inserted clip's range keeps its file id: the main recipe isn't on it)
    for p, r in zip(ranges, ranges[1:]):  # the same copy, continuing: the joint is IN the copy (its fades baked)
        if p.get("copy") is not None and p.get("copy") == r.get("copy") and abs(p["src"] + (p["t1"] - p["t0"]) - r["src"]) < 1e-4:
            r["cont"] = True
    _joins(ranges, keep, dur, float(base.get("fps") or 30))
    return {"ranges": ranges, "copies": used, "end": round(end, 6), "missing": missing}


def _joins(ranges: list[dict[str, Any]], keep: Any, dur: float, fps: float) -> None:
    """A range that starts at a joint of the new timeline gets the joint's kind: "cut" (the sound cross-cut, 15 ms) or
    "smooth" / "dissolve" (the sound crosses over XFADE_S, a dissolve also crosses the picture over n frames — the
    previous source CONTINUES under it, as footage.transitions / tl_audio make it) — the player does it, because the
    copies used here don't have this joint in them."""
    from app.tools import footage
    if not keep:
        return
    ss = footage.segs(keep)
    starts, acc = {}, 0.0
    for i, x in enumerate(ss):
        if i:
            starts[round(acc, 4)] = x
        acc += x["b"] - x["a"]
    for r in ranges:
        x = starts.get(round(r["t0"], 4))
        if x is None or r.get("cont"):
            continue
        j = x.get("join") or "cut"
        r["join"] = j
        if j == "dissolve":
            n = max(2, round(min(footage.DISSOLVE_S, (x["b"] - x["a"]) / 2) * fps))
            r["xfade"] = round(n / fps, 6)
        if j in ("smooth", "dissolve"):
            r["afade"] = footage.XFADE_S


def copies_of(base: dict[str, Any], *, parent: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """The copies of this upload that exist (the original's metadata: proxies, footage.make_proxy), plus the version's
    own media_id if it isn't listed (its recipe: the version's own — or, while pending, the parent's)."""
    from app.storage import read_meta
    from app.tools import crafts
    fid0 = str(base.get("file_id") or "")
    out: list[dict[str, Any]] = []
    for k, hit in (((read_meta(fid0) or {}).get("proxies") or {}).items() if fid0 else []):
        try:
            kd = json.loads(k)
            crafts.existing(hit["media_id"])
        except (ValueError, KeyError, FileNotFoundError, TypeError):
            continue
        out.append({"id": hit["media_id"], "keep": kd.get("keep"), "adjust": kd.get("adjust") or {}, "auto": hit.get("auto"),
                    "w": (hit.get("proxy") or {}).get("w"), "h": (hit.get("proxy") or {}).get("h")})
    own = base.get("media_id")
    pending = bool(base.get("proxy_pending"))
    for c in out:
        c["own"] = not pending and c["id"] == own
    if own and not any(c["id"] == own for c in out):
        try:
            crafts.existing(own)
            src = (parent or {}) if pending else base
            out.append({"id": own, "keep": src.get("keep"), "adjust": src.get("adjust") or {}, "auto": src.get("auto"),
                        "w": (base.get("proxy") or {}).get("w"), "h": (base.get("proxy") or {}).get("h"), "own": not pending})
        except FileNotFoundError:
            pass
    return out


# ---------- an inserted clip's browser copy (while the version's own copy is pending) ----------
# The clip's own picture as the timeline makes it (footage.timeline: HDR → SDR, the colour match, BT.709, fitted to
# the copy size, the video's frame rate) for just the inserted piece, H.264 at the working copy's size — a few seconds
# of encoding for a short clip, instead of the whole new working copy. Registered on the clip (its metadata:
# live_copies), so copies_of offers it like any other copy (its timeline: that one piece).

CLIP_V = 1
_CLIP_TASKS: dict[str, Any] = {}


def clip_key(a: float, b: float, match: Any, size: tuple[int, int], fps: float) -> str:
    return json.dumps({"v": CLIP_V, "a": round(float(a), 4), "b": round(float(b), 4), "match": match or {},
                       "size": [int(size[0]), int(size[1])], "fps": round(float(fps), 4)}, sort_keys=True)


def clip_copies(keep: Any) -> list[dict[str, Any]]:
    """The finished clip copies of the clips in this timeline (their files still there), as copies."""
    from app.storage import read_meta
    from app.tools import crafts, footage
    out = []
    for src in sorted({x["src"] for x in footage.segs(keep) if x["src"]}):
        for k, hit in ((read_meta(src) or {}).get("live_copies") or {}).items():
            try:
                kd = json.loads(k)
                crafts.existing(hit["media_id"])
            except (ValueError, KeyError, FileNotFoundError, TypeError):
                continue
            out.append({"id": hit["media_id"], "keep": [{"a": kd["a"], "b": kd["b"], "src": src, "match": kd.get("match") or {}}],
                        "adjust": {}, "auto": None, "w": kd["size"][0], "h": kd["size"][1], "own": False, "clip": True})
    return out


async def make_clip_copy(src: str, a: float, b: float, match: Any, gain: Any, size: tuple[int, int], fps: float) -> str:
    """The [a, b) piece of the clip `src` → a browser copy (see above) → its file id, registered on the clip."""
    import shutil
    import tempfile
    from app.convert.engines import run
    from app.storage import output_path, read_meta, temp_path, write_meta
    from app.storage.files import new_file_id
    from app.tools import crafts, encoding, fast_render, mediaprobe, video_fx, videonorm
    path = crafts.existing(src)
    info = await mediaprobe.info(path)
    tw, th = size
    work = Path(tempfile.mkdtemp(prefix="live-clip-", dir=str(temp_path("").parent)))
    try:
        pre = videonorm.tonemap_filters(info) + (video_fx.chain(match, work, None) if match else []) + \
            await fast_render.base_filters(path)
        fit = "contain" if videonorm.same_shape(info, tw, th) else "blur"
        d = b - a
        graph = f"[0:v]setpts=PTS-STARTPTS,{','.join(pre + [videonorm.fit_chain(tw, th, fit, 'lc')])},fps={fps:g}," \
                f"tpad=stop_mode=clone:stop_duration=2,trim=duration={d:.4f},format=yuv420p[v]"
        st = info.get("audio_stream")
        aud: list[str] = []
        if st is not None:
            af = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo" + \
                (f",volume={float(gain):.2f}dB" if gain else "") + f",apad,atrim=duration={d:.4f}"
            aud = ["-map", f"0:a:{st}", "-af", af, *encoding.aac()]
        await run("ffmpeg", "-v", "error", "-y", "-ss", f"{a:.4f}", "-t", f"{d + 0.5:.4f}", "-i", str(path), "-filter_complex", graph,
                  "-map", "[v]", *aud, "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-g", str(max(1, round(fps))),
                  "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                  "-movflags", "+faststart", "clip.mp4", timeout=600, cwd=work)
        fid = new_file_id("mp4")
        shutil.move(str(work / "clip.mp4"), output_path(fid))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    write_meta(fid, operation="live preview: an inserted clip's copy", source_file_id=src, extra={"ephemeral": True})
    have = (read_meta(src) or {}).get("live_copies") or {}
    write_meta(src, extra={"live_copies": {**have, clip_key(a, b, match, size, fps): {"media_id": fid}}})
    return fid


def _schedule_clips(keep: Any, missing: list[list[Any]], size: tuple[int, int], fps: float) -> list[str]:
    """Background copies for the inserted clips no copy holds yet (once per piece; the scene shows them as pending and
    the player's next scene has them). → the keys started or running."""
    import asyncio
    from app.tools import footage
    keys = []
    srcs = {m[2] for m in missing if m[2]}
    for x in footage.segs(keep):
        if x["src"] not in srcs:
            continue
        k = x["src"] + "|" + clip_key(x["a"], x["b"], x.get("match"), size, fps)
        keys.append(k)
        t = _CLIP_TASKS.get(k)
        if t is not None and not t.done():
            continue
        if t is not None and t.done() and t.exception() is None:
            continue                                  # (done: the registry has it — unless its file went away)

        async def go(x=x, k=k):
            try:
                await make_clip_copy(x["src"], x["a"], x["b"], x.get("match"), x.get("gain"), size, fps)
            except Exception as e:                    # noqa: BLE001 — logged; the range stays pending
                log.warning("live preview: the copy of the inserted clip %s failed: %s", k[:60], e)
                raise
        _CLIP_TASKS[k] = asyncio.get_running_loop().create_task(go())
    return keys


# ---------- the footage layer's transform (from the engine project) ----------

_PROPS = {"anchor": "anchor", "position": "position", "scale": "scale", "rotation": "rotation", "opacity": "opacity"}
_INTERP = {"Linear": "linear", "Bezier": "bezier", "Hold": "hold"}


def _val(v: Any) -> Any:
    x = (v or {}).get("v") if isinstance(v, dict) else v
    if isinstance(x, list):
        return [round(float(y), 6) for y in x[:2]]
    return round(float(x), 6) if isinstance(x, (int, float)) else x


def _children(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in node.get("children") or [] if isinstance(c, dict)]


def _ease(e: Any) -> list[list[float]]:
    return [[round(float(x.get("speed") or 0), 6), round(float(x.get("influence") or 0), 6)] for x in e or []]


def footage_transform(project: dict[str, Any], comp: str = "Video") -> dict[str, Any]:
    """The main composition's footage layer (named "Footage", a Footage source — fast_render.classify) in the project
    JSON → {"comp": [W, H], "fps", "bg": [r, g, b], "src": [w, h] (the copy's size in the project), "rest": {prop: value
    at 0 / static}, "keys": [{"prop", "t", "v", "in", "out", "ie", "oe", "si", "so"}], "remap": [[t, v]] | None,
    "moving": [[a, b]] (spans where the footage isn't at rest)}. Interpolation as stored: in / out ∈ linear | bezier |
    hold, ie / oe = temporal ease per dimension [[speed, influence]], si / so = spatial tangents (position)."""
    items = project.get("items") or {}
    main = next((it for it in items.values() if it.get("name") == comp and (it.get("kind") or {}).get("type") == "Comp"), None)
    if main is None:
        raise ValueError(f"the project has no composition {comp!r}")
    k = main["kind"]
    fr = k.get("frame_rate") or {}
    fps = float(fr.get("num") or 30) / float(fr.get("den") or 1)
    out: dict[str, Any] = {"comp": [int(k["width"]), int(k["height"])], "fps": round(fps, 6),
                           "bg": [round(float(x), 6) for x in (k.get("background") or [0, 0, 0])[:3]], "src": None,
                           "rest": {}, "keys": [], "remap": None, "moving": []}
    lay = next((L for L in k.get("layers") or [] if L.get("name") == "Footage" and (L.get("source") or {}).get("type") == "Footage"),
               None)
    if lay is None:
        raise ValueError("the project has no footage layer")
    src_item = items.get(str((lay.get("source") or {}).get("item"))) or {}
    sk = src_item.get("kind") or {}
    out["src"] = [int(sk.get("width") or 0), int(sk.get("height") or 0)]
    out["file"] = sk.get("path")
    root = lay.get("props") or {}
    for node in _children(root):
        if node.get("match") == "timeRemap" and node.get("keys"):
            out["remap"] = [[round(int(x["time"]) / TICKS, 6), _val(x.get("value"))] for x in node["keys"]]
        if node.get("match") != "transform":
            continue
        for p in _children(node):
            name = _PROPS.get(str(p.get("match")))
            if not name:
                continue
            out["rest"][name] = _val(p.get("value"))
            ks = p.get("keys") or []
            if ks:
                out["rest"][name] = _val(ks[0].get("value"))
            for x in ks:
                out["keys"].append({"prop": name, "t": round(int(x["time"]) / TICKS, 6), "v": _val(x.get("value")),
                                    "in": _INTERP.get(x.get("in_interp"), "linear"), "out": _INTERP.get(x.get("out_interp"), "linear"),
                                    "ie": _ease(x.get("in_ease")), "oe": _ease(x.get("out_ease")),
                                    "si": [round(float(v), 6) for v in (x.get("spatial_in") or [0, 0])[:2]],
                                    "so": [round(float(v), 6) for v in (x.get("spatial_out") or [0, 0])[:2]]})
    out["moving"] = moving(out)
    return out


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    try:
        return abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(a)), abs(float(b)))
    except (TypeError, ValueError):
        return a == b


def moving(tr: dict[str, Any]) -> list[list[float]]:
    """Where the footage isn't at its rest transform: between two keys of a property of which either is off rest
    (fast_render.moved, the same rule)."""
    by: dict[str, list[dict[str, Any]]] = {}
    for x in tr["keys"]:
        by.setdefault(x["prop"], []).append(x)
    out: list[list[float]] = []
    for prop, ks in by.items():
        r = tr["rest"].get(prop)
        for x, y in zip(ks, ks[1:]):
            if not _same(x["v"], r) or not _same(y["v"], r):
                out.append([x["t"], y["t"]])
        if ks and not _same(ks[-1]["v"], r):
            out.append([ks[-1]["t"], _BIG])
    out.sort()
    merged: list[list[float]] = []
    for a, b in out:
        if merged and a <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [[round(a, 6), round(min(b, _BIG), 6)] for a, b in merged]


def _bez(p1x: float, p1y: float, p2x: float, p2y: float, u: float) -> float:
    """y(x = u) on the cubic Bézier (0,0) (p1x,p1y) (p2x,p2y) (1,1) — x monotone (influences in 0…1)."""
    lo, hi = 0.0, 1.0
    for _ in range(60):
        s = (lo + hi) / 2
        x = 3 * (1 - s) ** 2 * s * p1x + 3 * (1 - s) * s * s * p2x + s ** 3
        if x < u:
            lo = s
        else:
            hi = s
    s = (lo + hi) / 2
    return 3 * (1 - s) ** 2 * s * p1y + 3 * (1 - s) * s * s * p2y + s ** 3


def value_at(tr: dict[str, Any], prop: str, t: float) -> Any:
    """A property's value at t (the same evaluation as live-player.js evalProp): before the first key its value, after
    the last the last; between two keys: hold (out = hold), linear (both sides linear), else the temporal Bézier —
    per dimension, the handles at influence × the span and speed × influence × the span (AE's model; with speed 0 the
    progress curve is (infl_out, 0) (1 − infl_in, 1) for every dimension, and a straight spatial path)."""
    ks = [x for x in tr["keys"] if x["prop"] == prop]
    if not ks:
        return tr["rest"].get(prop)
    if t <= ks[0]["t"]:
        return ks[0]["v"]
    if t >= ks[-1]["t"]:
        return ks[-1]["v"]
    i = max(j for j in range(len(ks)) if ks[j]["t"] <= t)
    a, b = ks[i], ks[i + 1]
    if a["out"] == "hold":
        return a["v"]
    dt = b["t"] - a["t"]
    u = (t - a["t"]) / dt if dt > 0 else 1.0
    va, vb = a["v"], b["v"]
    scalar = not isinstance(va, list)
    va_, vb_ = ([va], [vb]) if scalar else (va, vb)
    res = []
    for d in range(len(va_)):
        x0, x1 = float(va_[d]), float(vb_[d])
        if a["out"] == "linear" and b["in"] == "linear":
            res.append(x0 + (x1 - x0) * u)
            continue
        oe = (a["oe"][min(d, len(a["oe"]) - 1)] if a["oe"] else [0.0, 1 / 3]) if a["out"] == "bezier" else [(x1 - x0) / dt if dt else 0, 1 / 3]
        ie = (b["ie"][min(d, len(b["ie"]) - 1)] if b["ie"] else [0.0, 1 / 3]) if b["in"] == "bezier" else [(x1 - x0) / dt if dt else 0, 1 / 3]
        span = x1 - x0
        if abs(span) < 1e-12:
            res.append(x0)
            continue
        p1x, p2x = min(1.0, max(0.0, oe[1])), 1 - min(1.0, max(0.0, ie[1]))
        p1y = oe[0] * oe[1] * dt / span
        p2y = 1 - ie[0] * ie[1] * dt / span
        res.append(x0 + span * _bez(p1x, p1y, p2x, p2y, u))
    return res[0] if scalar else res


# ---------- the scene ----------

_PROJ_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def _project_file(pid: str, meta: dict[str, Any]) -> Path:
    """The project whose footage layer the scene reads: the version's caption-less preview project if it has one (the
    same footage layer, a fraction of the size), else the project itself; a project changed by hand in the editor:
    always itself."""
    from app.storage import output_path
    from app.tools import crafts
    pv = (meta.get("assets") or {}).get("preview.ecproj")
    if pv and not meta.get("hand_edited"):
        try:
            return crafts.existing(pv)
        except FileNotFoundError:
            pass
    return output_path(pid)


def _transform_of(path: Path) -> dict[str, Any]:
    st = path.stat()
    key = (st.st_mtime_ns, st.st_size)
    hit = _PROJ_CACHE.get(str(path))
    if hit and hit[0] == key:
        return hit[1]
    tr = footage_transform(json.loads(path.read_text("utf-8")))
    _PROJ_CACHE[str(path)] = (key, tr)
    while len(_PROJ_CACHE) > 32:
        _PROJ_CACHE.pop(next(iter(_PROJ_CACHE)))
    return tr


def _rel(url: str) -> str:
    """Absolute (PUBLIC_BASE_URL) → the path the page asks its own origin for."""
    for p in ("/api/", "/files/"):
        if p in url:
            return url[url.index(p):]
    return url


async def _part(mod: Any, name: str, pid: str, spec: dict[str, Any], ctx: dict[str, Any]) -> Any:
    """A lane's part (live_layers.layers_part / live_captions.captions_part): fn(pid, spec[, ctx=…]), sync or async.
    A failing lane never breaks the footage: its part is None (logged)."""
    fn: Optional[Callable[..., Any]] = getattr(mod, name, None) if mod is not None else None
    if fn is None:
        return None
    try:
        kw = {"ctx": ctx} if "ctx" in inspect.signature(fn).parameters else {}
        r = fn(pid, spec, **kw)
        return await r if inspect.isawaitable(r) else r
    except Exception as e:                       # noqa: BLE001 — the footage plays without this part
        log.warning("live scene: %s.%s(%s): %s", getattr(mod, "__name__", "?"), name, pid, e)
        return None


def _music(pid: str, opts: dict[str, Any], meta: dict[str, Any]) -> Optional[dict[str, Any]]:
    from app.storage import public_url
    from app.tools import crafts
    name = opts.get("music_src")
    if not name:
        return None
    fid = (meta.get("assets") or {}).get(name)
    try:
        crafts.existing(fid)
    except (FileNotFoundError, TypeError):
        return None
    return {"url": _rel(public_url(fid)), "start": float(opts.get("music_start") or 0), "level": opts.get("music_level")}


async def build(pid: str, *, prefer: str = "own") -> dict[str, Any]:
    """The version's scene JSON (see the module docstring and the plan). Raises LookupError for a project that isn't on
    the user's own footage, FileNotFoundError for a missing one."""
    from app.storage import output_path, public_url, read_meta
    from app.tools import footage
    if not pid.lower().endswith(".ecproj") or not output_path(pid).exists():
        raise FileNotFoundError(pid)
    meta = read_meta(pid) or {}
    spec = meta.get("spec") or {}
    base = spec.get("base") or {}
    if spec.get("kind") != "overlay" or not base:
        raise LookupError("The live preview is for videos on your own footage (overlay projects).")
    parent = None
    if base.get("proxy_pending") and meta.get("source_file_id"):
        parent = ((read_meta(meta["source_file_id"]) or {}).get("spec") or {}).get("base")
    fps = float(base.get("fps") or 30)
    fps = fps if 1 <= fps <= 120 else 30.0
    total = round(float(base.get("duration") or 0), 6)
    pw, ph = (int(base["proxy"]["w"]), int(base["proxy"]["h"])) if base.get("proxy") else \
        footage.size(int(base.get("width") or 1920), int(base.get("height") or 1080))
    fp = footage_plan(base, copies_of(base, parent=parent) + clip_copies(base.get("keep")), prefer=prefer)
    making = _schedule_clips(base.get("keep"), fp["missing"], (pw, ph), fps) if fp["missing"] else []
    copies = []
    for c in fp["copies"]:
        copies.append({"id": c["id"], "url": _rel(public_url(c["id"])), "w": c.get("w") or pw, "h": c.get("h") or ph,
                       "baked": c.get("adjust") or {},
                       "color": color_params(base.get("adjust"), c.get("adjust"), target_auto=base.get("auto"),
                                             baked_auto=c.get("auto"))})
    ranges = []
    for r in fp["ranges"]:
        rr = {**r, "url": copies[r["copy"]]["url"] if r.get("copy") is not None else None}
        if rr["url"] is None:
            rr["status"] = "pending"
        if r.get("clip"):            # an inserted clip: the main footage's colour recipe isn't on it (footage.timeline)
            rr["nocolor"] = True
        ranges.append(rr)
    card = base.get("end_card")
    plain = float((base.get("plain") or {}).get("duration") or 0) or fp["end"]
    hold_from = round(min(fp["end"], plain), 6) if card or total > fp["end"] + 0.5 / fps else None
    warnings = []
    try:
        tr = _transform_of(_project_file(pid, meta))
    except (OSError, ValueError, KeyError) as e:
        tr = None
        warnings.append(f"the footage transform couldn't be read: {str(e)[:120]}")
    if tr and tr.get("remap") and len(tr["remap"]) >= 2:
        t_last, v_last = tr["remap"][-1]
        if any(abs(float(t) - float(v)) > 1.5 / fps for t, v in tr["remap"]):
            warnings.append("the footage is retimed in the project (edited by hand?) — the live preview plays it unretimed")
    if fp["missing"]:
        warnings.append(f"{len(fp['missing'])} part(s) of the footage have no playable copy yet — the inserted clip's "
                        f"copy is being made ({len(making)} running); the page picks it up when it is ready")
    scene: dict[str, Any] = {
        "v": SCENE_V, "pid": pid, "fps": round(fps, 6), "W": pw, "H": ph, "total": total, "frames": round(total * fps),
        "footage": {"ranges": ranges, "copies": copies, "end": fp["end"], "hold_from": hold_from,
                    "pending": bool(base.get("proxy_pending")),
                    "adjust": recipe_of(base.get("adjust"), base.get("auto")) or None, "transform": tr},
        "captions": None, "layers": [], "audio": {"music": _music(pid, spec.get("opts") or {}, meta)},
        "warnings": warnings}
    ctx = {"fps": fps, "W": pw, "H": ph, "total": total, "comp": (tr or {}).get("comp"), "meta": meta}
    scene["captions"] = await _part(_live_captions, "captions_part_async" if hasattr(_live_captions, "captions_part_async")
                                    else "captions_part", pid, spec, ctx)
    scene["layers"] = await _part(_live_layers, "layers_part", pid, spec, ctx) or []
    scene["rev"] = revision(scene)
    return scene


def revision(scene: dict[str, Any]) -> str:
    """The scene's content hash without the signed tokens (they change with every call): the player's / poller's
    "did anything change"."""
    import hashlib
    import re as _re
    s = json.dumps({k: v for k, v in scene.items() if k != "rev"}, sort_keys=True, ensure_ascii=False)
    s = _re.sub(r"\?(p=[^\"&]*&)?t=[^\"]*", "", s)
    return hashlib.sha256(s.encode()).hexdigest()[:16]
