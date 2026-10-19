"""Konverzió egy helyen: a weboldal, a REST API, az MCP és az ügynök is ezt hívja.

    convert_path(src, "pdf", options, work)      útvonal → útvonal(ak)   (a gráf terve szerint, lépésenként)
    convert_file(file_id, "pdf", options, pool)   munkaterület-fájl → új munkaterület-fájl(ok), címkével, metaadattal

A meglévő `anyconvert` útjai (Craft-motorok: WordCraft, PdfCraft, DeckCraft…) változatlanul előnyt kapnak, ha a
pár ott szerepel és nincs külön opció — így a már bevált minőség nem változik. Ha ott a Craft-motor hiányzik
(pl. fejlesztői gépen), vagy opciót kértek, az új gráf fut.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from app.catalog.formats import FORMATS, from_filename, normalize
from app.convert import graph
from app.convert.engines import EngineError
from app.convert.options import OptionError, parse_pages, validate
from app.convert.steps import STEPS, Ctx, _stem, file_ext

log = logging.getLogger("aice.convert")

_TEXTUAL = {"txt", "docx", "doc", "odt", "rtf", "md", "html", "epub", "json", "xml", "pdfa"}


class ConversionError(ValueError):
    pass


def _subset_pdf(src: Path, spec: str, dst: Path) -> Path:
    import pymupdf
    with pymupdf.open(str(src)) as d:
        pages = parse_pages(spec, d.page_count)
        d.select(pages)
        d.save(str(dst), garbage=3, deflate=True)
    return dst


def _has_text(pdf: Path, pages: int = 3) -> bool:
    import pymupdf
    with pymupdf.open(str(pdf)) as d:
        return any(d[i].get_text().strip() for i in range(min(pages, d.page_count)))


async def ocr_pdf(src: Path, dst: Path, language: str = "eng", deskew: bool = True) -> Path:
    """Szöveges réteg egy szkennelt PDF-re (OCRmyPDF + Tesseract, helyben — nincs LLM-költség)."""
    from app.convert.engines import run
    import sys
    args = [sys.executable, "-m", "ocrmypdf", "--skip-text", "-l", language, "--output-type", "pdf", "--optimize", "1"]
    if deskew:
        args.append("--deskew")
    await run(*args, str(src), str(dst), timeout=1800, ok_codes=(0, 6, 10))
    if not dst.exists():
        shutil.copy(src, dst)
    return dst


def ocr_languages() -> set[str]:
    import subprocess
    try:
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {l.strip() for l in out.splitlines()[1:] if l.strip() and l.strip() != "osd"}


# a Rust-motorok (Craft-CLI-k) csak a sima átalakítást tudják: ha a felhasználó olyan opciót kért, amit nem
# kezelnek (méret, minőség, munkalap, elválasztó…), a gráf a következő legjobb motort választja
_CRAFT_OPTS = {"deckcraft": {"page_range", "dpi"}, "gridcraft": set(), "photocraft": set(), "vectorcraft": set()}
_NEUTRAL = {"engine", "ocr", "ocr_language", "output", "filename"}


def craft_avoid(opts: dict[str, Any], tfmt: str) -> set[str]:
    active = set(opts) - _NEUTRAL
    out = {eng for eng, ok in _CRAFT_OPTS.items() if active - ok}
    if "page_range" in active and tfmt != "pdf":
        out.add("deckcraft")                 # diánkénti képnél az oldaltartományt a pdf-es út tudja
    from app.catalog.formats import FORMATS
    if tfmt == "gif" or (FORMATS.get(tfmt) and FORMATS[tfmt].category == "video"):
        out.add("deckcraft")                 # diából videó/GIF: a slideshow lépés (az maga is a DeckCraftot használja)
    return out


def plan_or_raise(sfmt: str, tfmt: str, prefer: Optional[str] = None, avoid: Iterable[str] = ()) -> list:
    if sfmt not in FORMATS:
        raise ConversionError(f"Unknown source format: .{sfmt}")
    if tfmt not in FORMATS:
        raise ConversionError(f"Unknown target format: .{tfmt}")
    p = graph.plan(sfmt, tfmt, prefer=prefer, avoid=avoid)
    if not p:
        static = graph.plan(sfmt, tfmt, static=True)
        if static:
            missing = sorted({e.engine for e in static if not graph.engine_ok(e.engine)})
            raise ConversionError(f".{sfmt} → .{tfmt} needs {', '.join(missing)}, which isn't installed on this server")
        options = graph.targets(sfmt)[:20]
        raise ConversionError(f"Can't convert .{sfmt} to .{tfmt}. Options: {', '.join(options) or 'none'}.")
    return p


async def convert_path(src: Path, target: str, options: Optional[dict[str, Any]], work: Path,
                       resolve: Optional[Callable[[str], Path]] = None, source_format: Optional[str] = None
                       ) -> tuple[list[Path], list[str]]:
    """Vissza: (kimeneti útvonalak, a használt motorok sorrendben)."""
    opts = validate(options)
    sfmt = normalize(source_format) if source_format else from_filename(src.name)
    tfmt = normalize(target)
    prefer = opts.get("engine") if opts.get("engine") not in (None, "auto") else None
    avoid = craft_avoid(opts, tfmt)
    if sfmt == "md":
        from app.tools import wordcraft
        if not wordcraft.md_ok(src):
            avoid.add("wordcraft")           # lábjegyzet, kép, képlet…: a WordCraft betűre hagyná vagy elejtené
    path = plan_or_raise(sfmt, tfmt, prefer, avoid - ({prefer} if prefer else set()))
    work.mkdir(parents=True, exist_ok=True)
    current = [src]
    used: list[str] = []
    rng = opts.get("page_range")
    if sfmt == "pdf" and rng:
        current = [await asyncio.to_thread(_subset_pdf, src, rng, work / "range.pdf")]
    want_ocr = opts.get("ocr", "auto")
    if sfmt == "pdf" and want_ocr != "off" and (tfmt in _TEXTUAL or want_ocr == "on"):
        if want_ocr == "on" or not await asyncio.to_thread(_has_text, current[0]):
            from app.convert.engines import available
            if available("ocrmypdf"):
                current = [await ocr_pdf(current[0], work / "ocr.pdf", opts.get("ocr_language", "eng"))]
                used.append("ocrmypdf")
    n = 0                                   # a lépésmappák számozása folytatódik: a 2. lépés kimenete nem írhatja
    for i, edge in enumerate(path):         # felül az 1.-ét (md→pdf→PDF/A-nál a Ghostscript a saját bemenetére írt: üres lap)
        final = i == len(path) - 1
        ctx = Ctx(work, opts, final=final, source_fmt=sfmt, resolve=resolve, _n=n)
        nxt: list[Path] = []
        for p in current:
            nxt += await STEPS[edge.engine](p, edge.dst, ctx)
        n = ctx._n
        current = nxt
        used.append(edge.engine)
        if edge.dst == "pdf" and rng and sfmt != "pdf" and len(current) == 1:
            current = [await asyncio.to_thread(_subset_pdf, current[0], rng, work / f"range{i}.pdf")]
    from app.tools import ios_safe
    for p in current:                       # minden kimenet iPhone-on is menjen (Keynote, faststart, hvc1, 4:2:0…)
        await ios_safe.finalize(p, opts)
    return current, used


def _zip(files: list[Path], dst: Path) -> None:
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        seen: set[str] = set()
        for f in files:
            name = f.name
            k = 1
            while name in seen:
                k += 1
                name = f"{f.stem}-{k}{f.suffix}"
            seen.add(name)
            z.write(f, name)


def _resolver() -> Callable[[str], Path]:
    from app.tools import crafts

    def resolve(fid: str) -> Path:
        try:
            return crafts.existing(fid)
        except (FileNotFoundError, ValueError):
            raise OptionError(f"file not found: {fid}")
    return resolve


def legacy_target(ext: str, target: str) -> Optional[str]:
    """A bevált (anyconvert) út célja a saját írásmódjával — a „tiff” kérés a régi „tif” útját kapja."""
    from app.tools import anyconvert
    tfmt = normalize(target)
    return next((t for t in anyconvert.targets_for(ext) if normalize(t) == tfmt), None)


def _legacy_ok(ext: str, target: str, options: dict[str, Any]) -> bool:
    return not options and legacy_target(ext, target) is not None


async def convert_file(file_id: str, target: str, options: Optional[dict[str, Any]] = None, pool=None,
                       split: bool = False) -> dict[str, Any]:
    """Munkaterület-fájl konvertálása. Több kimenet (pl. oldalanként egy kép) → egy .zip, vagy split=True
    esetén külön fájlok (`files`)."""
    from app.storage import display_label, output_path, public_url, temp_path, write_meta
    from app.storage.files import new_file_id
    from app.tools import crafts
    opts = dict(options or {})
    out_mode = opts.pop("output", "auto")
    filename = opts.pop("filename", None)
    src = crafts.existing(file_id)
    ext = from_filename(src.name) or src.suffix.lstrip(".").lower()
    tfmt = normalize(target)
    if pool is None:                    # a webes /api/convert/run és a /v1 nem ad át poolt → a futó appé
        from app.mcp_server import _pool_or_none
        pool = _pool_or_none()
    if _legacy_ok(ext, tfmt, opts):
        from app.tools import anyconvert
        try:
            res = await anyconvert.convert_any(pool, file_id, legacy_target(ext, tfmt))
            res.setdefault("engines", ["legacy"])
            res.setdefault("files", [{k: res[k] for k in ("file_id", "url", "label", "size_kb") if k in res}])
            return res
        except RuntimeError as e:
            if "isn't installed" not in str(e) and "not initialized" not in str(e):
                raise
            log.info("legacy convert %s→%s unavailable (%s) — graph engine", ext, tfmt, e)
    work = temp_path(new_file_id("cv"))
    stem = Path(filename).stem if filename else _stem(Path(display_label(file_id)))
    try:
        outs, used = await convert_path(src, tfmt, opts, work, resolve=_resolver(), source_format=ext)
        files: list[dict[str, Any]] = []
        if len(outs) == 1 or split or out_mode == "files":
            for i, o in enumerate(outs, 1):
                oext = from_filename(o.name) or file_ext(tfmt)
                fid = new_file_id(oext)
                shutil.move(str(o), output_path(fid))
                label = f"{stem}{f'-{i:03d}' if len(outs) > 1 else ''}.{oext}"
                write_meta(fid, source_file_id=file_id, operation=f"convert {ext}→{tfmt}", label=label)
                files.append({"file_id": fid, "url": public_url(fid), "label": label,
                              "size_kb": output_path(fid).stat().st_size // 1024})
            main = files[0]
        else:
            fid = new_file_id("zip")
            await asyncio.to_thread(_zip, outs, output_path(fid))
            label = f"{stem}.zip"
            write_meta(fid, source_file_id=file_id, operation=f"convert {ext}→{tfmt}", label=label)
            main = {"file_id": fid, "url": public_url(fid), "label": label,
                    "size_kb": output_path(fid).stat().st_size // 1024, "count": len(outs)}
            files = [main]
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {**main, "files": files, "engines": used, "source_format": ext, "target_format": tfmt}


def targets_for(ext: str, static: bool = False) -> list[str]:
    """A legacy célok (változatlan sorrendben) elöl, utána a gráf többi célja — mind a katalógus kulcsával
    (a régi „tif” itt „tiff”, különben a png → tiff nem látszott felkínáltnak)."""
    from app.tools import anyconvert
    ext = from_filename("x." + ext.lower().lstrip(".")) or ext
    legacy = list(dict.fromkeys(normalize(t) for t in anyconvert.targets_for(ext)))
    rest = [t for t in graph.targets(ext, static=static) if t not in legacy]
    return legacy + rest


__all__ = ["convert_path", "convert_file", "targets_for", "ConversionError", "EngineError", "OptionError", "ocr_pdf",
           "ocr_languages"]
