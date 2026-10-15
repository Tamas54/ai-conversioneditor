"""Fájleszközök: egyesítés (PDF, videó, hang), bélyegkép, archívum készítése és kibontása."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from app.catalog.formats import FORMATS, from_filename
from app.convert.engines import run
from app.convert.options import OptionError
from app.convert.steps import VIDEO_TIMEOUT, _has_audio, extract, pack, probe
from app.ops.handlers import out_name, to_pdf


def _cat(p: Path) -> str:
    f = FORMATS.get(from_filename(p.name))
    return f.category if f else ""


async def merge(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    cats = {_cat(p) for p in inputs}
    if cats <= {"video"}:
        return [await _concat_av(inputs, out, video=True, fmt=o.get("output_format") or "mp4")]
    if cats <= {"audio"}:
        return [await _concat_av(inputs, out, video=False, fmt=o.get("output_format") or "mp3")]
    if cats & {"video", "audio"}:
        raise OptionError("merge videos with videos and audio with audio")
    from app.ops.handlers.pdf import merge as pdf_merge
    return await pdf_merge(inputs, {}, out)


async def _concat_av(inputs: list[Path], out: Path, video: bool, fmt: str) -> Path:
    """Különböző forrásokat is összefűz: egységes felbontásra/mintavételre kódolva (concat szűrő)."""
    dst = out_name(out, inputs[0], "-merged", fmt)
    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for p in inputs:
        args += ["-i", str(p)]
    n = len(inputs)
    if video:
        infos = [await probe(p) for p in inputs]
        v0 = next(s for s in infos[0]["streams"] if s["codec_type"] == "video")
        W, H = int(v0["width"]) // 2 * 2, int(v0["height"]) // 2 * 2
        all_audio = all(_has_audio(i) for i in infos)
        parts = []
        for i in range(n):
            parts.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v{i}]")
            if all_audio:
                parts.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        streams = "".join(f"[v{i}]" + (f"[a{i}]" if all_audio else "") for i in range(n))
        fc = ";".join(parts) + f";{streams}concat=n={n}:v=1:a={1 if all_audio else 0}[v]" + ("[a]" if all_audio else "")
        from app.ops.handlers.media import _acodec_for, _faststart, _vcodec_for
        args += ["-filter_complex", fc, "-map", "[v]"] + (["-map", "[a]", *_acodec_for(fmt)] if all_audio else ["-an"])
        args += _vcodec_for(fmt) + _faststart(fmt)
    else:
        from app.convert.steps import _AUDIO_DEF
        parts = [f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]" for i in range(n)]
        fc = ";".join(parts) + ";" + "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[a]"
        args += ["-filter_complex", fc, "-map", "[a]", *_AUDIO_DEF.get(fmt, (["-c:a", "libmp3lame"], "mp3", None))[0]]
        if fmt == "mp3":
            args += ["-b:a", "192k"]
    await run(*args, str(dst), timeout=VIDEO_TIMEOUT)
    return dst


async def thumbnail(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    fmt = o.get("image_format", "png")
    if fmt == "pdf":
        fmt = "png"
    W, H = int(o.get("width") or 400), o.get("height")
    res = []
    for src in inputs:
        cat = _cat(src)
        work = out / f"_t-{src.stem}"
        work.mkdir(exist_ok=True)
        if cat in ("video",):
            frame = work / "f.png"
            at = o.get("at") or "1"
            await run("ffmpeg", "-y", "-loglevel", "error", "-ss", str(at), "-i", str(src), "-frames:v", "1", str(frame), timeout=120)
            if not frame.exists():
                await run("ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-frames:v", "1", str(frame), timeout=120)
            img = frame
        elif cat == "image":
            img = src
        elif cat == "vector" and src.suffix.lower() == ".svg":
            img = work / "v.png"
            await run("rsvg-convert", "-w", str(W * 2), "-o", str(img), str(src), timeout=120)
        else:
            pdf = await to_pdf(src, work)
            import pymupdf
            page = max(1, int(o.get("page") or 1))
            img = work / "p.png"

            def render():
                with pymupdf.open(str(pdf)) as d:
                    pg = d[min(page, d.page_count) - 1]
                    pg.get_pixmap(dpi=110).save(str(img))
            await asyncio.to_thread(render)

        def finish(img=img, src=src):
            from PIL import Image, ImageOps
            from app.convert.steps import _pil_save, _register_plugins
            _register_plugins()
            with Image.open(img) as im:
                im = ImageOps.exif_transpose(im)
                if H:
                    im = ImageOps.fit(im.convert("RGB"), (W, int(H)), Image.LANCZOS)
                else:
                    im = im.copy()
                    im.thumbnail((W, W * 4), Image.LANCZOS)
                dst = out_name(out, src, "-thumb", fmt)
                _pil_save(im, dst, fmt, {"quality": 85}, None, {})
                return dst
        res.append(await asyncio.to_thread(finish))
    return res


async def create_archive(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    from app.storage import display_label
    fmt = o.get("archive_format", "zip")
    root = out / "_pack"
    root.mkdir()
    seen: set[str] = set()
    for p in inputs:
        name = p.name
        try:
            fid = p.name
            name = display_label(fid) or p.name
        except Exception:
            pass
        base, i = name, 2
        while name in seen:
            stem, dot, ext = base.rpartition(".")
            name = f"{stem or base}-{i}{dot}{ext}" if dot else f"{base}-{i}"
            i += 1
        seen.add(name)
        shutil.copy(p, root / name)
    stem = (o.get("filename") or "archive").rsplit(".", 1)[0]
    dst = out / f"{stem}.{fmt}"
    await pack(root, dst, fmt, o.get("password"), o.get("level"))
    return [dst]


async def extract_archive(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    dest = await extract(inputs[0], out / "_x", o.get("password"))
    files = sorted(p for p in dest.rglob("*") if p.is_file())
    if not files:
        raise ValueError("The archive is empty.")
    if len(files) > 500:
        raise ValueError(f"The archive holds {len(files)} files — convert it to ZIP instead of extracting.")
    res, names = [], []
    for i, p in enumerate(files):
        rel = p.relative_to(dest).as_posix()
        target = out / f"{i:04d}_{p.name}"
        shutil.move(str(p), target)
        res.append(target)
        names.append(rel)
    return {"files": res, "names": names}
