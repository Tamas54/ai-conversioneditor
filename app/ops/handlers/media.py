"""Videó- és hangeszközök FFmpeggel: tömörítés, vágás, kivágás, átméretezés, forgatás, némítás, GIF, hangkinyerés,
felirat, hangerő. Minden paraméter validált (app/convert/options.py), a parancssor argumentumlista."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Optional

from app.convert.engines import run
from app.convert.options import OptionError
from app.convert.steps import VIDEO_TIMEOUT, _RES, _has_audio, _has_video, probe
from app.ops.handlers import out_name

_FF = ("ffmpeg", "-y", "-hide_banner", "-loglevel", "error")


def _fmt(p: Path) -> str:
    from app.catalog.formats import from_filename
    return from_filename(p.name)


def _vcodec_for(fmt: str) -> list[str]:
    if fmt == "webm":
        return ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32", "-row-mt", "1"]
    if fmt in ("avi", "divx"):
        return ["-c:v", "mpeg4", "-q:v", "3"]
    if fmt in ("mpeg", "mpg", "vob"):
        return ["-c:v", "mpeg2video", "-q:v", "3"]
    if fmt in ("wmv", "asf"):
        return ["-c:v", "wmv2", "-q:v", "4"]
    if fmt == "ogv":
        return ["-c:v", "libtheora", "-q:v", "7"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]


def _acodec_for(fmt: str) -> list[str]:
    if fmt == "webm":
        return ["-c:a", "libopus"]
    if fmt in ("avi", "divx"):
        return ["-c:a", "libmp3lame"]
    if fmt in ("mpeg", "mpg"):
        return ["-c:a", "mp2"]
    if fmt in ("wmv", "asf"):
        return ["-c:a", "wmav2"]
    if fmt in ("ogv", "ogg"):
        return ["-c:a", "libvorbis"]
    from app.convert.steps import _AUDIO_DEF
    if fmt in _AUDIO_DEF:
        return list(_AUDIO_DEF[fmt][0])
    return ["-c:a", "aac", "-b:a", "160k"]


def _faststart(fmt: str) -> list[str]:
    return ["-movflags", "+faststart"] if fmt in ("mp4", "m4v", "mov") else []


async def _video(src: Path, dst: Path, vf: Optional[list[str]] = None, extra: Optional[list[str]] = None,
                 pre: Optional[list[str]] = None, audio: bool = True, af: Optional[list[str]] = None) -> Path:
    info = await probe(src)
    fmt = _fmt(dst)
    args = [*_FF, *(pre or []), "-i", str(src)]
    if vf:
        args += ["-vf", ",".join(vf + ["scale=trunc(iw/2)*2:trunc(ih/2)*2"])]
    args += _vcodec_for(fmt)
    if audio and _has_audio(info):
        args += _acodec_for(fmt) + (["-af", ",".join(af)] if af else [])
    else:
        args.append("-an")
    args += (extra or []) + _faststart(fmt) + [str(dst)]
    await run(*args, timeout=VIDEO_TIMEOUT, cwd=dst.parent)
    return dst


# ---------- tömörítés ----------

async def compress_video(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    res = []
    for src in inputs:
        fmt = _fmt(src) if _fmt(src) in ("mp4", "mov", "mkv", "webm", "m4v") else "mp4"
        dst = out_name(out, src, "-compressed", fmt)
        info = await probe(src)
        dur = float(info.get("format", {}).get("duration") or 0)
        vf = []
        if o.get("resolution"):
            wh = _RES.get(o["resolution"], o["resolution"].replace("x", ":"))
            vf.append(f"scale={wh}:force_original_aspect_ratio=decrease")
        codec = o.get("video_codec", "h264")
        if fmt == "webm":
            vargs = ["-c:v", "libvpx-vp9", "-row-mt", "1"]
        elif codec == "h265":
            vargs = ["-c:v", "libx265", "-tag:v", "hvc1", "-preset", "medium"]
        else:
            vargs = ["-c:v", "libx264", "-preset", "medium"]
        if o.get("target_size_mb") and dur > 0:
            kbit = max(100, int(o["target_size_mb"] * 8192 / dur) - 128)
            rate = ["-b:v", f"{kbit}k", "-maxrate", f"{int(kbit * 1.3)}k", "-bufsize", f"{kbit * 2}k"]
        else:
            rate = ["-crf", str(o.get("crf", 28 if codec != "h265" else 30))] + (["-b:v", "0"] if fmt == "webm" else [])
        args = [*_FF, "-i", str(src)]
        if vf:
            args += ["-vf", ",".join(vf + ["scale=trunc(iw/2)*2:trunc(ih/2)*2"])]
        args += vargs + rate + ["-pix_fmt", "yuv420p"]
        args += (["-c:a", "libopus", "-b:a", "96k"] if fmt == "webm" else ["-c:a", "aac", "-b:a", "128k"]) if _has_audio(info) else ["-an"]
        args += _faststart(fmt) + [str(dst)]
        await run(*args, timeout=VIDEO_TIMEOUT)
        explicit = o.get("resolution") or o.get("target_size_mb") or o.get("video_codec") not in (None, "auto")
        if dst.stat().st_size >= src.stat().st_size and fmt == _fmt(src) and not explicit:
            shutil.copy(src, dst)          # nem lett kisebb, és mást nem kértek: az eredeti marad
        res.append(dst)
    return res


async def compress_mp3(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    res = []
    for src in inputs:
        dst = out_name(out, src, "-compressed", "mp3")
        args = [*_FF, "-i", str(src), "-vn", "-c:a", "libmp3lame", "-b:a", o.get("audio_bitrate") or "96k"]
        if o.get("channels"):
            args += ["-ac", str(o["channels"])]
        await run(*args, str(dst), timeout=VIDEO_TIMEOUT)
        res.append(dst)
    return res


async def compress_wav(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    res = []
    for src in inputs:
        fmt = _fmt(src) if _fmt(src) in ("wav", "aiff", "flac") else "wav"
        dst = out_name(out, src, "-compressed", fmt)
        bits = int(o.get("bit_depth") or 16)
        codec = {"wav": f"pcm_s{bits}le", "aiff": f"pcm_s{bits}be", "flac": "flac"}[fmt]
        args = [*_FF, "-i", str(src), "-vn", "-c:a", codec, "-ar", str(o.get("sample_rate") or 22050), "-ac", str(o.get("channels") or 1)]
        if fmt == "flac":
            args += ["-sample_fmt", "s16" if bits == 16 else "s32", "-compression_level", "12"]
        await run(*args, str(dst), timeout=VIDEO_TIMEOUT)
        res.append(dst)
    return res


# ---------- vágás és geometria ----------

def _t(v) -> str:
    return str(v)


async def trim(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    if not o.get("start") and not o.get("end"):
        raise OptionError("give start and/or end (seconds or hh:mm:ss)")
    res = []
    for src in inputs:
        dst = out_name(out, src, "-trimmed")
        info = await probe(src)
        pre = (["-ss", _t(o["start"])] if o.get("start") else []) + (["-to", _t(o["end"])] if o.get("end") else [])
        if _has_video(info):
            await _video(src, dst, pre=pre)
        else:
            af = []
            dur = _dur_after(info, o)
            if o.get("fade_in"):
                af.append(f"afade=t=in:st=0:d={o['fade_in']}")
            if o.get("fade_out") and dur:
                af.append(f"afade=t=out:st={max(0.0, dur - o['fade_out'])}:d={o['fade_out']}")
            args = [*_FF, *pre, "-i", str(src), "-vn", *_acodec_for(_fmt(dst))] + (["-af", ",".join(af)] if af else [])
            await run(*args, str(dst), timeout=VIDEO_TIMEOUT)
        res.append(dst)
    return res


def _secs(v) -> float:
    s = str(v)
    if ":" in s:
        parts = [float(x) for x in s.split(":")]
        return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
    return float(s)


def _dur_after(info: dict, o: dict) -> float:
    total = float(info.get("format", {}).get("duration") or 0)
    a = _secs(o["start"]) if o.get("start") else 0.0
    b = _secs(o["end"]) if o.get("end") else total
    return max(0.0, min(b, total) - a)


async def crop_video(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    if o.get("box"):
        x, y, w, h = [int(float(v)) for v in o["box"][:4]]
        vf = [f"crop={w}:{h}:{x}:{y}"]
    elif o.get("aspect"):
        a, b = (int(v) for v in str(o["aspect"]).split(":"))
        vf = [f"crop='min(iw,ih*{a}/{b})':'min(ih,iw*{b}/{a})'", "setsar=1"]
    else:
        raise OptionError("give aspect (e.g. 9:16) or box [x, y, width, height]")
    return [await _video(src, out_name(out, src, "-cropped"), vf=vf) for src in inputs]


async def resize_video(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    if not o.get("resolution"):
        raise OptionError("resolution is required, e.g. 1280x720 or 720p")
    wh = _RES.get(o["resolution"], o["resolution"].replace("x", ":"))
    mode = o.get("aspect_mode", "pad")
    vf = [f"scale={wh}:force_original_aspect_ratio=decrease,pad={wh}:(ow-iw)/2:(oh-ih)/2" if mode == "pad" else
          f"scale={wh}:force_original_aspect_ratio=increase,crop={wh}" if mode == "crop" else f"scale={wh}", "setsar=1"]
    return [await _video(src, out_name(out, src, "-resized"), vf=vf) for src in inputs]


async def rotate_video(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    vf = []
    ang = int(o.get("angle") or 90) % 360
    vf += {90: ["transpose=1"], 180: ["transpose=1,transpose=1"], 270: ["transpose=2"]}.get(ang, [])
    if o.get("flip") == "horizontal":
        vf.append("hflip")
    elif o.get("flip") == "vertical":
        vf.append("vflip")
    return [await _video(src, out_name(out, src, "-rotated"), vf=vf, extra=["-metadata:s:v", "rotate=0"]) for src in inputs]


async def mute(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    res = []
    for src in inputs:
        dst = out_name(out, src, "-muted")
        await run(*_FF, "-i", str(src), "-c:v", "copy", "-an", *_faststart(_fmt(dst)), str(dst), timeout=VIDEO_TIMEOUT)
        res.append(dst)
    return res


async def to_gif(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    from app.convert.steps import Ctx, _animated
    res = []
    for src in inputs:
        dst = out_name(out, src, "", "gif")
        await _animated(src, "gif", Ctx(out, {k: o[k] for k in ("start", "end", "fps", "max_width") if k in o}), dst)
        res.append(dst)
    return res


async def gif_to_video(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    res = []
    for src in inputs:
        dst = out_name(out, src, "", "mp4")
        loops = int(o.get("loops") or 1)
        await run(*_FF, "-stream_loop", str(loops - 1), "-i", str(src), "-movflags", "+faststart", "-pix_fmt", "yuv420p",
                  "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-crf", "20", "-an", str(dst), timeout=VIDEO_TIMEOUT)
        res.append(dst)
    return res


async def extract_audio(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    fmt = o.get("audio_format", "mp3")
    res = []
    for src in inputs:
        if not _has_audio(await probe(src)):
            raise ValueError(f"{src.name} has no audio track")
        dst = out_name(out, src, "", fmt)
        args = [*_FF, "-i", str(src), "-vn", *_acodec_for(fmt)]
        if o.get("audio_bitrate"):
            args += ["-b:a", o["audio_bitrate"]]
        elif fmt == "mp3":
            args += ["-b:a", "192k"]
        from app.convert.steps import _AUDIO_DEF
        await run(*args, "-f", _AUDIO_DEF[fmt][1], str(dst), timeout=VIDEO_TIMEOUT)
        res.append(dst)
    return res


async def subtitles(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    vids = [p for p in inputs if p.suffix.lower() not in (".srt", ".vtt", ".ass")]
    subs = [p for p in inputs if p.suffix.lower() in (".srt", ".vtt", ".ass")]
    if len(vids) != 1 or len(subs) != 1:
        raise OptionError("give one video and one subtitle file (srt, vtt or ass)")
    src, sub = vids[0], subs[0]
    dst = out_name(out, src, "-subtitled")
    local = out / f"subs{sub.suffix.lower()}"
    shutil.copy(sub, local)
    if o.get("burn", True):
        # a Studio feliratstílusa (a videók feliratsávja: Inter, sötét áttetsző sáv) — az .ass a saját stílusát viszi
        from app.tools.video import subtitle_force_style
        st = next((x for x in (await probe(src)).get("streams") or [] if x.get("codec_type") == "video"), {})
        style = subtitle_force_style(int(st.get("width") or 1920), int(st.get("height") or 1080))
        await _video(src, dst, vf=[f"subtitles={local.name}:force_style='{style}'" if local.suffix != ".ass" else f"subtitles={local.name}"])
    else:
        fmt = _fmt(dst)
        sc = "mov_text" if fmt in ("mp4", "m4v", "mov") else "srt" if fmt == "mkv" else "webvtt"
        lang = (o.get("language") or "")[:3].lower()
        await run(*_FF, "-i", str(src), "-i", str(local), "-map", "0", "-map", "1:0", "-c", "copy", "-c:s", sc,
                  *(["-metadata:s:s:0", f"language={lang}"] if lang else []), str(dst), timeout=VIDEO_TIMEOUT, cwd=out)
    return [dst]


async def volume(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    if not o.get("volume") and not o.get("normalize"):
        raise OptionError("give volume (e.g. 1.5 or +3dB) or normalize")
    af = ["loudnorm=I=-16:TP=-1.5:LRA=11"] if o.get("normalize") else [f"volume={o['volume']}"]
    res = []
    for src in inputs:
        dst = out_name(out, src, "-volume")
        info = await probe(src)
        if _has_video(info):
            await run(*_FF, "-i", str(src), "-c:v", "copy", "-af", ",".join(af), *_acodec_for(_fmt(dst)), *_faststart(_fmt(dst)),
                      str(dst), timeout=VIDEO_TIMEOUT)
        else:
            await run(*_FF, "-i", str(src), "-af", ",".join(af), *_acodec_for(_fmt(dst)), str(dst), timeout=VIDEO_TIMEOUT)
        res.append(dst)
    return res
