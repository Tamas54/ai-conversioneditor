"""„Convert” — bármit bármivé, a legjobb motorral párosonként.

A felület a forrás kiterjesztéséből kérdezi le a lehetséges célokat (targets_for), a konverziót a
convert_any végzi. A régi `convert` (LibreOffice/pandoc/WeasyPrint/pdf2docx) marad az alap; ahol egy
Craft-motor gyorsabb vagy jobb, ott az fut:
    docx/odt/rtf/md/html/txt → pdf/odt/rtf/html/md/txt   WordCraft (ms-ok, Caladea/Carlito metrika)
    pdf → docx/html/rtf/txt                              PdfCraft (olvasási sorrend, címsorok, képek)
    pdf → png/jpg                                        pdftoppm (több oldal → .zip)
    pptx → pdf, pptx → png (diák .zip-ben)               DeckCraft
    xlsx/csv/tsv ↔ xlsx/csv/tsv/json/html                GridCraft
    képek ↔ png/jpg/webp/tif/psd                         PhotoCraft;  kép → pdf: Pillow
    svg/ai/eps → pdf/png/svg/jpg                         VectorCraft
"""
from __future__ import annotations

import asyncio
import shutil
import zipfile
from pathlib import Path
from typing import Any, Optional

from app.storage import display_label, output_path, public_url, temp_path, write_meta
from app.storage.files import new_file_id

DOCS = {"docx", "doc", "odt", "rtf", "md", "html", "htm", "txt"}
SLIDES = {"pptx", "ppt", "odp"}
SHEETS = {"xlsx", "xls", "ods", "csv", "tsv"}
IMAGES = {"png", "jpg", "jpeg", "webp", "tif", "tiff", "bmp", "gif", "psd"}
VECTORS = {"svg", "ai", "eps", "vectorcraft"}

LABELS = {
    "pdf": "PDF", "docx": "Word (.docx)", "odt": "OpenDocument (.odt)", "rtf": "Rich Text (.rtf)",
    "md": "Markdown (.md)", "html": "Web page (.html)", "txt": "Plain text (.txt)", "epub": "E-book (.epub)",
    "pptx": "PowerPoint (.pptx)", "odp": "OpenDocument slides (.odp)", "png": "PNG image", "jpg": "JPG image",
    "webp": "WebP image", "tif": "TIFF image", "psd": "Photoshop (.psd)", "xlsx": "Excel (.xlsx)",
    "csv": "CSV", "tsv": "TSV", "json": "JSON", "ods": "OpenDocument sheet (.ods)", "svg": "SVG",
}


def targets_for(ext: str) -> list[str]:
    ext = ext.lower().lstrip(".")
    if ext in DOCS:
        return [t for t in ("pdf", "docx", "odt", "rtf", "md", "html", "txt", "epub") if t != ext]
    if ext == "pdf":
        return ["docx", "txt", "html", "rtf", "jpg", "png"]
    if ext in SLIDES:          # pptx → docx = Word-kiosztmány (diakép + szöveg + jegyzetek)
        return [t for t in ("pdf", "pptx", "odp", "png", *(("docx",) if ext == "pptx" else ())) if t != ext]
    if ext in SHEETS:
        return [t for t in ("xlsx", "csv", "pdf", "html", "json", "ods", "tsv") if t != ext]
    if ext in IMAGES:
        norm = "jpg" if ext == "jpeg" else "tif" if ext == "tiff" else ext
        return [t for t in ("jpg", "png", "webp", "pdf", "tif", "psd") if t != norm]
    if ext in VECTORS:
        return [t for t in ("pdf", "png", "svg", "jpg") if t != ext]
    return []


def _image_save(src: Path, dst: Path, fmt: str) -> None:
    """jpg/png/webp: Pillow, ésszerű minőséggel (a webp alapból veszteségmentes lenne → nagyobb, mint a jpg)."""
    from PIL import Image, ImageOps
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if fmt == "jpg":
            im.convert("RGB").save(dst, "JPEG", quality=88, optimize=True)
        elif fmt == "webp":
            im.save(dst, "WEBP", quality=85, method=4)
        else:
            im.save(dst, "PNG", optimize=True)


def _image_to_pdf(src: Path, dst: Path) -> None:
    from PIL import Image, ImageOps
    with Image.open(src) as im:
        ImageOps.exif_transpose(im).convert("RGB").save(dst, "PDF", resolution=150)


async def _run(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="replace")[:300] or f"{args[0]} failed")


def _zip_dir(files: list[Path], dst: Path, stem: str) -> None:
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for i, f in enumerate(files, 1):
            z.write(f, f"{stem}-{i:03d}{f.suffix}")


async def lo_convert(src: Path, dst: Path) -> None:
    """LibreOffice-konverzió (saját profillal, így párhuzamosan is fut) — a cél formátuma a `dst` kiterjesztése.
    A validate_document is ezt hívja: a LibreOffice-tördelés ≈ ahogy a letöltött fájlt a Word mutatja."""
    work = temp_path(new_file_id("lo")); work.mkdir(parents=True)
    try:
        await _run("soffice", f"-env:UserInstallation=file://{work}/profile", "--headless", "--convert-to",
                   dst.suffix.lstrip("."), "--outdir", str(work), str(src))
        out = work / (src.stem + dst.suffix)
        if not out.exists():
            raise RuntimeError("LibreOffice produced nothing")
        shutil.move(str(out), dst)
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def to_pdf(src: Path, dst: Path) -> None:
    """Nyomtatáshoz: bármelyik támogatott fájl → PDF a `dst`-be, CSENDBEN (nincs meta, nincs verzió-sor)."""
    from app.tools import crafts, wordcraft
    ext = src.suffix.lstrip(".").lower()
    if ext in DOCS and ext in wordcraft.OPEN_FORMATS and ext != "doc":
        await wordcraft.convert_file(src, dst)
    elif ext == "pptx":
        await crafts.office_convert("deck", src, dst)
    elif ext in IMAGES:
        await asyncio.to_thread(_image_to_pdf, src, dst)
    elif ext in VECTORS:
        await crafts.convert_file("vectorcraft", src, dst)
    elif ext in DOCS | SLIDES | SHEETS:           # doc, ppt, odp, táblázatok: LibreOffice
        await lo_convert(src, dst)
    else:
        raise ValueError(f"Printing .{ext} files isn't supported — download it instead.")
    if not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError("The print version came out empty.")


async def convert_any(pool, file_id: str, target: str) -> dict[str, Any]:
    from app.tools import crafts, wordcraft
    from app.tools.convert import convert as legacy_convert
    src = crafts.existing(file_id)
    ext = src.suffix.lstrip(".").lower()
    target = target.lower().lstrip(".")
    if target not in targets_for(ext):
        raise ValueError(f"Can't convert .{ext} to .{target}. Options: {', '.join(targets_for(ext)) or 'none'}.")
    if ext == "pptx" and target == "docx":
        from app.tools import deck_handout
        return await deck_handout.build(file_id)
    stem = Path(display_label(file_id)).stem
    work: Optional[Path] = None
    out_ext = target
    try:
        if ext == "pdf" and target in ("png", "jpg"):
            work = temp_path(new_file_id("pg")); work.mkdir(parents=True)
            await _run("pdftoppm", "-r", "150", *(["-png"] if target == "png" else ["-jpeg", "-jpegopt", "quality=85"]),
                       str(src), str(work / "p"))
            pages = sorted(work.glob("p-*"))
            if len(pages) == 1:
                fid = new_file_id(target); shutil.move(str(pages[0]), output_path(fid))
            else:
                out_ext = "zip"; fid = new_file_id("zip")
                await asyncio.to_thread(_zip_dir, pages, output_path(fid), stem)
        elif ext == "pdf":
            fid = new_file_id(target)
            if target == "txt":
                await _run("pdftotext", "-layout", str(src), str(output_path(fid)))
            else:
                await crafts.pdf_script([file_id], [{"tool": "doc_export_office", "args": {"path": "$out"}}],
                                        output_path(fid), save=False)
        elif ext in SLIDES and target == "png":
            work = temp_path(new_file_id("sl")); work.mkdir(parents=True)
            await crafts.run_cli(crafts.office_cli("deck"), "deckcraft-cli", "render", str(src), "--all", "--scale", "1.5", str(work / "s.png"))
            slides = sorted((work / "s.png").glob("*.png")) if (work / "s.png").is_dir() else sorted(work.glob("*.png"))
            out_ext = "zip"; fid = new_file_id("zip")
            await asyncio.to_thread(_zip_dir, slides, output_path(fid), stem)
        elif ext == "pptx" and target == "pdf":
            fid = new_file_id("pdf"); await crafts.office_convert("deck", src, output_path(fid))
        elif ext in {"xlsx", "csv", "tsv"} and target in {"xlsx", "csv", "tsv", "json", "html"}:
            fid = new_file_id(target); await crafts.office_convert("sheet", src, output_path(fid))
        elif ext in IMAGES and target == "pdf":
            fid = new_file_id("pdf"); await asyncio.to_thread(_image_to_pdf, src, output_path(fid))
        elif ext in IMAGES and ext != "psd" and target in ("jpg", "png", "webp"):
            fid = new_file_id(target); await asyncio.to_thread(_image_save, src, output_path(fid), target)
        elif ext in IMAGES:
            fid = new_file_id(target); await crafts.convert_file("photocraft", src, output_path(fid))
        elif ext in VECTORS:
            fid = new_file_id(target); await crafts.convert_file("vectorcraft", src, output_path(fid))
        elif ext in wordcraft.OPEN_FORMATS and target in wordcraft.SAVE_FORMATS and target != "docx" and ext != "doc":
            fid = new_file_id(target); await wordcraft.convert_file(src, output_path(fid))
        elif target == "epub":
            import pypandoc
            fid = new_file_id("epub")
            await asyncio.to_thread(pypandoc.convert_file, str(src), "epub", outputfile=str(output_path(fid)),
                                    extra_args=["--metadata", f"title={stem}"])
        else:
            res = await legacy_convert(pool, file_id=file_id, target_format=target)
            fid = res["file_id"]
        if not output_path(fid).exists() or output_path(fid).stat().st_size == 0:
            raise RuntimeError("The conversion produced an empty file.")
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)
    label = f"{stem}.{out_ext}"
    write_meta(fid, source_file_id=file_id, operation=f"convert {ext}→{target}", label=label)
    return {"file_id": fid, "url": public_url(fid), "label": label, "size_kb": output_path(fid).stat().st_size // 1024}
