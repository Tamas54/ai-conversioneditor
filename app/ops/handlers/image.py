"""Képeszközök Pillow-val (és ahol jobb: pngquant, ffmpeg): tömörítés, átméretezés, vágás, forgatás, tükrözés,
nagyítás, kollázs, színválasztó. A formátum (és az átlátszóság) marad, a metaadat kérésre törlődik."""
from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any

from app.convert.engines import available, run
from app.convert.options import OptionError
from app.ops.handlers import out_name


def _open(p: Path):
    from PIL import Image, ImageOps
    from app.convert.steps import _register_plugins
    _register_plugins()
    Image.MAX_IMAGE_PIXELS = 400_000_000
    im = Image.open(p)
    info = dict(im.info)
    im = ImageOps.exif_transpose(im)
    im.load()
    return im, info


def _fmt(p: Path) -> str:
    from app.catalog.formats import from_filename
    return from_filename(p.name) or "png"


def _save(im, dst: Path, fmt: str, o: dict[str, Any], info: dict, quality=None) -> Path:
    from app.convert.steps import _pil_save
    opts = {"strip_metadata": o.get("strip_metadata", False), **({"quality": quality} if quality else {})}
    if fmt not in ("jpg", "jfif", "png", "webp", "gif", "bmp", "tiff", "avif", "heic", "heif", "jxl", "tga", "ico", "jp2", "pcx",
                   "ppm", "pgm", "pbm", "pnm", "sgi", "xbm", "dds", "apng", "icns"):
        fmt = "png"
        dst = dst.with_suffix(".png")
    _pil_save(im, dst, fmt, opts, None, info)
    return dst


async def _each(inputs, o, out, fn, suffix: str) -> list[Path]:
    res = []
    for src in inputs:
        def go(src=src):
            im, info = _open(src)
            fmt = _fmt(src)
            im2 = fn(im, fmt)
            return _save(im2, out_name(out, src, suffix), fmt, o, info, o.get("quality"))
        res.append(await asyncio.to_thread(go))
    return res


# ---------- tömörítés ----------

async def compress(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    res = []
    for src in inputs:
        fmt = _fmt(src)
        if fmt == "png":
            res += await compress_png([src], o, out)
        elif fmt == "gif":
            res += await compress_gif([src], o, out)
        else:
            def go(src=src):
                from PIL import Image
                im, info = _open(src)
                if o.get("max_width") and im.width > o["max_width"]:
                    im = im.resize((o["max_width"], round(im.height * o["max_width"] / im.width)), Image.LANCZOS)
                q = o.get("quality") or (80 if fmt in ("jpg", "jfif", "webp") else 60 if fmt in ("avif", "heic") else None)
                dst = _save(im, out_name(out, src, "-compressed"), fmt, {"strip_metadata": o.get("strip_metadata", True)}, info, q)
                return _smaller(src, dst)
            res.append(await asyncio.to_thread(go))
    return res


def _smaller(src: Path, dst: Path) -> Path:
    if dst.stat().st_size >= src.stat().st_size:
        dst.write_bytes(src.read_bytes())          # nem lett kisebb: az eredeti, változatlanul
    return dst


async def compress_jpeg(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    return await compress(inputs, {**o, "quality": o.get("quality") or 80}, out)


async def compress_png(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    res = []
    colors = int(o.get("colors") or 256)
    for src in inputs:
        dst = out_name(out, src, "-compressed", "png")
        import shutil
        if shutil.which("pngquant"):
            await run("pngquant", "--force", "--skip-if-larger", "--speed", "3", "--quality", "50-90", str(colors),
                      "--output", str(dst), str(src), ok_codes=(0, 98, 99))
            if not dst.exists():
                shutil.copy(src, dst)
        else:
            def go():
                from PIL import Image
                im, info = _open(src)
                q = im.convert("RGBA").quantize(colors=colors, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.FLOYDSTEINBERG)
                q.save(dst, "PNG", optimize=True)
            await asyncio.to_thread(go)
        res.append(_smaller(src, dst))
    return res


async def compress_gif(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    res = []
    for src in inputs:
        dst = out_name(out, src, "-compressed", "gif")
        w = o.get("max_width") or -1
        fps = o.get("fps")
        vf = ",".join(x for x in [f"fps={fps}" if fps else "", f"scale={w}:-1:flags=lanczos" if w != -1 else ""] if x)
        colors = int(o.get("colors") or 128)
        chain = (vf + "," if vf else "") + f"split[a][b];[a]palettegen=max_colors={colors}:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4"
        await run("ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vf", chain, "-loop", "0", str(dst), timeout=600)
        res.append(_smaller(src, dst))
    return res


# ---------- geometria ----------

async def resize(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    from app.convert.steps import _apply_image_opts
    if not (o.get("width") or o.get("height") or o.get("percent")):
        raise OptionError("give width, height or percent")
    if o.get("fit") == "blur":
        raise OptionError("fit: inside, cover, contain or fill (blur is for videos)")

    def fn(im, fmt):
        if o.get("percent"):
            from PIL import Image
            k = o["percent"] / 100
            return im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
        return _apply_image_opts(im, {"width": o.get("width"), "height": o.get("height"), "fit": o.get("fit", "inside")}, fmt)
    return await _each(inputs, o, out, fn, "-resized")


async def crop(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    def fn(im, fmt):
        if o.get("box"):
            x, y, w, h = [int(float(v)) for v in o["box"][:4]]
            if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > im.width or y + h > im.height:
                raise OptionError(f"box must lie inside the {im.width}×{im.height} image")
            return im.crop((x, y, x + w, y + h))
        if o.get("aspect"):
            a, b = (int(v) for v in str(o["aspect"]).split(":"))
            tw, th = (im.width, round(im.width * b / a)) if im.width * b / a <= im.height else (round(im.height * a / b), im.height)
            x, y = (im.width - tw) // 2, (im.height - th) // 2
            return im.crop((x, y, x + tw, y + th))
        raise OptionError("give box [x, y, width, height] or aspect (e.g. 1:1)")
    return await _each(inputs, o, out, fn, "-cropped")


async def rotate(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    from PIL import Image
    ang = int(o.get("angle") or 90)
    return await _each(inputs, o, out, lambda im, f: im.rotate(-ang, expand=bool(o.get("expand", True)), resample=Image.BICUBIC), "-rotated")


async def flip(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    from PIL import ImageOps
    d = o.get("direction") or o.get("flip") or "horizontal"
    return await _each(inputs, o, out, lambda im, f: ImageOps.mirror(im) if d == "horizontal" else ImageOps.flip(im), "-flipped")


async def upscale(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    from PIL import Image, ImageFilter
    k = int(o.get("scale") or 2)

    def fn(im, f):
        if im.width * k * im.height * k > 120_000_000:
            raise OptionError("the result would be over 120 megapixels")
        big = im.resize((im.width * k, im.height * k), Image.LANCZOS)
        return big.filter(ImageFilter.UnsharpMask(radius=1.6, percent=60, threshold=2))
    return await _each(inputs, o, out, fn, f"-x{k}")


async def collage(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    def go():
        from PIL import Image, ImageOps
        from app.convert.steps import _rgba
        ims = [_open(p)[0].convert("RGB") for p in inputs]
        n = len(ims)
        cols = int(o.get("columns") or math.ceil(math.sqrt(n)))
        rows = math.ceil(n / cols)
        W = int(o.get("width") or 2400)
        gap = int(o.get("gap") if o.get("gap") is not None else 8)
        cell = (W - gap * (cols + 1)) // cols
        H = rows * cell + gap * (rows + 1)
        bg = _rgba(o.get("background") or "#ffffff")
        canvas = Image.new("RGB", (W, H), bg[:3])
        for i, im in enumerate(ims):
            r, c = divmod(i, cols)
            tile = ImageOps.fit(im, (cell, cell), Image.LANCZOS)
            canvas.paste(tile, (gap + c * (cell + gap), gap + r * (cell + gap)))
        dst = out_name(out, inputs[0], "-collage", "jpg")
        canvas.save(dst, "JPEG", quality=90, optimize=True)
        return dst
    return [await asyncio.to_thread(go)]


async def palette(inputs: list[Path], o: dict[str, Any], out: Path) -> dict[str, Any]:
    n = int(o.get("colors") or 8)

    def go():
        from PIL import Image
        im, _ = _open(inputs[0])
        small = im.convert("RGB")
        small.thumbnail((300, 300))
        q = small.quantize(colors=min(n, 64), method=Image.Quantize.MEDIANCUT)
        pal = q.getpalette()
        counts = sorted(q.getcolors(), reverse=True)
        total = sum(c for c, _ in counts)
        cols = []
        for cnt, idx in counts[:n]:
            r, g, b = pal[idx * 3: idx * 3 + 3]
            cols.append({"hex": f"#{r:02x}{g:02x}{b:02x}", "rgb": [r, g, b], "share": round(cnt / total, 4)})
        # mintakártya PNG-ben is
        sw = Image.new("RGB", (80 * len(cols), 80), "white")
        for i, c in enumerate(cols):
            sw.paste(tuple(c["rgb"]), (i * 80, 0, (i + 1) * 80, 80))
        dst = out_name(out, inputs[0], "-palette", "png")
        sw.save(dst)
        return {"files": [dst], "data": {"colors": cols}}
    return await asyncio.to_thread(go)
