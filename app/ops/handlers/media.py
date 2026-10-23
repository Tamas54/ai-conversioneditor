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
    from app.tools import encoding
    return encoding.x264("intermediate")                 # egységes profil (OMLETT 1.2): CRF 17, env-ből felülírható


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
    from app.tools import encoding
    return encoding.aac()


def _faststart(fmt: str) -> list[str]:
    from app.tools import encoding
    return encoding.faststart(fmt)


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


async def trim(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path] | dict[str, Any]:
    """Videó: smart cut, ahol biztonságos (app/tools/smart_trim.py) — a zöme bitre pontos másolat —, különben a teljes
    rész újrakódolása; a válasz (data) megmondja, melyik történt és miért."""
    if not o.get("start") and not o.get("end"):
        raise OptionError("give start and/or end (seconds or hh:mm:ss)")
    res, notes = [], []
    for src in inputs:
        dst = out_name(out, src, "-trimmed")
        info = await probe(src)
        pre = (["-ss", _t(o["start"])] if o.get("start") else []) + (["-to", _t(o["end"])] if o.get("end") else [])
        if _has_video(info):
            from app.tools import smart_trim
            notes.append(await smart_trim.trim(src, dst, o.get("start"), o.get("end"), info, _acodec_for(_fmt(dst)),
                                               lambda src=src, dst=dst, pre=pre: _video(src, dst, pre=pre)))
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
    if not notes:
        return res
    return {"files": res, "data": notes[0] if len(notes) == 1 else notes}


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
    from app.tools import mediaprobe, videonorm
    W, H = (int(x) for x in _RES.get(o["resolution"], o["resolution"].replace("x", ":")).split(":"))
    mode = o.get("aspect_mode", "pad")
    fit = {"pad": "contain", "crop": "cover"}.get(mode, mode)          # pad | crop | stretch | blur
    res = []
    for src in inputs:
        m = mediaprobe.summarize(await probe(src))
        res.append(await _video(src, out_name(out, src, "-resized"), vf=[videonorm.input_chain(m, W, H, fit)]))
    return res


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
        await run(*_FF, "-stream_loop", str(loops - 1), "-i", str(src), "-movflags", "+faststart",
                  "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", *_vcodec_for("mp4"), "-an", str(dst), timeout=VIDEO_TIMEOUT)
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


# ---------- meghosszabbítás, kép → videó (OMLETT 3.4) ----------

def _seconds_opt(o: dict[str, Any], key: str = "duration", default: Optional[float] = None) -> float:
    v = o.get(key, default)
    if v is None or float(v) <= 0:
        raise OptionError(f"{key}: how many seconds, e.g. 3")
    return float(v)


async def extend_video(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    """A felvétel meghosszabbítása kimerevített kockával: alapból az UTOLSÓ kocka áll `duration` mp-ig (zárókártya,
    végefőcím alá), `at` megadásával az ott látható kocka áll `duration` mp-re, utána a felvétel folytatódik. A kimerevítés
    alatt csend; a hang a kép után folytatódik (szinkronban marad)."""
    from app.tools import mediaprobe, videonorm
    hold = _seconds_opt(o)
    res = []
    for src in inputs:
        m = mediaprobe.summarize(await probe(src))
        if not m["has_video"]:
            raise OptionError(f"{src.name} has no picture to hold")
        fmt = _fmt(src) if _fmt(src) in ("mp4", "mov", "m4v", "mkv") else "mp4"
        dst = out_name(out, src, "-extended", fmt)
        hf = ",".join(videonorm.head_filters(m) + ["scale=trunc(iw/2)*2:trunc(ih/2)*2"])
        k = m["audio_stream"] or 0
        if o.get("at") in (None, ""):
            fc = f"[0:v]{hf},tpad=stop_mode=clone:stop_duration={hold:g}[v]"
            fa = f"[0:a:{k}]apad=pad_dur={hold:g}[a]"
        else:
            t = _secs(o["at"])
            if not 0 < t < (m["duration"] or 0):
                raise OptionError(f"at: a time inside the video (0–{m['duration']:.1f} s)")
            fc = (f"[0:v]{hf},split[v1][v2];[v1]trim=end={t:g},setpts=PTS-STARTPTS,"
                  f"tpad=stop_mode=clone:stop_duration={hold:g}[va];[v2]trim=start={t:g},setpts=PTS-STARTPTS[vb];"
                  f"[va][vb]concat=n=2:v=1:a=0[v]")
            fa = (f"[0:a:{k}]asplit[a1][a2];[a1]atrim=end={t:g},asetpts=PTS-STARTPTS,apad=pad_dur={hold:g}[aa];"
                  f"[a2]atrim=start={t:g},asetpts=PTS-STARTPTS[ab];[aa][ab]concat=n=2:v=0:a=1[a]")
        args = [*_FF, "-i", str(src), "-filter_complex", fc + (f";{fa}" if m["has_audio"] else ""), "-map", "[v]"]
        args += (["-map", "[a]", *_acodec_for(fmt)] if m["has_audio"] else ["-an"]) + _vcodec_for(fmt) + _faststart(fmt)
        await run(*args, str(dst), timeout=VIDEO_TIMEOUT)
        res.append(dst)
    return res


MOTIONS = ("still", "zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down")


def _ken_burns(motion: str, W: int, H: int, n: int, fps: float, zoom: float = 1.15) -> str:
    """zoompan a vászonra illesztett képen (2× felskálázva, hogy a kerekítés ne remegjen). A mozgás neve a videó
    eszközéé: zoom_in / zoom_out a közép felé / felől; pan_left = a kép balra úszik (a kivágás balról jobbra halad)."""
    p = f"on/{max(1, n - 1)}"
    z, x, y = f"{zoom}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if motion == "zoom_in":
        z = f"1+{zoom - 1:g}*{p}"
    elif motion == "zoom_out":
        z = f"{zoom:g}-{zoom - 1:g}*{p}"
    elif motion in ("pan_left", "pan_right"):
        x = f"(iw-iw/zoom)*{p}" if motion == "pan_left" else f"(iw-iw/zoom)*(1-{p})"
    elif motion in ("pan_up", "pan_down"):
        y = f"(ih-ih/zoom)*{p}" if motion == "pan_up" else f"(ih-ih/zoom)*(1-{p})"
    return f"scale={W * 2}:{H * 2}:flags=lanczos,zoompan=z='{z}':x='{x}':y='{y}':d=1:s={W}x{H}:fps={fps:g}"


async def image_to_video(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    """Állókép → videóklip: `duration` mp (alap 5), `motion` = still | zoom_in | zoom_out | pan_left | pan_right |
    pan_up | pan_down (Ken Burns), `canvas` (alap: a kép mérete, a hosszabb oldal legfeljebb 1920) + `fit` (blur |
    contain | cover). Néma sztereó hangsávval, hogy a merge-files a többi klip hangját megtartsa."""
    import asyncio
    from app.tools import videonorm
    dur = _seconds_opt(o, default=5.0)
    fps = min(60.0, float(o.get("fps") or 30))
    motion = o.get("motion") or "still"
    if motion not in MOTIONS:
        raise OptionError(f"motion: {' | '.join(MOTIONS)}")
    res = []
    for src in inputs:
        png = out / f"_still-{src.stem}.png"

        def upright(src=src, png=png) -> tuple[int, int]:
            from PIL import Image, ImageOps
            from app.convert.steps import _register_plugins
            _register_plugins()
            with Image.open(src) as im:          # a telefonos fotó EXIF-elforgatása (az ffmpeg nem veszi figyelembe)
                im = ImageOps.exif_transpose(im)
                im = im.convert("RGBA").convert("RGB") if im.mode in ("P", "LA", "RGBA") else im.convert("RGB")
                im.save(png)
                return im.size
        iw, ih = await asyncio.to_thread(upright)
        m = {"display_width": iw, "display_height": ih}
        if (o.get("canvas") or "auto") == "auto":
            k = min(1.0, 1920 / max(iw, ih))
            W, H = videonorm.even(iw * k), videonorm.even(ih * k)
        else:
            W, H = videonorm.canvas_size(o["canvas"], m)
        fit = o.get("fit") if o.get("fit") in videonorm.FITS else "blur"
        n = max(1, round(dur * fps))
        vf = videonorm.input_chain(m, W, H, fit) + ("" if motion == "still" else "," + _ken_burns(motion, W, H, n, fps))
        dst = out_name(out, src, "", "mp4")
        await run(*_FF, "-loop", "1", "-framerate", f"{fps:g}", "-t", f"{dur:g}", "-i", str(png), "-f", "lavfi", "-t", f"{dur:g}",
                  "-i", "anullsrc=r=48000:cl=stereo", "-filter_complex", f"[0:v]{vf},format=yuv420p[v]", "-map", "[v]",
                  "-map", "1:a", "-frames:v", str(n), *_vcodec_for("mp4"), "-r", f"{fps:g}", *_acodec_for("mp4"), "-shortest",
                  *_faststart("mp4"), str(dst), timeout=VIDEO_TIMEOUT)
        png.unlink(missing_ok=True)
        res.append(dst)
    return res


# ---------- korrekció: színek, fények, zaj, élesség, stabilizálás (OMLETT 3.2, app/tools/video_fx.py) ----------

async def adjust_video(inputs: list[Path], o: dict[str, Any], out: Path) -> dict[str, Any]:
    """A video_fx beállításai lapos opciókként; preview=true: előtte/utána kép 4 kockáról a teljes kódolás helyett."""
    from app.tools import video_fx
    adj = video_fx.normalize({k: v for k, v in o.items() if k in video_fx.KEYS})
    if not adj:
        raise OptionError('give at least one adjustment, e.g. auto=true, lut="news_studio", sharpen=0.3, denoise="fast"')
    files, data = [], []
    for src in inputs:
        work = out / f"_fx-{src.stem}"
        if o.get("preview"):
            png, info = await video_fx.preview(src, adj, work)
            dst = out_name(out, src, "-before-after", "png")
            shutil.move(str(png), dst)
        else:
            dst = out_name(out, src, "-adjusted", _fmt(src) if _fmt(src) in ("mp4", "mov", "m4v", "mkv") else "mp4")
            info = await video_fx.apply(src, dst, adj, work)
        shutil.rmtree(work, ignore_errors=True)
        files.append(dst)
        data.append({k: info.get(k) for k in ("settings", "auto", "measured", "applied")})
    return {"files": files, "data": data[0] if len(data) == 1 else data}
