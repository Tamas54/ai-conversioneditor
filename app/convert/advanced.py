"""Haladó ffmpeg- és ImageMagick-paraméterek — szigorú engedélyezőlistával.

A felhasználó (vagy egy API-hívó) kapcsolókat adhat a motornak, de csak az itt felsoroltakat, és mindegyiknek
csak mintára illeszkedő értéket. Fájlra, URL-re, programra hivatkozó kapcsoló és szűrő (movie=, subtitles=,
amovie=, ImageMagick @fájl, msl:, url:, |cmd, -script, -write, -define …) soha nem mehet át. Az ffmpeg -vf/-af
szűrőlánca elemenként ellenőrzött: csak ismert szűrő, csak szám/kulcsszó paraméter.

    check(["-crf", "23", "-preset", "slow", "-vf", "scale=1280:-2,hflip"])   → ok
    check(["-vf", "movie=/etc/passwd"])                                       → AdvancedError
"""
from __future__ import annotations

import re
from typing import Optional


class AdvancedError(ValueError):
    pass


_NUM = r"-?\d{1,7}(\.\d{1,4})?"
_INT = r"\d{1,7}"
_RATE = r"\d{1,6}(\.\d{1,3})?[kKmM]?"
_GEOM = r"\d{0,5}(x\d{0,5})?([+-]\d{1,5}[+-]\d{1,5})?[%!<>^]?"
_COLOR = r"(#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,20}|(rgb|rgba|srgb)\(\d{1,3}(\.\d+)?%?,\d{1,3}(\.\d+)?%?,\d{1,3}(\.\d+)?%?(,[01](\.\d+)?)?\))"

FFMPEG_FLAGS: dict[str, Optional[str]] = {
    "-crf": r"\d{1,2}", "-qp": r"\d{1,2}", "-q:v": r"\d{1,2}", "-q:a": r"\d{1,2}",
    "-preset": r"(ultrafast|superfast|veryfast|faster|fast|medium|slow|slower|veryslow|placebo|good|best|realtime|p[1-7])",
    "-tune": r"(film|animation|grain|stillimage|fastdecode|zerolatency|psnr|ssim)",
    "-profile:v": r"(baseline|main|high|high10|high422|high444|main10|[0-3])", "-level": r"\d(\.\d)?",
    "-pix_fmt": r"(yuv420p|yuv422p|yuv444p|yuv420p10le|yuv422p10le|rgb24|rgba|gray|nv12)",
    "-g": _INT, "-bf": r"\d{1,2}", "-refs": r"\d{1,2}", "-threads": r"\d{1,2}",
    "-b:v": _RATE, "-maxrate": _RATE, "-minrate": _RATE, "-bufsize": _RATE, "-b:a": _RATE,
    "-r": r"\d{1,3}(\.\d{1,3})?|\d{1,6}/\d{1,4}", "-ar": r"\d{4,6}", "-ac": r"[1-8]",
    "-movflags": r"\+?(faststart|frag_keyframe|empty_moov)(\+(faststart|frag_keyframe|empty_moov))*",
    "-row-mt": r"[01]", "-tile-columns": r"[0-6]", "-cpu-used": r"-?\d{1,2}", "-deadline": r"(good|best|realtime)",
    "-x264-params": r"[a-z0-9_\-]{1,20}=[0-9a-z.\-]{1,10}(:[a-z0-9_\-]{1,20}=[0-9a-z.\-]{1,10}){0,10}",
    "-x265-params": r"[a-z0-9_\-]{1,20}=[0-9a-z.\-]{1,10}(:[a-z0-9_\-]{1,20}=[0-9a-z.\-]{1,10}){0,10}",
    "-an": None, "-vn": None, "-sn": None, "-shortest": None,
    "-aspect": r"\d{1,2}:\d{1,2}|\d(\.\d{1,4})?",
    "-c:v": r"(libx264|libx265|libvpx|libvpx-vp9|libaom-av1|libsvtav1|mpeg4|mpeg2video|prores_ks|mjpeg|copy)",
    "-c:a": r"(aac|libmp3lame|libopus|libvorbis|flac|alac|pcm_s16le|pcm_s24le|ac3|copy)",
    "-vf": "FILTER_V", "-af": "FILTER_A",
}

_FV = {
    "scale": r"(-?\d{1,5}|iw\*?[\d.]*|ih\*?[\d.]*|-1|-2)[:x](-?\d{1,5}|iw\*?[\d.]*|ih\*?[\d.]*|-1|-2)(:flags=(lanczos|bicubic|bilinear))?",
    "crop": r"(\d{1,5}|iw\*?[\d.]*|ih\*?[\d.]*)(:(\d{1,5}|iw\*?[\d.]*|ih\*?[\d.]*))?(:\d{1,5}:\d{1,5})?",
    "pad": r"\d{1,5}:\d{1,5}(:(\(ow-iw\)/2|\d{1,5}):(\(oh-ih\)/2|\d{1,5}))?(:color=" + _COLOR + ")?",
    "fps": r"\d{1,3}(\.\d{1,3})?", "transpose": r"[0-3]|clock|cclock|clock_flip|cclock_flip",
    "hflip": "", "vflip": "", "setsar": r"\d{1,3}(/\d{1,3})?", "setdar": r"\d{1,3}(/\d{1,3})?|\d{1,2}:\d{1,2}",
    "format": r"(yuv420p|yuv444p|rgb24|gray)", "eq": r"((brightness|contrast|saturation|gamma)=" + _NUM + ":?){1,4}",
    "hue": r"(h|s|b)=" + _NUM + r"(:(h|s|b)=" + _NUM + "){0,2}", "unsharp": r"\d{1,2}(:\d{1,2}(:" + _NUM + r"){0,1}){0,2}",
    "deband": "", "deflicker": "", "hqdn3d": r"(" + _NUM + r")(:" + _NUM + r"){0,3}", "yadif": r"[0-3]?",
    "reverse": "", "negate": "", "boxblur": r"\d{1,2}(:\d{1,2})?", "vignette": "", "colorchannelmixer": r"(" + _NUM + r":?){1,12}",
    "setpts": r"(PTS|[\d.]+\*PTS|PTS/[\d.]+)",
}
_FA = {
    "volume": r"\d{1,2}(\.\d{1,3})?|[+-]?\d{1,2}(\.\d)?dB", "loudnorm": r"(I=-?\d{1,2}(:TP=-?\d(\.\d)?)?(:LRA=\d{1,2})?)?",
    "atempo": r"(0\.[5-9]\d{0,2}|1(\.\d{1,3})?|2(\.0{1,3})?)", "aresample": r"\d{4,6}", "highpass": r"f=\d{1,5}",
    "lowpass": r"f=\d{1,5}", "afade": r"t=(in|out):st=" + _NUM + ":d=" + _NUM, "areverse": "", "dynaudnorm": "",
    "equalizer": r"f=\d{1,5}:width_type=(h|q|o|s):width=" + _NUM + ":g=" + _NUM, "silenceremove": r"(start|stop)_periods=\d",
    "pan": r"(mono|stereo)\|c0=[c0-9+*.]{1,30}(\|c1=[c0-9+*.]{1,30})?", "aecho": r"[\d.:|]{3,60}",
}

IM_FLAGS: dict[str, Optional[str]] = {
    "-resize": _GEOM, "-thumbnail": _GEOM, "-scale": _GEOM, "-sample": _GEOM, "-extent": _GEOM, "-crop": _GEOM,
    "-quality": r"\d{1,3}", "-strip": None, "-auto-orient": None, "-flip": None, "-flop": None, "-trim": None,
    "-normalize": None, "-auto-level": None, "-auto-gamma": None, "-negate": None, "-monochrome": None,
    "-flatten": None, "-enhance": None, "-despeckle": None, "-equalize": None, "+repage": None, "-transpose": None,
    "-rotate": _NUM, "-blur": r"\d{1,3}(\.\d+)?x\d{1,3}(\.\d+)?", "-gaussian-blur": r"\d{1,3}(\.\d+)?x\d{1,3}(\.\d+)?",
    "-sharpen": r"\d{1,3}(\.\d+)?x\d{1,3}(\.\d+)?", "-unsharp": r"\d{1,3}(\.\d+)?x\d{1,3}(\.\d+)?(\+\d(\.\d+)?\+\d(\.\d+)?)?",
    "-brightness-contrast": r"-?\d{1,3}(x-?\d{1,3})?", "-modulate": r"\d{1,3}(,\d{1,3}(,\d{1,3})?)?",
    "-gamma": r"\d(\.\d{1,3})?", "-level": r"\d{1,3}%?(,\d{1,3}%?(,\d(\.\d+)?)?)?",
    "-colorspace": r"(sRGB|RGB|Gray|CMYK|Lab|HSL|HSB)", "-type": r"(Grayscale|Bilevel|TrueColor|TrueColorAlpha|Palette)",
    "-colors": r"\d{1,3}", "-depth": r"(8|16)", "-density": r"\d{2,4}(x\d{2,4})?", "-units": r"(PixelsPerInch|PixelsPerCentimeter)",
    "-gravity": r"(Center|North|South|East|West|NorthEast|NorthWest|SouthEast|SouthWest)",
    "-background": _COLOR, "-bordercolor": _COLOR, "-fill": _COLOR, "-border": _GEOM, "-frame": _GEOM,
    "-posterize": r"\d{1,3}", "-sepia-tone": r"\d{1,3}%", "-charcoal": r"\d{1,2}", "-sketch": r"\d{1,2}x\d{1,2}\+\d{1,3}",
    "-sigmoidal-contrast": r"\d{1,2}(x\d{1,3}%?)?", "-threshold": r"\d{1,3}%", "-dither": r"(None|FloydSteinberg|Riemersma)",
    "-interlace": r"(None|Line|Plane|JPEG|PNG)", "-sampling-factor": r"\d:\d:\d|\dx\d", "-alpha": r"(on|off|remove|set)",
    "-vignette": r"\d{1,3}x\d{1,3}", "-swirl": r"-?\d{1,3}", "-implode": r"-?\d(\.\d+)?", "-shave": _GEOM, "-chop": _GEOM,
}
def _filter_chain(chain: str, allowed: dict[str, str]) -> None:
    if len(chain) > 600 or ";" in chain or "[" in chain:
        raise AdvancedError("filter chains: simple comma-separated filters only")
    for part in chain.split(","):
        name, _, args = part.strip().partition("=")
        if name not in allowed:
            raise AdvancedError(f"filter not allowed: {name}")
        pat = allowed[name]
        if not args:
            if pat and name not in ("yadif", "loudnorm"):
                raise AdvancedError(f"{name} needs parameters")
        elif not pat or not re.fullmatch(pat, args):
            raise AdvancedError(f"invalid parameters for {name}")


def check(args: list, tool: str = "ffmpeg") -> list[str]:
    """Az engedélyezőlista ellenőrzése; vissza: a (változatlan) argumentumlista szövegként."""
    if not isinstance(args, list) or len(args) > 60:
        raise AdvancedError("advanced: a list of at most 60 items")
    flags = IM_FLAGS if tool == "imagemagick" else FFMPEG_FLAGS
    out = [str(a) for a in args]
    i = 0
    while i < len(out):
        flag = out[i]
        if flag not in flags:
            raise AdvancedError(f"option not allowed: {flag[:30]}")
        pat = flags[flag]
        if pat is None:
            i += 1
            continue
        if i + 1 >= len(out):
            raise AdvancedError(f"{flag} needs a value")
        val = out[i + 1]
        if pat == "FILTER_V":
            _filter_chain(val, _FV)
        elif pat == "FILTER_A":
            _filter_chain(val, _FA)
        else:
            if not re.fullmatch(pat, val):
                raise AdvancedError(f"invalid value for {flag}: {val[:30]}")
        i += 2
    return out


def check_any(args: list) -> str:
    """Konverziós `advanced`: ffmpeg- VAGY ImageMagick-kapcsolók (vegyesen nem). Vissza: melyiké."""
    try:
        check(args, "ffmpeg")
        return "ffmpeg"
    except AdvancedError as e:
        try:
            check(args, "imagemagick")
            return "imagemagick"
        except AdvancedError:
            raise e


def split_by_tool(args: list) -> tuple[list[str], list[str]]:
    """Konverziós `advanced` opció: ami ffmpeg-kapcsoló, az ffmpeg-é; ami ImageMagick, az azé."""
    a = [str(x) for x in args]
    ff = all(x in FFMPEG_FLAGS or (i > 0 and a[i - 1] in FFMPEG_FLAGS and FFMPEG_FLAGS[a[i - 1]] is not None)
             for i, x in enumerate(a))
    return (a, []) if ff else ([], a)
