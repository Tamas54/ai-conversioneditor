"""Haladó eszközök: saját ffmpeg- és ImageMagick-kapcsolók — csak az engedélyezőlistán szereplők
(app/convert/advanced.py), csak mintára illő értékkel. Bemenet és kimenet mindig a mi útvonalunk, a kimeneti
formátumot a katalógus ismert formátumai közül kell választani; shell nincs."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from app.catalog.formats import FORMATS, from_filename, normalize
from app.convert.advanced import AdvancedError, check
from app.convert.engines import run
from app.convert.options import OptionError
from app.convert.steps import file_ext
from app.ops.handlers import out_name

_IM_OUT = {"png", "jpg", "jpeg", "webp", "gif", "tiff", "bmp", "avif", "heic", "jp2", "ico", "tga", "ppm", "pgm", "pbm",
           "pcx", "psd", "pdf", "jxl", "apng", "dds", "exr", "hdr", "sgi", "xpm", "xbm"}
_IM_IN_CODER = {"jpg": "jpeg", "jfif": "jpeg", "tif": "tiff", "heif": "heic"}


def _target(o: dict[str, Any], src: Path, allowed_cats: tuple[str, ...]) -> str:
    fmt = normalize(o.get("output_format") or from_filename(src.name) or "")
    f = FORMATS.get(fmt)
    if f is None or (f.category not in allowed_cats and fmt not in ("gif", "apng", "webp", "pdf")):
        raise OptionError(f"output_format: .{fmt or '?'} isn't a {'/'.join(allowed_cats)} format")
    return fmt


def _args(o: dict[str, Any], tool: str) -> list[str]:
    try:
        return check(list(o.get("args") or []), tool)
    except AdvancedError as e:
        raise OptionError(f"args: {e}")


async def ffmpeg(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    src = inputs[0]
    fmt = _target(o, src, ("video", "audio"))
    args = _args(o, "ffmpeg")
    dst = out_name(out, src, "-ffmpeg", file_ext(fmt))
    await run("ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(src), *args, str(dst), timeout=3600)
    if not dst.exists() or dst.stat().st_size == 0:
        raise OptionError("ffmpeg produced no output with these parameters")
    return [dst]


async def imagemagick(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    exe = "magick" if shutil.which("magick") else "convert"
    args = _args(o, "imagemagick")
    res = []
    for src in inputs:
        fmt = _target(o, src, ("image",))
        if fmt not in _IM_OUT:
            raise OptionError(f"output_format: ImageMagick can't write .{fmt} here. Use one of: {', '.join(sorted(_IM_OUT))}")
        sfmt = from_filename(src.name) or "png"
        dst = out_name(out, src, "-im", file_ext(fmt))
        coder_in = _IM_IN_CODER.get(sfmt, sfmt)
        coder_out = _IM_IN_CODER.get(fmt, fmt)
        await run(exe, f"{coder_in}:{src}", *args, f"{coder_out}:{dst}", timeout=600)
        res.append(dst)
    return res
