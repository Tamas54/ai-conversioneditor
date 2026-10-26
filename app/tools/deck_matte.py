"""The background deck's speaker in the FINAL video (N2 round 2): the person matte made on the server, only over the
background decks' spans, with the live preview's recipe — so the export looks like what the owner saw in the browser.

Recipe (RECIPE; app/static/live-player.js does the same per frame, app/tools/deck_layer.matte_part sends it):
  · the working copy's frame scaled to 256×144 → MediaPipe Selfie Segmenter landscape (the vendored model,
    app/static/vendor/mediapipe/, sha256 pinned) → the person confidence;
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

MODEL_NAME = "selfie_segmenter_landscape.tflite"
MODEL_SHA256 = "490e9ea734313e0de10fa0cd9e3c6133e36ea4db2b7a49bde9ef019f72796b8e"
RECIPE = {"v": 1, "model": "selfie_segmenter_landscape", "size": [256, 144], "ema": 0.5, "motion": [0.03, 0.1],
          "levels": [0.35, 0.75], "blur": 0.6}
SPEAKER = "Speaker "                       # the builder layer's name prefix (fast_render.classify reads it)
VENDORED = Path(__file__).resolve().parent.parent / "static" / "vendor" / "mediapipe" / MODEL_NAME
_CHECKED: dict[str, bool] = {}


def model_path() -> Path:
    """The model: MEDIAPIPE_MODELS/<name> when the image has it there, else the vendored copy (the one the browser uses)."""
    d = os.getenv("MEDIAPIPE_MODELS", "/opt/models")
    p = Path(d) / MODEL_NAME
    return p if p.exists() else VENDORED


def available() -> Optional[str]:
    """None when the server can make the matte; else what is missing."""
    try:
        from mediapipe.tasks.python import vision  # noqa: F401
    except Exception:  # noqa: BLE001
        return "MediaPipe isn't installed on this server"
    p = model_path()
    if not p.exists():
        return f"the person-matte model ({MODEL_NAME}) isn't installed on this server"
    key = f"{p}:{p.stat().st_size}:{p.stat().st_mtime_ns}"
    if key not in _CHECKED:
        _CHECKED[key] = hashlib.sha256(p.read_bytes()).hexdigest() == MODEL_SHA256
    return None if _CHECKED[key] else f"the person-matte model at {p} isn't the pinned one (sha256)"


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


def make(src: Path, fa: int, n: int, fps: float, dst: Path, recipe: dict[str, Any] = RECIPE) -> dict[str, Any]:
    """The matte clip of frames [fa, fa + n) of src (blocking: a thread). → timing."""
    import cv2
    import mediapipe as mp
    import numpy as np
    from mediapipe.tasks.python import BaseOptions, vision
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                                       "-of", "json", str(src)], capture_output=True, text=True, check=True).stdout)
    W, H = int(probe["streams"][0]["width"]), int(probe["streams"][0]["height"])
    sw, sh = (int(v) for v in recipe["size"])
    t0 = time.time()
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, (fa - 0.5) / fps):.6f}", "-i", str(src), "-frames:v", str(n),
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{W}x{H}", "-r", _fps_q(fps),
                            "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "10", "-pix_fmt", "yuv420p",
                            "-g", str(max(1, int(fps))), "-color_range", "tv", "-movflags", "+faststart", str(dst)],
                           stdin=subprocess.PIPE)
    opts = vision.ImageSegmenterOptions(base_options=BaseOptions(model_asset_path=str(model_path())),
                                        running_mode=vision.RunningMode.VIDEO, output_confidence_masks=True,
                                        output_category_mask=False)
    e, k, done, t_seg = None, float(recipe["ema"]), 0, 0.0
    last = None
    try:
        with vision.ImageSegmenter.create_from_options(opts) as seg:
            while done < n:
                buf = dec.stdout.read(W * H * 3)
                if len(buf) < W * H * 3:
                    break
                fr = np.frombuffer(buf, np.uint8).reshape(H, W, 3)
                small = cv2.resize(fr, (sw, sh), interpolation=cv2.INTER_AREA)
                a_ = time.time()
                res = seg.segment_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(small)),
                                            int(round(done * 1000 / fps)))
                a = res.confidence_masks[0].numpy_view().astype(np.float32).reshape(sh, sw)
                t_seg += time.time() - a_
                e = a.copy() if e is None else ema_step(e, a, k, recipe.get("motion"))
                last = alpha_of(e, W, H, recipe)
                enc.stdin.write(last.tobytes())
                done += 1
            while done < n and last is not None:              # (the copy ended early: its last frame stands, as in the project)
                enc.stdin.write(last.tobytes())
                done += 1
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
    return {"frames": done, "size": [W, H], "wall_s": round(wall, 2), "fps": round(done / max(wall, 1e-6), 1),
            "seg_ms": round(1000 * t_seg / max(done, 1), 2)}


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
