"""The background deck's speaker in the FINAL video (N2 round 2): the person matte made on the server, only over the
background decks' spans, with the live preview's recipe — so the export looks like what the owner saw in the browser.

Recipe (RECIPE; app/static/live-player.js does the same per frame, app/tools/deck_layer.matte_part sends it):
  · the working copy's frame scaled to 256×256 → MediaPipe Selfie Segmenter MULTICLASS (the vendored model,
    app/static/vendor/mediapipe/, sha256 pinned) → person = 1 − its background confidence (round 3; the browser makes
    the same mask for a still frame and uses it as the gate of its per-frame landscape mask while playing);
  · motion-aware time smoothing: e = w·e + (1 − w)·a, w = k·(1 − smoothstep(m0, m1, |a − e|)) — where the mask moves
    (a turning head) the new outline is taken at once, a still edge stays calm;
  · the five-tap softening of the live shader (centre ×2 + four diagonal bilinear taps at ±0.6 texel = a 3×3 kernel on the
    mask grid), bilinear up to the copy's size, levels smoothstep(l0, l1).
The clip: gray H.264 (Y = the matte), the working copy's size and frame rate, frame k = output frame Fa + k. Cached by
(the footage file's id — a file id never gets new content —, the span in frames, the frame rate, the recipe, the model's
sha256) under <DATA_DIR>/preview-cache/_mattes/<key>.mp4, registered in Postgres (preview_segments "m-<key>", the preview
cache's LRU). Building: `add_speakers` puts one builder item per background deck right after the deck's last slide —
video_builder.jsx speakerLayer: the footage layer's item again (so the final render's full-size swap takes it too) with
this clip as its LUMA track matte — and app/tools/fast_render.py draws that span with the engine (an opaque span).

Why these numbers (measured 10-26 on S4, 25:20–25:40, head and hand moving, against a reference mask of each drawn frame —
.claude/qa/nx-deck/leak.mjs; "leak" = the mask says person where the slide should show, % of the frame, moving frames):
round 1 (worker, plain EMA 0.5) 0.85–1.06 % mean, p95 2.5–3.7 %, the mask 1–11 frames behind its frame; the mask made
for the drawn frame itself (sync) with the plain EMA 0.39 %; no smoothing 0.04–0.06 % (but the edge flickers more);
smoothing only below a 0.03–0.1 change (this recipe) 0.09 %, p95 0.27 %, the flicker back near round 1's."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("aice.deck_matte")

# round 3 (measured on S4 against a BiRefNet-portrait reference, .claude/qa/nx-deck/r3): the MULTICLASS segmenter (person =
# 1 − its background class) — the landscape model took the framed picture beside the head for a person and left the light
# shoulders half see-through; the browser runs the landscape model per frame and this one as its semantic gate
MODEL_NAME = "selfie_multiclass_256x256.tflite"
MODEL_SHA256 = "c6748b1253a99067ef71f7e26ca71096cd449baefa8f101900ea23016507e0e0"
# + the landscape model's CONFIDENT person (>= 0.9) within 4 texels of the multiclass person fills its holes: on a blown-out
# cheek against the bright window the multiclass model cut a hole into the face (S4 25:03, 3.6 % of the person) — with
# the fill: holes 0.58 -> 0.19 %, loss 2.3 -> 1.4 %, leak 0.14 -> 0.23 % (of the frame)
LIVE_MODEL = "selfie_segmenter_landscape.tflite"
LIVE_SHA256 = "490e9ea734313e0de10fa0cd9e3c6133e36ea4db2b7a49bde9ef019f72796b8e"
# round 4 (the matte is made ONCE, offline, and both the live preview and the final video draw it): + the face oval of
# MediaPipe Face Landmarker (shrunk to 0.95) is always person — no hole in a blown-out face (holes 0.16 → 0.001 %);
# the multiclass model on every 2nd frame, interpolated (its cost halved: ~2.4× real time on this CPU for a span); the
# median of each frame and its two neighbours (both ways — no lag). Measured on the 28 reference frames: leak 0.28 %,
# loss 1.2 %, edge 0.12 (round 3's server recipe 0.27 / 1.39 / 0.12, holes up to 1.8 %)
FACE_MODEL = "face_landmarker.task"
FACE_SHA256 = "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"
RECIPE = {"v": 4, "model": "selfie_multiclass_256x256", "live_model": "selfie_segmenter_landscape", "size": [256, 256],
          "invert": True, "levels": [0.35, 0.75], "blur": 0.6, "every": 2, "median": 1,
          "fill": {"size": [256, 144], "min": 0.9, "r": 4}, "face": {"model": "face_landmarker", "scale": 0.95}}
SPEAKER = "Speaker "                       # the builder layer's name prefix (fast_render.classify reads it)
VENDORED = Path(__file__).resolve().parent.parent / "static" / "vendor" / "mediapipe" / MODEL_NAME
_CHECKED: dict[str, bool] = {}


def model_path(name: str = MODEL_NAME) -> Path:
    """A model: MEDIAPIPE_MODELS/<name> when the image has it there, else the vendored copy (the one the browser uses)."""
    p = Path(os.getenv("MEDIAPIPE_MODELS", "/opt/models")) / name
    return p if p.exists() else VENDORED.with_name(name)


def available() -> Optional[str]:
    """None when the server can make the matte; else what is missing."""
    try:
        from mediapipe.tasks.python import vision  # noqa: F401
    except Exception:  # noqa: BLE001
        return "MediaPipe isn't installed on this server"
    for name, sha in ((MODEL_NAME, MODEL_SHA256), (LIVE_MODEL, LIVE_SHA256), (FACE_MODEL, FACE_SHA256)):
        p = model_path(name)
        if not p.exists():
            return f"the person-matte model ({name}) isn't installed on this server"
        key = f"{p}:{p.stat().st_size}:{p.stat().st_mtime_ns}"
        if key not in _CHECKED:
            _CHECKED[key] = hashlib.sha256(p.read_bytes()).hexdigest() == sha
        if not _CHECKED[key]:
            return f"the person-matte model at {p} isn't the pinned one (sha256)"
    return None


# ---------- the recipe (numpy; = live-player.js emaStep + the speaker shader) ----------

def ema_step(e, a, k: float, motion: Optional[list[float]]):
    """One frame of the motion-aware smoothing (e is updated in place and returned)."""
    import numpy as np
    if motion:
        lo, span = float(motion[0]), max(1e-6, float(motion[1]) - float(motion[0]))
        x = np.clip((np.abs(a - e) - lo) / span, 0.0, 1.0)
        w = k * (1.0 - x * x * (3.0 - 2.0 * x))
    else:
        w = k
    e[...] = w * e + (1.0 - w) * a
    return e


def kernel(d: float = 0.6):
    """The shader's five taps (centre ×2, the four diagonals at ±d texel, bilinear) as a 3×3 kernel on the mask grid."""
    import numpy as np
    c = (2.0 + 4 * (1 - d) * (1 - d)) / 6.0
    edge = 2 * d * (1 - d) / 6.0
    corner = d * d / 6.0
    return np.array([[corner, edge, corner], [edge, c, edge], [corner, edge, corner]], dtype=np.float32)


def fill_holes(m, l, recipe: dict[str, Any] = RECIPE):
    """The multiclass mask m (0-1) + the landscape mask l where it is confident, near m's person (holes in a blown-out face)."""
    import cv2
    import numpy as np
    f = recipe.get("fill")
    if not f or l is None:
        return m
    r = int(f["r"])
    lu = cv2.resize(l.astype(np.float32), (m.shape[1], m.shape[0]), interpolation=cv2.INTER_LINEAR)
    near = cv2.dilate((m > 0.5).astype(np.uint8), np.ones((2 * r + 1, 2 * r + 1), np.uint8)) > 0
    return np.maximum(m, np.where(near & (lu >= float(f["min"])), lu, 0.0)).astype(np.float32)


def alpha_of(e, W: int, H: int, recipe: dict[str, Any] = RECIPE):
    """The smoothed mask (mask grid, 0–1) → the matte at W×H, 0–255 (softened, bilinear, levels)."""
    import cv2
    import numpy as np
    m = cv2.filter2D(e.astype(np.float32), -1, kernel(float(recipe["blur"])), borderType=cv2.BORDER_REPLICATE)
    m = cv2.resize(m, (W, H), interpolation=cv2.INTER_LINEAR)
    lo, hi = (float(v) for v in recipe["levels"])
    x = np.clip((m - lo) / (hi - lo), 0.0, 1.0)
    return (x * x * (3.0 - 2.0 * x) * 255.0 + 0.5).astype(np.uint8)


def _fps_q(fps: float) -> str:
    q = Fraction(fps).limit_denominator(1001)
    return f"{q.numerator}/{q.denominator}"


def face_oval(lm_xy, G: int, scale: float):
    """The face oval (36 normalized points) as a 0/1 mask on a G×G grid, shrunk / grown about its centre."""
    import cv2
    import numpy as np
    p = np.asarray(lm_xy, np.float32)
    c = p.mean(axis=0)
    p = c + (p - c) * scale
    img = np.zeros((G, G), np.uint8)
    cv2.fillPoly(img, [np.round(p * [G, G]).astype(np.int32)], 1)
    return img.astype(np.float32)


FACE_OVAL = [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400, 377, 152, 148, 176, 149, 150,
             136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109]


def make(src: Path, fa: int, n: int, fps: float, dst: Path, recipe: dict[str, Any] = RECIPE) -> dict[str, Any]:
    """The matte clip of frames [fa, fa + n) of src (blocking: a thread), OFFLINE: the whole span is known, so the time
    smoothing looks both ways and the heavy model may skip frames. Per frame: the multiclass person (every `every`-th frame,
    interpolated between), its holes filled from the landscape model's sure person, the face oval (Face Landmarker) always
    person; then the median of the frame and its two neighbours; softened, scaled to the copy, levels. → timing."""
    import cv2
    import mediapipe as mp
    import numpy as np
    from mediapipe.tasks.python import BaseOptions, vision
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                                       "-of", "json", str(src)], capture_output=True, text=True, check=True).stdout)
    W, H = int(probe["streams"][0]["width"]), int(probe["streams"][0]["height"])
    G = int(recipe["size"][0])
    every = max(1, int(recipe.get("every", 1)))
    t0 = time.time()
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, (fa - 0.5) / fps):.6f}", "-i", str(src), "-frames:v", str(n),
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{W}x{H}", "-r", _fps_q(fps),
                            "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "10", "-pix_fmt", "yuv420p",
                            "-g", str(max(1, int(fps))), "-color_range", "tv", "-movflags", "+faststart", str(dst)],
                           stdin=subprocess.PIPE)

    def seg_options(name: str):
        return vision.ImageSegmenterOptions(base_options=BaseOptions(model_asset_path=str(model_path(name))),
                                            running_mode=vision.RunningMode.VIDEO, output_confidence_masks=True,
                                            output_category_mask=False)
    fill, face = recipe.get("fill"), recipe.get("face")
    fw, fh = (int(v) for v in fill["size"]) if fill else (0, 0)
    ms = lambda i: int(round(i * 1000 / fps))                                       # noqa: E731
    pend: dict[int, dict[str, Any]] = {}       # frame → {l, oval} waiting for its multiclass neighbours
    mcs: dict[int, Any] = {}                   # frame → multiclass person (the frames it ran on)
    fused: dict[int, Any] = {}
    written, t_m = 0, {"mc": 0.0, "l": 0.0, "face": 0.0}
    last_mc = None

    def emit(i: int) -> None:                  # the median of i−1, i, i+1 (the ends: what there is) → the clip
        nonlocal written
        win = [fused[j] for j in (i - 1, i, i + 1) if j in fused]
        m = np.median(np.stack(win), axis=0) if len(win) > 1 else win[0]
        enc.stdin.write(alpha_of(m.astype(np.float32), W, H, recipe).tobytes())
        written += 1

    def finish(i: int) -> None:                # frame i's own person mask, once its multiclass value is known
        a = max(k for k in mcs if k <= i)
        b = min((k for k in mcs if k >= i), default=a)
        w = 0.0 if b == a else (i - a) / (b - a)
        m = (1 - w) * mcs[a] + w * mcs[b]
        x = pend.pop(i)
        if fill:
            m = fill_holes(m, x["l"], recipe)
        if x["oval"] is not None:
            m = np.maximum(m, x["oval"])
        fused[i] = m.astype(np.float32)

    try:
        with vision.ImageSegmenter.create_from_options(seg_options(MODEL_NAME)) as seg, \
                vision.ImageSegmenter.create_from_options(seg_options(LIVE_MODEL)) as seg_l, \
                vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
                    base_options=BaseOptions(model_asset_path=str(model_path(FACE_MODEL))), running_mode=vision.RunningMode.VIDEO,
                    num_faces=1)) as fl:
            i = 0
            while i < n:
                buf = dec.stdout.read(W * H * 3)
                if len(buf) < W * H * 3:
                    break
                fr = np.frombuffer(buf, np.uint8).reshape(H, W, 3)
                x: dict[str, Any] = {"l": None, "oval": None}
                if fill:
                    a_ = time.time()
                    sl = np.ascontiguousarray(cv2.resize(fr, (fw, fh), interpolation=cv2.INTER_AREA))
                    x["l"] = seg_l.segment_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=sl), ms(i)) \
                        .confidence_masks[0].numpy_view().astype(np.float32).reshape(fh, fw)
                    t_m["l"] += time.time() - a_
                if face:
                    a_ = time.time()
                    sf = np.ascontiguousarray(cv2.resize(fr, (480, int(round(480 * H / W))), interpolation=cv2.INTER_AREA))
                    r = fl.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=sf), ms(i))
                    if r.face_landmarks:
                        lm = r.face_landmarks[0]
                        x["oval"] = face_oval([[lm[k].x, lm[k].y] for k in FACE_OVAL], G, float(face["scale"]))
                    t_m["face"] += time.time() - a_
                pend[i] = x
                if i % every == 0 or i == n - 1:
                    a_ = time.time()
                    sm = np.ascontiguousarray(cv2.resize(fr, (G, G), interpolation=cv2.INTER_AREA))
                    c = seg.segment_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=sm), ms(i)) \
                        .confidence_masks[0].numpy_view().astype(np.float32).reshape(G, G)
                    mcs[i] = 1.0 - c if recipe.get("invert") else c
                    t_m["mc"] += time.time() - a_
                    for j in sorted(k for k in pend if k <= i):
                        finish(j)
                    for j in sorted(k for k in fused if k <= i - 1 and k >= written):
                        emit(j)
                    for k in [k for k in mcs if k < i - every]:
                        mcs.pop(k)
                    for k in [k for k in fused if k < written - 1]:
                        fused.pop(k)
                    last_mc = i
                i += 1
            for j in sorted(k for k in pend):         # (the copy ended early)
                mcs.setdefault(j, mcs[max(mcs)])
                finish(j)
            for j in sorted(k for k in fused if k >= written):
                emit(j)
            last = alpha_of(fused[max(fused)], W, H, recipe) if fused else None
            while written < n and last is not None:   # (the copy ended early: its last frame stands, as in the project)
                enc.stdin.write(last.tobytes())
                written += 1
    finally:
        try:
            enc.stdin.close()
        except OSError:
            pass
        enc.wait()
        dec.kill()
        dec.wait()
    if enc.returncode != 0 or not dst.exists():
        raise RuntimeError("the speaker matte couldn't be encoded")
    wall = time.time() - t0
    return {"frames": written, "size": [W, H], "wall_s": round(wall, 2), "fps": round(written / max(wall, 1e-6), 1),
            "x_realtime": round(wall / max(written / fps, 1e-6), 2),
            "ms": {k: round(1000 * v / max(written, 1), 1) for k, v in t_m.items()}, "seg_ms": round(1000 * sum(t_m.values()) / max(written, 1), 1)}


# ---------- the cache ----------

_LOCKS: dict[str, tuple[Any, asyncio.Lock]] = {}


def _lock(key: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    hit = _LOCKS.get(key)
    if hit is None or hit[0] is not loop:
        hit = (loop, asyncio.Lock())
        _LOCKS[key] = hit
    return hit[1]


def key_of(media: str, fa: int, n: int, fps: float, recipe: dict[str, Any] = RECIPE) -> str:
    return hashlib.sha256(json.dumps({"media": media, "fa": fa, "n": n, "fps": _fps_q(fps), "recipe": recipe,
                                      "model": MODEL_SHA256}, sort_keys=True).encode()).hexdigest()


async def matte(src: Path, media: str, fa: int, n: int, fps: float) -> tuple[Path, dict[str, Any]]:
    """The cached matte clip (made now if missing). media: the footage file's identity (its file id)."""
    from app.tools import preview_cache as pc
    key = key_of(media, fa, n, fps)
    dst = pc.matte_path(key)
    if dst.exists():
        await pc.touch(["m-" + key], "_mattes")
        return dst, {"cached": True}
    async with _lock(key):
        if dst.exists():
            return dst, {"cached": True}
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f"{key}.tmp-{uuid.uuid4().hex[:6]}.mp4")
        try:
            stat = await asyncio.to_thread(make, src, fa, n, fps, tmp)
            os.replace(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)
        await pc.register("m-" + key, "_mattes", dst.stat().st_size)
        log.info("speaker matte %s: %d frame(s) at %s fps (seg %.1f ms/frame)", key[:12], stat["frames"], stat["fps"], stat["seg_ms"])
        return dst, {"cached": False, **stat}


# ---------- the builder ----------

def spans_of(D: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """The background decks' carriers in D.layers → [(index of the deck's last item, carrier)]."""
    out = []
    items = D.get("layers") or []
    for i, it in enumerate(items):
        dk = it.get("deck") or {}
        if dk.get("role") == "carrier" and dk.get("placement") == "background":
            last = i
            for j in range(i + 1, len(items)):
                if str(items[j].get("id") or "").split(".")[0] == it["id"] and not items[j].get("speaker"):
                    last = j
            out.append((last, it))
    return out


async def add_speakers(D: dict[str, Any], work: Path, footage_file: str, media: str) -> tuple[list[str], list[str]]:
    """For every background deck: the matte clip into the build folder and a speaker item after the deck's last item.
    → (the asset names to keep with the project, findings). Without MediaPipe: nothing, and a finding."""
    sp = spans_of(D)
    if not sp:
        return [], []
    why = available()
    if why:
        return [], [f"The background deck(s) — {why}: the final video shows the slides over you there (like placement \"front\")."]
    fps = float(D["fps"])
    total_f = round(float(D["total"]) * fps)
    names, notes = [], []
    for last, car in sorted(sp, key=lambda x: -x[0]):       # from the end: the inserts don't move the earlier indexes
        fa = max(0, round(float(car["at"]) * fps))
        fb = min(total_f, round((float(car["at"]) + float(car["dur"])) * fps))
        if fb - fa < 2:
            continue
        try:
            clip, st = await matte(work / footage_file, media, fa, fb - fa, fps)
        except Exception as e:                                # noqa: BLE001 — the video still renders, the check says so
            log.warning("speaker matte for %s failed: %s", car["id"], e)
            notes.append(f"Layer {car['id']} (deck, background): the speaker couldn't be cut out ({str(e)[:120]}) — the final "
                         "video shows the slides over you there.")
            continue
        name = f"speaker-{clip.stem[:16]}.mp4"
        if not (work / name).exists():
            try:
                os.link(clip, work / name)
            except OSError:
                import shutil
                shutil.copyfile(clip, work / name)
        D["layers"].insert(last + 1, {"id": f"{car['id']}.speaker", "name": f"{SPEAKER}{car['id']}",
                                      "speaker": {"file": name, "at": round(fa / fps, 6), "dur": round((fb - fa) / fps, 6)}})
        names.append(name)
    return names, notes


# ---------- the live preview: the same clip, made in the background (app/tools/live_scene.py, live-player.js) ----------

_TASKS: dict[str, Any] = {}
_GATE: list[Any] = []                  # (loop, semaphore): one matte at a time — it takes most of the CPU for minutes


def _gate() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if not _GATE or _GATE[0][0] is not loop:
        _GATE[:] = [(loop, asyncio.Semaphore(1))]
    return _GATE[0][1]


def _sig_key(key: str) -> str:
    return "M:" + key


def clip_url(pid: str, key: str) -> str:
    """The matte clip's signed address (= live_layers.clip_url, its own key space)."""
    from app.config import settings
    from app.tools import preview_serve as ps
    uid = ps._workspace(pid)
    exp = (int(time.time()) // 3600 + 1) * 3600 + settings.FILE_TTL_HOURS * 3600
    return f"/api/video/live/matte/{key}.mp4{ps._query(pid, _sig_key(key), exp, uid)}"


def verify(pid: str, key: str, token: str, uid: str) -> bool:
    from app.tools import preview_serve as ps
    return ps.verify(pid, _sig_key(key), token, uid)


def _spans_live(spec: dict[str, Any]) -> list[tuple[str, int, int]]:
    """The version's background decks → [(deck id, first output frame, frame count)] (= add_speakers' frames)."""
    base = spec.get("base") or {}
    fps = float(base.get("fps") or 30)
    total_f = round(float(base.get("duration") or 0) * fps)
    out = []
    for ly in ((spec.get("story") or {}).get("layers") or []):
        if ly.get("type") == "deck" and ly.get("placement") == "background":
            fa = max(0, round(float(ly["at"]) * fps))
            fb = min(total_f, round((float(ly["at"]) + float(ly["dur"])) * fps))
            if fb - fa >= 2:
                out.append((str(ly["id"]), fa, fb - fa))
    return out


async def live_clips(pid: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """The background decks' matte clips for the live scene: [{id, t0, t1, f0, frames, key, status, url|null}]; a missing
    one starts being made (in the background, one at a time). The working copy must be the version's own (not a stand-in
    while a cut is pending: the matte is frame-locked to the copy it was made from)."""
    from app.tools import crafts, preview_cache as pc, video
    base = spec.get("base") or {}
    spans = _spans_live(spec)
    if not spans:
        return []
    fps = float(base.get("fps") or 30)
    why = available()
    media = video.assets_of(pid).get(base.get("asset") or "") or base.get("media_id")
    out = []
    for did, fa, n in spans:
        row: dict[str, Any] = {"id": did, "f0": fa, "frames": n, "t0": round(fa / fps, 6), "t1": round((fa + n) / fps, 6),
                               "url": None}
        if why or base.get("proxy_pending") or not media:
            row["status"] = "unavailable"
            row["reason"] = why or "the editor's copy of this version is still being made"
            out.append(row)
            continue
        key = key_of(str(media), fa, n, fps)
        row["key"] = key
        if pc.matte_path(key).exists():
            row.update(status="ready", url=clip_url(pid, key))
        else:
            t = _TASKS.get(key)
            if t is not None and t.done() and t.exception() is not None:
                row.update(status="failed", reason=str(t.exception())[:160])
            else:
                if t is None or t.done():
                    src = crafts.existing(media)

                    async def run(src=src, media=str(media), fa=fa, n=n):
                        from app.convert.engines import background
                        async with _gate():
                            with background():
                                return await matte(src, media, fa, n, fps)
                    _TASKS[key] = asyncio.get_running_loop().create_task(run())
                row["status"] = "making"
                row["eta_s"] = round(n / fps * 2.6)
        out.append(row)
    return out
