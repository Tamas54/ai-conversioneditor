"""Videó háttér nélkül (10-22): zöld / kék háttér kulcsolása — AI és letöltött modell nélkül, ffmpeg chromakey + despill.

  1. a háttér felismerése: néhány kocka szélsávjának színe (a képernyő egyszínű, telített zöld vagy kék, a szélek
     nagy részét kitölti); megadható kézzel is (key: green | blue | "#RRGGBB");
  2. a kulcsolás erőssége a mérésből: a szélsáv képpontjainak UV-távolsága a kulcsszíntől (a chromakey saját mértéke:
     √(du² + dv²) / (255·√2)) — a 95. percentilis fölé, hogy a gyűrött / árnyékos vászon is eltűnjön, az alany ne;
  3. kimenet: új háttér (szín, kép, videó — stock is) MP4-ben, és/vagy átlátszó videó (WebM VP9 / ProRes 4444 .mov);
     a check egy dekódolt kocka alfájából mondja meg, mennyi lett átlátszó (a szélsávban és a kép közepén).

Zöld vászon nélküli felvételhez (az alany mögött szoba, utca) mesterséges matting kell — az külön döntés (licenc).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from app.config import settings

log = logging.getLogger("aice.video_key")

KEYS = {"green": "#00B140", "blue": "#0047BB"}           # a szokásos chroma-zöld / -kék (a felismerés a valódit méri)
MAX_SECONDS = 600.0
MOV_MAX_SECONDS = 120.0                                 # a ProRes 4444 nagy (~40 MB/s 1080p-n)


class KeyError_(ValueError):
    pass


def _hex(rgb) -> str:
    return "#" + "".join(f"{int(round(c)):02X}" for c in rgb[:3])


def _uv(rgb):
    """BT.601 (full range) RGB → U, V (0–255) — numpy tömbön is."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    u = -0.168736 * r - 0.331264 * g + 0.5 * b + 128
    v = 0.5 * r - 0.418688 * g - 0.081312 * b + 128
    return u, v


def screen_of(frames: list) -> Optional[dict[str, Any]]:
    """Kockák (H×W×3 uint8 numpy) → a háttérvászon: {color, kind, share, similarity} vagy None (nincs egyszínű, telített
    zöld/kék szélsáv). share = a szélsáv mekkora része ilyen színű; similarity = a chromakey küszöbe."""
    import colorsys
    import numpy as np
    border = []
    for f in frames:
        h, w = f.shape[:2]
        bh, bw = max(2, int(h * 0.06)), max(2, int(w * 0.06))
        border += [f[:bh].reshape(-1, 3), f[-bh:].reshape(-1, 3), f[:, :bw].reshape(-1, 3), f[:, -bw:].reshape(-1, 3)]
    px = np.concatenate(border).astype(np.float32)
    med = np.median(px, axis=0)
    hh, ss, vv = colorsys.rgb_to_hsv(*(med / 255))
    deg = hh * 360
    kind = "green" if 70 <= deg <= 170 else "blue" if 190 <= deg <= 250 else None
    if kind is None or ss < 0.35 or vv < 0.2:
        return None
    u, v = _uv(px)
    ku, kv = _uv(med[None])
    d = np.sqrt((u - ku[0]) ** 2 + (v - kv[0]) ** 2) / (255 * np.sqrt(2))
    close = d < 0.12
    share = float(close.mean())
    if share < 0.55:
        return None
    sim = float(np.percentile(d[close], 95)) * 1.25 + 0.02
    return {"color": _hex(med), "kind": kind, "share": round(share, 3), "similarity": round(min(0.3, max(0.06, sim)), 3)}


def _kind_of(color: str) -> Optional[str]:
    import colorsys
    h, s_, v = colorsys.rgb_to_hsv(*(int(color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)))
    return "green" if 70 <= h * 360 <= 170 and s_ > 0.2 else "blue" if 190 <= h * 360 <= 250 and s_ > 0.2 else None


def key_of(raw: Any) -> Optional[str]:
    """key: auto (alap) | green | blue | "#RRGGBB" → a kulcsszín vagy None (= felismerés)."""
    k = str(raw or "auto").strip().lower()
    if k in ("auto", ""):
        return None
    if k in KEYS:
        return KEYS[k]
    if re.fullmatch(r"#?[0-9a-f]{6}", k):
        return "#" + k.lstrip("#").upper()
    raise KeyError_('key: "auto" (found from the frame edges) | "green" | "blue" | "#RRGGBB" (the screen colour).')


async def _frames(src: Path, dur: float, work: Path, n: int = 3) -> list:
    import numpy as np
    from PIL import Image
    from app.convert.engines import run
    out = []
    for i in range(n):
        t = dur * (0.15 + 0.7 * i / max(1, n - 1))
        png = work / f"probe{i}.png"
        await run("ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1", "-vf", "scale=480:-2",
                  str(png), timeout=120)
        if png.exists():
            out.append(np.asarray(Image.open(png).convert("RGB")))
    return out


def _filters(color: str, similarity: float, kind: Optional[str]) -> str:
    c = color.lstrip("#")
    spill = f",despill=type={'blue' if kind == 'blue' else 'green'}:mix=0.6:expand=0.1" if kind else ""
    return f"chromakey=color=0x{c}:similarity={similarity:.3f}:blend=0.06{spill}"


def background_of(raw: Any) -> Any:
    """background: "transparent" | "none" (csak az átlátszó videó) | "#RRGGBB" | kép (file_id | URL | {"stock": szavak})
    | videó (egy felvétel file_id-ja | {"stock_video": szavak})."""
    from app.tools import video
    if raw in (None, "", "none", "transparent", False):
        return None
    if isinstance(raw, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", raw.strip()):
        return {"color": raw.strip().upper()}
    return video._img_spec(raw)


async def _background_file(spec: Any, W: int, H: int, work: Path) -> tuple[list[str], str, Optional[str]]:
    """A háttér ffmpeg-bemenete → (bemeneti argumentumok, a [1:v] lánca, a forrás kreditje)."""
    from app.tools import images as imgs, video
    if isinstance(spec, dict) and spec.get("color"):
        return (["-f", "lavfi", "-i", f"color=c=0x{spec['color'].lstrip('#')}:s={W}x{H}:r=30"],
                "[1:v]setsar=1", None)
    fit = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"
    credit = None
    movie = None
    if isinstance(spec, dict) and spec.get("stock_video"):
        from app.tools import stock_video
        try:
            movie, credit = await stock_video.fetch(spec, short=min(W, H), orientation="portrait" if H > W * 1.2 else
                                                    "landscape" if W > H * 1.2 else "square")
        except stock_video.StockVideoError as e:
            raise ValueError(str(e)) from None
    elif video._movie_src(spec):
        movie = video._movie_src(spec)
    if movie:
        from app.tools import crafts
        return ["-stream_loop", "-1", "-i", str(crafts.existing(movie))], f"[1:v]{fit},fps=30", credit
    r = await imgs.resolve(spec, aspect=W / H, max_px=max(W, H) * 2)
    return ["-loop", "1", "-i", str(r.path)], f"[1:v]{fit}", r.attribution


async def cutout(body: dict[str, Any], progress: Optional[Callable[..., None]] = None) -> dict[str, Any]:
    """{file_id: felvétel, background?: lásd background_of (alap: transparent), key?: auto | green | blue | #hex,
    alpha?: webm (alap, ha nincs háttér) | mov | both} → az új hátterű MP4 és/vagy az átlátszó videó + check."""
    from app.convert.engines import run
    from app.storage import display_label, new_file_id, output_path, public_url, temp_path, write_meta
    from app.storage.naming import result_stem
    from app.tools import crafts, mediaprobe, video
    step = progress or (lambda *a, **k: None)
    fid = str(body.get("file_id") or "")
    try:
        src = crafts.existing(fid)
    except FileNotFoundError:
        raise ValueError("cutout: file_id = a video filmed in front of a green or blue screen.") from None
    if src.suffix.lstrip(".").lower() not in video.MOVIE_EXTS:
        raise ValueError(f"cutout takes a video (mp4, mov, webm, m4v) — not .{src.suffix.lstrip('.')}.")
    forced = key_of(body.get("key"))
    bg = background_of(body.get("background"))
    kinds = video.alpha_kinds(body.get("alpha")) if body.get("alpha") not in (None, "") else ([] if bg else ["webm"])
    mi = await mediaprobe.info(src)
    W, H = int(mi.get("display_width") or mi.get("width") or 0), int(mi.get("display_height") or mi.get("height") or 0)
    dur = float(mi.get("duration") or 0)
    if not W or dur < 0.5:
        raise ValueError("cutout: that video has no picture (or is shorter than half a second).")
    if dur > MAX_SECONDS:
        raise ValueError(f"cutout: up to {MAX_SECONDS / 60:.0f} minutes at a time — trim it first (file_tools trim-video).")
    if "mov" in kinds and dur > MOV_MAX_SECONDS:
        raise ValueError(f"A ProRes 4444 .mov of {dur:.0f} s would be several GB — use alpha=\"webm\" (or a part ≤ "
                         f"{MOV_MAX_SECONDS:.0f} s).")
    work = temp_path(f"cutout_{os.urandom(5).hex()}")
    work.mkdir(parents=True)
    findings: list[str] = []
    verified: list[str] = []
    files: list[dict[str, Any]] = []
    try:
        step(0, 4, "finding the screen")
        frames = await _frames(src, dur, work)
        found = screen_of(frames) if frames else None
        if forced:
            kind = _kind_of(forced)
            if found and found["kind"] == kind and str(body.get("key")).lower() in KEYS:
                screen = found                               # „green”: a valódi zöldje, a mért küszöbbel
            else:
                screen = {"color": forced, "kind": kind, "share": None,
                          "similarity": found["similarity"] if found and found["kind"] == kind else 0.14}
        elif found:
            screen = found
        else:
            raise ValueError("cutout: no green or blue screen was found around the edges of this video. Background removal "
                             "here works with a screen behind the person (give key=\"#RRGGBB\" if it's another colour); "
                             "removing a room or street behind someone needs AI matting, which isn't switched on yet.")
        verified.append(f"The {screen['kind'] or 'coloured'} screen ({screen['color']}"
                        + (f", {screen['share'] * 100:.0f}% of the frame edges" if screen.get("share") else "")
                        + f") is keyed out (similarity {screen['similarity']:.2f}), with its colour spill taken off the edges.")
        chain = _filters(screen["color"], screen["similarity"], screen.get("kind"))
        stem = result_stem(fid, "cut out")
        a_in = ["-map", "0:a:0?"]
        if bg is not None:
            step(1, 4, "the new background")
            b_args, b_chain, credit = await _background_file(bg, W, H, work)
            out = work / "out.mp4"
            fc = f"{b_chain}[bg];[0:v]scale={W}:{H},setsar=1,{chain},format=yuva420p[fg];[bg][fg]overlay=shortest=1:format=auto,format=yuv420p[v]"
            await run("ffmpeg", "-v", "error", "-y", "-i", str(src), *b_args, "-filter_complex", fc, "-map", "[v]", *a_in,
                      "-t", f"{dur:.3f}", "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
                      "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out), timeout=settings.VIDEO_TIMEOUT_S)
            vid = new_file_id("mp4")
            shutil.move(str(out), output_path(vid))
            write_meta(vid, label=f"{stem} – new background.mp4", operation="video cutout", source_file_id=fid,
                       extra={"screen": screen, **({"credit": credit} if credit else {})})
            files.append({**video._file_entry(vid), "what": "on the new background"})
            if credit:
                verified.append(f"Background: {credit}.")
        for ext in kinds:
            step(2, 4, f"the transparent .{ext}")
            out = work / f"alpha.{ext}"
            enc = (["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "30", "-row-mt", "1", "-auto-alt-ref", "0",
                    "-c:a", "libopus", "-b:a", "128k"] if ext == "webm" else
                   ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-c:a", "pcm_s16le"])
            await run("ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", f"{chain},format=yuva420p" if ext == "webm"
                      else f"{chain},format=yuva444p10le", "-map", "0:v:0", *a_in, "-t", f"{dur:.3f}", *enc, str(out),
                      timeout=settings.VIDEO_TIMEOUT_S)
            proof = await video._alpha_proof(out, dur / 2)
            aid = new_file_id(ext)
            shutil.move(str(out), output_path(aid))
            write_meta(aid, label=f"{stem} – transparent.{ext}", operation="video cutout", source_file_id=fid,
                       extra={"screen": screen, "alpha": proof})
            files.append({**video._file_entry(aid), "what": "transparent", "format": "WebM VP9" if ext == "webm" else
                          "ProRes 4444 .mov", "alpha": proof["ok"]})
            if proof.get("transparent") is not None:
                verified.append(f"Transparent .{ext}: at {proof['at']:.1f} s {proof['transparent'] * 100:.0f}% of the frame is "
                                f"see-through, {proof['graphics'] * 100:.0f}% is the person / subject.")
            if proof.get("graphics") is not None and proof["graphics"] < 0.02:
                findings.append("Almost nothing is left after keying — is the subject the same colour as the screen? Give "
                                "key=\"#RRGGBB\" with the screen's exact colour.")
            elif (proof.get("transparent") or 0) < 0.2:
                findings.append("Less than a fifth of the frame became transparent — the screen may be unevenly lit; check "
                                "the result, or give key=\"#RRGGBB\".")
            elif not proof["ok"]:
                findings.append(f"The .{ext} came out without a usable alpha channel — try alpha=\"{'mov' if ext == 'webm' else 'webm'}\".")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    main = files[0] if files else None
    return {"files": files, "video": main, "screen": screen, "source": display_label(fid),
            "check": {"status": "needs_check" if findings else "ok", "findings": findings, "verified": verified}}
