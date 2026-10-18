"""Vízjel PDF-re (PyMuPDF), képre (Pillow) és videóra (FFmpeg): szöveg vagy logó, helyzet, átlátszóság, elforgatás.
A szöveg teljes Unicode-betűvel készül (ő, ű)."""
from __future__ import annotations

import asyncio
import math
import shutil
from pathlib import Path
from typing import Any

from app.convert.engines import run
from app.convert.options import OptionError
from app.convert.steps import VIDEO_TIMEOUT, _has_audio, probe
from app.ops.handlers import font_path, out_name, resolve, rgb


def _need(o: dict[str, Any]) -> None:
    if not o.get("text") and not o.get("image_file"):
        raise OptionError("give text or image_file (a logo)")


def _anchor(pos: str, W: float, H: float, w: float, h: float, m: float) -> tuple[float, float]:
    x = {"left": m, "top-left": m, "bottom-left": m, "right": W - w - m, "top-right": W - w - m, "bottom-right": W - w - m}.get(pos, (W - w) / 2)
    y = {"top": m, "top-left": m, "top-right": m, "bottom": H - h - m, "bottom-left": H - h - m, "bottom-right": H - h - m}.get(pos, (H - h) / 2)
    return x, y


async def pdf(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    import pymupdf
    _need(o)
    logo = resolve(o, "image_file")
    fp = font_path(bold=True)
    opacity = float(o.get("opacity", 0.3))
    pos = o.get("position", "center")
    rot = float(o.get("rotation", 45 if pos == "center" and o.get("text") else 0))
    res = []
    for src in inputs:
        dst = out_name(out, src, "-watermarked")

        def go(src=src, dst=dst):
            d = pymupdf.open(str(src))
            for pg in d:
                W, H = pg.rect.width, pg.rect.height
                if logo:
                    lw = W * 0.3
                    with pymupdf.open(str(logo)) as li:
                        r = li[0].rect
                    lh = lw * r.height / max(r.width, 1)
                    x, y = _anchor(pos, W, H, lw, lh, 28)
                    pg.insert_image(pymupdf.Rect(x, y, x + lw, y + lh), filename=str(logo), overlay=True, keep_proportion=True)
                if o.get("text"):
                    size = float(o.get("font_size") or max(18, min(W, H) / 10))
                    from app.tools import pdffonts           # csak a vízjel jelei (nem a teljes DejaVu)
                    font = pymupdf.Font(fontbuffer=pdffonts.subset_for(o["text"], fp)) if fp else pymupdf.Font("helv")
                    tw = font.text_length(o["text"], fontsize=size)
                    x, y = _anchor(pos, W, H, tw, size, 36)
                    tw_writer = pymupdf.TextWriter(pg.rect, opacity=opacity, color=rgb(o.get("color"), (0.5, 0.5, 0.5)))
                    tw_writer.append(pymupdf.Point(x, y + size), o["text"], font=font, fontsize=size)
                    center = pymupdf.Point(x + tw / 2, y + size / 2)
                    tw_writer.write_text(pg, morph=(center, pymupdf.Matrix(-rot)) if rot else None, overlay=True)
            d.save(str(dst), garbage=3, deflate=True)
            d.close()
            return dst
        res.append(await asyncio.to_thread(go))
    return res


async def image(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    _need(o)
    logo = resolve(o, "image_file")
    opacity = float(o.get("opacity", 0.3))
    pos = o.get("position", "bottom-right")
    res = []
    for src in inputs:
        def go(src=src):
            from PIL import Image, ImageDraw, ImageFont
            from app.ops.handlers.image import _fmt, _open, _save
            im, info = _open(src)
            base = im.convert("RGBA")
            layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
            W, H = base.size
            m = max(8, int(min(W, H) * 0.03))
            if logo:
                lg, _ = _open(logo)
                lg = lg.convert("RGBA")
                lw = max(16, int(W * 0.22))
                lg = lg.resize((lw, max(1, round(lg.height * lw / lg.width))), Image.LANCZOS)
                alpha = lg.getchannel("A").point(lambda a: int(a * opacity))
                lg.putalpha(alpha)
                if pos == "tile":
                    for yy in range(0, H, lg.height * 2):
                        for xx in range(0, W, lg.width * 2):
                            layer.alpha_composite(lg, (xx, yy))
                else:
                    x, y = _anchor(pos, W, H, lg.width, lg.height, m)
                    layer.alpha_composite(lg, (int(x), int(y)))
            if o.get("text"):
                size = int(o.get("font_size") or max(14, min(W, H) // 18))
                fp = font_path(bold=True)
                font = ImageFont.truetype(fp, size) if fp else ImageFont.load_default()
                dr = ImageDraw.Draw(layer)
                bb = dr.textbbox((0, 0), o["text"], font=font)
                tw, th = bb[2] - bb[0], bb[3] - bb[1]
                c = tuple(int(v * 255) for v in rgb(o.get("color"), (1, 1, 1)))
                fill = (*c, int(255 * opacity))
                shadow = (0, 0, 0, int(255 * opacity * 0.6))
                if pos == "tile":
                    for yy in range(0, H, th * 4):
                        for xx in range(-(yy % (tw + 40)), W, tw + 80):
                            dr.text((xx, yy), o["text"], font=font, fill=fill)
                else:
                    x, y = _anchor(pos, W, H, tw, th, m)
                    dr.text((x + 1, y + 1), o["text"], font=font, fill=shadow)
                    dr.text((x, y), o["text"], font=font, fill=fill)
            base.alpha_composite(layer)
            fmt = _fmt(src)
            outim = base if fmt in ("png", "webp", "tiff", "avif", "heic", "gif") else base.convert("RGB")
            return _save(outim, out_name(out, src, "-watermarked"), fmt, o, info, 92 if fmt in ("jpg", "jfif") else None)
        res.append(await asyncio.to_thread(go))
    return res


def _ff_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’").replace("%", "\\%")


async def video(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    _need(o)
    logo = resolve(o, "image_file")
    opacity = float(o.get("opacity", 0.3))
    pos = o.get("position", "bottom-right")
    xy = {"top-left": ("20", "20"), "top": ("(W-w)/2", "20"), "top-right": ("W-w-20", "20"), "left": ("20", "(H-h)/2"),
          "center": ("(W-w)/2", "(H-h)/2"), "right": ("W-w-20", "(H-h)/2"), "bottom-left": ("20", "H-h-20"),
          "bottom": ("(W-w)/2", "H-h-20"), "bottom-right": ("W-w-20", "H-h-20"), "tile": ("(W-w)/2", "(H-h)/2")}[pos]
    res = []
    for src in inputs:
        from app.ops.handlers.media import _acodec_for, _faststart, _fmt, _vcodec_for
        dst = out_name(out, src, "-watermarked")
        info = await probe(src)
        args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
        if logo:
            local = out / f"logo{logo.suffix.lower()}"
            shutil.copy(logo, local)
            args += ["-i", str(local)]
            fc = f"[1:v]format=rgba,colorchannelmixer=aa={opacity}[lg0];[lg0][0:v]scale2ref=w=main_w*0.2:h=ow/mdar[lg][base];"
            fc += f"[base][lg]overlay=x={xy[0]}:y={xy[1]}[v0]"
            last = "[v0]"
        else:
            fc, last = "", "[0:v]"
        if o.get("text"):
            fp = font_path(bold=True)
            size = int(o.get("font_size") or 36)
            tx = xy[0].replace("W", "w").replace("-w", "-text_w").replace("(w-w)", "(w-text_w)")
            ty = xy[1].replace("H", "h").replace("-h", "-text_h").replace("(h-h)", "(h-text_h)")
            draw = (f"drawtext=fontfile={fp}:text='{_ff_escape(o['text'])}':fontsize={size}:"
                    f"fontcolor=white@{opacity}:shadowcolor=black@{opacity * 0.6}:shadowx=2:shadowy=2:x={tx}:y={ty}")
            fc = (fc + ";" if fc else "") + f"{last}{draw}[v1]"
            last = "[v1]"
        fc += ";" + f"{last}scale=trunc(iw/2)*2:trunc(ih/2)*2[vout]"
        fmt = _fmt(dst)
        args += ["-filter_complex", fc, "-map", "[vout]"] + (["-map", "0:a?", *_acodec_for(fmt)] if _has_audio(info) else ["-an"])
        args += _vcodec_for(fmt) + _faststart(fmt) + [str(dst)]
        await run(*args, timeout=VIDEO_TIMEOUT, cwd=out)
        res.append(dst)
    return res
