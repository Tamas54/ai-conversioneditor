"""
convert tool — the main conversion engine.

Inputs accept either:
    - file_id (already uploaded via /upload), OR
    - source_url (will be downloaded)
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings
from app.storage import (
    input_path,
    new_file_id,
    output_path,
    public_url,
    save_bytes,
    temp_path,
)


def _existing_path(file_id: str) -> Optional[Path]:
    """Look for file_id in both inputs and outputs (chained edits land in outputs)."""
    p = input_path(file_id)
    if p.exists():
        return p
    p = output_path(file_id)
    if p.exists():
        return p
    return None
from app.tools.formats import LO_FILTERS, detect_from_path, lo_filter_for, normalize_format, route

log = logging.getLogger("aice.convert")


async def _resolve_input(
    file_id: Optional[str], source_url: Optional[str], source_format: Optional[str]
) -> Path:
    if file_id:
        p = _existing_path(file_id)
        if p is None:
            raise FileNotFoundError(f"file_id not found: {file_id}")
        return p
    if source_url:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(source_url)
            r.raise_for_status()
            ext = source_format or Path(source_url.split("?")[0]).suffix or "bin"
            fid = new_file_id(ext)
            p = input_path(fid)
            await save_bytes(p, r.content)
            return p
    raise ValueError("Either file_id or source_url is required")


async def convert(
    pool,
    *,
    file_id: Optional[str] = None,
    source_url: Optional[str] = None,
    source_format: Optional[str] = None,
    target_format: str,
    use_browser: bool = False,
    options: Optional[dict] = None,
) -> dict:
    """Convert a document. Returns {file_id, url, size_kb, ms_elapsed, backend}."""
    t0 = time.time()
    options = options or {}

    src_path = await _resolve_input(file_id, source_url, source_format)
    src_fmt = normalize_format(source_format or detect_from_path(src_path))
    tgt_fmt = normalize_format(target_format)

    backend = route(src_fmt, tgt_fmt, use_browser=use_browser)
    log.info("convert: %s → %s via %s", src_fmt, tgt_fmt, backend)

    out_id = new_file_id(tgt_fmt)
    out_path = output_path(out_id)
    # Lineage metadata — best-effort (won't block on failure).
    from app.storage import write_meta, display_label
    src_label = (display_label(file_id) if file_id else
                 (Path(source_url).name if source_url else "unknown"))
    write_meta(
        out_id,
        source_file_id=file_id,
        operation=f"convert→{tgt_fmt}",
        label=f"{Path(src_label).stem}.{tgt_fmt}",
    )

    if backend == "noop":
        shutil.copy2(src_path, out_path)

    elif backend == "libreoffice":
        target_filter = lo_filter_for(src_fmt, tgt_fmt) or LO_FILTERS.get(tgt_fmt, tgt_fmt)
        worker = await pool.acquire_lo()
        try:
            tmp_dir = temp_path()
            tmp_dir.mkdir(parents=True, exist_ok=True)
            produced = await worker.convert(src_path, tmp_dir, target_filter)
            shutil.move(str(produced), out_path)
            shutil.rmtree(tmp_dir, ignore_errors=True)
        finally:
            await pool.release_lo(worker)

    elif backend == "weasyprint":
        from weasyprint import HTML

        html_text = src_path.read_text(encoding="utf-8")
        HTML(string=html_text, base_url=str(src_path.parent)).write_pdf(str(out_path))

    elif backend == "chromium":
        ctx, page = await pool.acquire_chromium_page()
        try:
            await page.goto(f"file://{src_path.resolve()}", wait_until="networkidle")
            pdf_options = {
                "path": str(out_path),
                "format": options.get("page_size", "A4"),
                "print_background": True,
                "margin": options.get("margin", {
                    "top": "20mm", "bottom": "20mm",
                    "left": "20mm", "right": "20mm",
                }),
            }
            await page.pdf(**pdf_options)
        finally:
            await pool.release_chromium_page(ctx, page)

    elif backend == "pandoc":
        import pypandoc

        pypandoc.convert_file(
            str(src_path),
            tgt_fmt,
            outputfile=str(out_path),
            extra_args=options.get("pandoc_args", []),
        )

    elif backend == "md_to_pdf":
        import pypandoc
        from weasyprint import HTML

        html_tmp = temp_path("md.html")
        pypandoc.convert_file(
            str(src_path), "html", outputfile=str(html_tmp),
            extra_args=["--standalone", "--metadata", f"title={src_path.stem}"],
        )
        HTML(filename=str(html_tmp)).write_pdf(str(out_path))
        html_tmp.unlink(missing_ok=True)

    elif backend == "pdf2docx":
        try:
            from pdf2docx import Converter

            cv = Converter(str(src_path))
            cv.convert(str(out_path))
            cv.close()
        except ImportError:
            # Fallback: LibreOffice
            log.warning("pdf2docx not installed, falling back to LibreOffice")
            worker = await pool.acquire_lo()
            try:
                tmp_dir = temp_path()
                tmp_dir.mkdir(parents=True, exist_ok=True)
                produced = await worker.convert(src_path, tmp_dir, "docx")
                shutil.move(str(produced), out_path)
                shutil.rmtree(tmp_dir, ignore_errors=True)
            finally:
                await pool.release_lo(worker)

    elif backend == "via_docx":
        # PDF → DOCX (pdf2docx) → tgt_fmt (LO or pandoc).
        # Used for any PDF target other than DOCX itself, since LO can't
        # export from a Draw document (which is how it opens PDFs).
        from pdf2docx import Converter as _Pdf2docx

        intermediate_dir = temp_path()
        intermediate_dir.mkdir(parents=True, exist_ok=True)
        intermediate_docx = intermediate_dir / f"{src_path.stem}.docx"
        cv = _Pdf2docx(str(src_path))
        cv.convert(str(intermediate_docx))
        cv.close()

        # Pick a sub-backend for DOCX → tgt_fmt
        # Pandoc handles md/html/rtf cleanly; txt has no pandoc target so route via LO.
        pandoc_text_flow = {"md", "html", "htm", "rtf"}
        if tgt_fmt == "docx":
            shutil.move(str(intermediate_docx), out_path)
        elif tgt_fmt in pandoc_text_flow:
            import pypandoc
            pypandoc.convert_file(
                str(intermediate_docx), tgt_fmt,
                outputfile=str(out_path),
                extra_args=options.get("pandoc_args", []),
            )
        else:
            # LibreOffice for txt, odt, pptx, xlsx, etc. — DOCX is Writer class
            target_filter = lo_filter_for("docx", tgt_fmt) or LO_FILTERS.get(tgt_fmt, tgt_fmt)
            worker = await pool.acquire_lo()
            try:
                tmp_dir = temp_path()
                tmp_dir.mkdir(parents=True, exist_ok=True)
                produced = await worker.convert(intermediate_docx, tmp_dir, target_filter)
                shutil.move(str(produced), out_path)
                shutil.rmtree(tmp_dir, ignore_errors=True)
            finally:
                await pool.release_lo(worker)

        shutil.rmtree(intermediate_dir, ignore_errors=True)

    elif backend == "via_pdf":
        # Cross-class conversion (e.g. PPTX → DOCX, XLSX → DOCX): go via PDF.
        # Step 1: source → PDF using its own class's PDF filter.
        intermediate_dir = temp_path()
        intermediate_dir.mkdir(parents=True, exist_ok=True)
        src_to_pdf_filter = lo_filter_for(src_fmt, "pdf")
        if src_to_pdf_filter is None:
            raise RuntimeError(f"via_pdf: no PDF filter for source {src_fmt}")
        worker = await pool.acquire_lo()
        try:
            produced_pdf = await worker.convert(src_path, intermediate_dir, src_to_pdf_filter)
        finally:
            await pool.release_lo(worker)

        if tgt_fmt == "pdf":
            shutil.move(str(produced_pdf), out_path)
        else:
            # Step 2: PDF → tgt via pdf2docx + LO (re-use via_docx logic)
            from pdf2docx import Converter as _Pdf2docx
            intermediate_docx = intermediate_dir / f"{src_path.stem}.docx"
            cv = _Pdf2docx(str(produced_pdf))
            cv.convert(str(intermediate_docx))
            cv.close()

            pandoc_text_flow = {"md", "html", "htm", "rtf"}
            if tgt_fmt == "docx":
                shutil.move(str(intermediate_docx), out_path)
            elif tgt_fmt in pandoc_text_flow:
                import pypandoc
                pypandoc.convert_file(
                    str(intermediate_docx), tgt_fmt,
                    outputfile=str(out_path),
                    extra_args=options.get("pandoc_args", []),
                )
            else:
                # Try Writer-class filter first; if missing, fall back to plain ext
                # (LO will try to auto-pick a filter from the registered loaders).
                target_filter = lo_filter_for("docx", tgt_fmt) or LO_FILTERS.get(tgt_fmt) or tgt_fmt
                worker = await pool.acquire_lo()
                try:
                    tmp_dir = temp_path()
                    tmp_dir.mkdir(parents=True, exist_ok=True)
                    produced = await worker.convert(intermediate_docx, tmp_dir, target_filter)
                    shutil.move(str(produced), out_path)
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                finally:
                    await pool.release_lo(worker)

        shutil.rmtree(intermediate_dir, ignore_errors=True)

    elif backend == "via_md_docx":
        # MD → DOCX (pandoc) → tgt (LO)
        import pypandoc
        intermediate_dir = temp_path()
        intermediate_dir.mkdir(parents=True, exist_ok=True)
        intermediate_docx = intermediate_dir / f"{src_path.stem}.docx"
        pypandoc.convert_file(str(src_path), "docx", outputfile=str(intermediate_docx))

        if tgt_fmt == "docx":
            shutil.move(str(intermediate_docx), out_path)
        else:
            target_filter = lo_filter_for("docx", tgt_fmt)
            if target_filter is None:
                raise RuntimeError(f"via_md_docx: no Writer→{tgt_fmt} filter")
            worker = await pool.acquire_lo()
            try:
                tmp_dir = temp_path()
                tmp_dir.mkdir(parents=True, exist_ok=True)
                produced = await worker.convert(intermediate_docx, tmp_dir, target_filter)
                shutil.move(str(produced), out_path)
                shutil.rmtree(tmp_dir, ignore_errors=True)
            finally:
                await pool.release_lo(worker)

        shutil.rmtree(intermediate_dir, ignore_errors=True)

    else:
        raise RuntimeError(f"Backend {backend} not implemented")

    size_kb = out_path.stat().st_size // 1024
    ms = int((time.time() - t0) * 1000)
    return {
        "file_id": out_id,
        "url": public_url(out_id),
        "size_kb": size_kb,
        "ms_elapsed": ms,
        "backend": backend,
        "source_format": src_fmt,
        "target_format": tgt_fmt,
    }
