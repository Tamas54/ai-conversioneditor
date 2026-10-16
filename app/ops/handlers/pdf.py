"""PDF-eszközök PyMuPDF-fel, Ghostscripttel, pikepdf-fel és OCRmyPDF-fel — PdfCraft nélkül, bárhol futnak.
A szöveget, amit rajzolunk (oldalszám, szerkesztés, vízjel), teljes Unicode-betűvel írjuk: az ő/ű is helyes."""
from __future__ import annotations

import asyncio
import difflib
import re
import shutil
from pathlib import Path
from typing import Any

from app.convert.engines import EngineError, available, run
from app.convert.options import OptionError, parse_pages
from app.ops.handlers import font_path, out_name, resolve, rgb, to_pdf


def _open(p: Path):
    import pymupdf
    d = pymupdf.open(str(p))
    if d.needs_pass:
        d.close()
        raise ValueError("This PDF is password-protected — unlock it first.")
    return d


def _save(d, dst: Path) -> Path:
    d.save(str(dst), garbage=3, deflate=True)
    d.close()
    return dst


# ---------- tömörítés ----------

_GS_LEVEL = {"low": "/printer", "recommended": "/ebook", "extreme": "/screen"}


async def compress(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    level = o.get("compression", "recommended")
    res = []
    for src in inputs:
        dst = out_name(out, src)
        if available("ghostscript"):
            await run("gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.6", f"-dPDFSETTINGS={_GS_LEVEL[level]}",
                      "-dNOPAUSE", "-dBATCH", "-dQUIET", "-dSAFER", "-dDetectDuplicateImages=true", "-dCompressFonts=true",
                      "-dSubsetFonts=true", f"-sOutputFile={dst}", str(src), timeout=1200)
        else:
            await asyncio.to_thread(lambda: _save(_open(src), dst))
        if available("qpdf"):                       # objektumfolyamok: még néhány %
            tmp = dst.with_suffix(".q.pdf")
            await run("qpdf", "--object-streams=generate", "--compress-streams=y", "--recompress-flate", str(dst), str(tmp),
                      timeout=600, ok_codes=(0, 3))
            if tmp.exists() and tmp.stat().st_size < dst.stat().st_size:
                tmp.replace(dst)
            tmp.unlink(missing_ok=True)
        if dst.stat().st_size >= src.stat().st_size:     # nem lett kisebb → az eredeti marad
            shutil.copy(src, dst)
        res.append(dst)
    return res


# ---------- egyesítés, bontás, oldalak ----------

async def merge(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    import pymupdf
    ranges = list(o.get("page_ranges") or [])
    work = out / "_src"
    work.mkdir(exist_ok=True)
    pdfs = [await to_pdf(p, work) for p in inputs]

    def go() -> Path:
        res = pymupdf.open()
        for i, p in enumerate(pdfs):
            d = _open(p)
            spec = ranges[i] if i < len(ranges) else None
            pages = parse_pages(None if spec in (None, "", "all") else str(spec), d.page_count)
            for pg in pages:
                res.insert_pdf(d, from_page=pg, to_page=pg)
            d.close()
        return _save(res, out_name(out, inputs[0], "-merged", "pdf"))
    return [await asyncio.to_thread(go)]


async def split(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    import pymupdf
    src = inputs[0]

    def go() -> list[Path]:
        d = _open(src)
        n = d.page_count
        mode = o.get("mode", "ranges")
        if mode == "each":
            groups = [[i] for i in range(n)]
        elif mode == "every":
            k = int(o.get("every") or 1)
            groups = [list(range(i, min(n, i + k))) for i in range(0, n, k)]
        else:
            spec = o.get("page_range")
            if not spec:
                raise OptionError("page_range: give the parts, e.g. 1-3,4-10 (each comma-separated part becomes a file)")
            groups = [parse_pages(part, n) for part in str(spec).split(",") if part.strip()]
        outs = []
        for gi, pages in enumerate(groups, 1):
            part = pymupdf.open()
            for pg in pages:
                part.insert_pdf(d, from_page=pg, to_page=pg)
            outs.append(_save(part, out_name(out, src, f"-{gi:03d}", "pdf")))
        d.close()
        return outs
    return await asyncio.to_thread(go)


async def extract_pages(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    src = inputs[0]

    def go():
        d = _open(src)
        d.select(parse_pages(o.get("page_range") or "1", d.page_count))
        return _save(d, out_name(out, src, "-pages"))
    return [await asyncio.to_thread(go)]


async def delete_pages(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    src = inputs[0]

    def go():
        d = _open(src)
        drop = set(parse_pages(o.get("page_range"), d.page_count)) if o.get("page_range") else set()
        if not drop:
            raise OptionError("page_range: which pages to delete, e.g. 1,4-6")
        keep = [i for i in range(d.page_count) if i not in drop]
        if not keep:
            raise OptionError("that would delete every page")
        d.select(keep)
        return _save(d, out_name(out, src))
    return [await asyncio.to_thread(go)]


async def rotate(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    src = inputs[0]

    def go():
        d = _open(src)
        for i in parse_pages(o.get("page_range"), d.page_count):
            d[i].set_rotation((d[i].rotation + int(o.get("angle") or 90)) % 360)
        return _save(d, out_name(out, src))
    return [await asyncio.to_thread(go)]


async def organize(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    src = inputs[0]

    def go():
        import pymupdf
        d = _open(src)
        if not o.get("order"):
            raise OptionError("order: the new page order, e.g. 3,1,2,4-")
        order: list[int] = []
        for part in str(o["order"]).replace(" ", "").split(","):
            order += parse_pages(part, d.page_count)          # itt az ismétlés megengedett (megkettőzés)
        res = pymupdf.open()
        for pg in order:
            res.insert_pdf(d, from_page=pg, to_page=pg)
        d.close()
        return _save(res, out_name(out, src))
    return [await asyncio.to_thread(go)]


async def crop(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    src = inputs[0]
    m = [float(x) for x in (o.get("margins_mm") or [10, 10, 10, 10])][:4]
    while len(m) < 4:
        m.append(m[-1] if m else 10)

    def go():
        import pymupdf
        d = _open(src)
        k = 72 / 25.4
        for i in parse_pages(o.get("page_range"), d.page_count):
            pg = d[i]
            r = pg.mediabox
            new = pymupdf.Rect(r.x0 + m[3] * k, r.y0 + m[0] * k, r.x1 - m[1] * k, r.y1 - m[2] * k)
            if new.is_empty or new.width < 20 or new.height < 20:
                raise OptionError("the margins are larger than the page")
            pg.set_cropbox(new)
        return _save(d, out_name(out, src))
    return [await asyncio.to_thread(go)]


_SIZES = {"A3": (842, 1191), "A4": (595, 842), "A5": (420, 595), "B5": (499, 709), "Letter": (612, 792),
          "Legal": (612, 1008), "Tabloid": (792, 1224)}


async def resize(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    src = inputs[0]

    def go():
        import pymupdf
        d = _open(src)
        res = pymupdf.open()
        W, H = _SIZES[o.get("page_size", "A4")]
        fit = o.get("fit_page", "fit")
        for i, pg in enumerate(d):
            w, h = (W, H) if pg.rect.width <= pg.rect.height else (H, W)          # tájolás marad
            np = res.new_page(width=w, height=h)
            target = np.rect
            if fit == "fill":
                sr = pg.rect
                scale = max(w / sr.width, h / sr.height)
                cw, ch = w / scale, h / scale
                clip = pymupdf.Rect(sr.x0 + (sr.width - cw) / 2, sr.y0 + (sr.height - ch) / 2,
                                    sr.x0 + (sr.width + cw) / 2, sr.y0 + (sr.height + ch) / 2)
                np.show_pdf_page(target, d, i, clip=clip)
            else:
                np.show_pdf_page(target, d, i, keep_proportion=(fit == "fit"))
        d.close()
        return _save(res, out_name(out, src, f"-{o.get('page_size', 'A4')}"))
    return [await asyncio.to_thread(go)]


# ---------- szabványok: PDF/A, PDF/X, OCR ----------

async def to_pdfa(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    from app.convert.steps import Ctx, ghostscript
    res = []
    for src in inputs:
        r = await ghostscript(src, "pdfa", Ctx(out / "_gs", {"pdfa_level": o.get("pdfa_level", "2b")}))
        dst = out_name(out, src, "-PDFA")
        shutil.move(str(r[0]), dst)
        res.append(dst)
    return res


async def to_pdfx(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    from app.convert.steps import _icc
    res = []
    for src in inputs:
        dst = out_name(out, src, "-PDFX")
        icc = _icc()
        args = ["gs", "-dBATCH", "-dNOPAUSE", "-dQUIET", "-dSAFER", "-dPDFX=3", "-sDEVICE=pdfwrite",
                "-sColorConversionStrategy=RGB", "-dEmbedAllFonts=true", "-dSubsetFonts=true"]
        if icc:
            defs = out / "PDFX_def.ps"
            defs.write_text("%!\n[/_objdef {icc_PDFX} /type /stream /OBJ pdfmark\n"
                            f"[{{icc_PDFX}} <</N 3>> /PUT pdfmark\n[{{icc_PDFX}} ({icc}) (r) file /PUT pdfmark\n"
                            "[/_objdef {OutputIntent_PDFX} /type /dict /OBJ pdfmark\n"
                            "[{OutputIntent_PDFX} <</Type /OutputIntent /S /GTS_PDFX /DestOutputProfile {icc_PDFX} "
                            "/OutputConditionIdentifier (sRGB) /RegistryName (http://www.color.org)>> /PUT pdfmark\n"
                            "[{Catalog} <</OutputIntents [ {OutputIntent_PDFX} ]>> /PUT pdfmark\n"
                            "[ /GTS_PDFXVersion (PDF/X-3:2003) /Title (Document) /Trapped /False /DOCINFO pdfmark\n")
            args += [f"--permit-file-read={icc}", f"-sOutputFile={dst}", str(defs), str(src)]
        else:
            args += [f"-sOutputFile={dst}", str(src)]
        await run(*args, timeout=900)
        res.append(dst)
    return res


async def ocr(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    from app.convert.service import ocr_languages, ocr_pdf
    lang = o.get("ocr_language") or "eng"
    have = ocr_languages()
    missing = [l for l in lang.split("+") if have and l not in have]
    if missing:
        raise OptionError(f"OCR language not installed: {', '.join(missing)} (available: {', '.join(sorted(have))})")
    res = []
    for src in inputs:
        pdf = src
        if src.suffix.lower() != ".pdf":
            pdf = await to_pdf(src, out)
        dst = out_name(out, src, "-searchable", "pdf")
        await ocr_pdf(pdf, dst, lang, bool(o.get("deskew", True)))
        res.append(dst)
    return res


# ---------- védelem ----------

async def protect(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    import secrets
    import pikepdf
    pw = o.get("password") or ""
    if len(pw) < 4:
        raise OptionError("password: at least 4 characters")
    owner = o.get("owner_password") or secrets.token_urlsafe(18)
    allow = pikepdf.Permissions(print_lowres=bool(o.get("allow_print", True)), print_highres=bool(o.get("allow_print", True)),
                                extract=bool(o.get("allow_copy", True)), accessibility=True,
                                modify_annotation=bool(o.get("allow_annotate", True)), modify_form=bool(o.get("allow_annotate", True)),
                                modify_other=bool(o.get("allow_edit", False)), modify_assembly=bool(o.get("allow_edit", False)))
    res = []
    for src in inputs:
        dst = out_name(out, src, "-protected")

        def go():
            with pikepdf.open(str(src)) as p:
                p.save(str(dst), encryption=pikepdf.Encryption(user=pw, owner=owner, allow=allow, aes=True, R=6))
        await asyncio.to_thread(go)
        res.append(dst)
    return res


async def unlock(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    import pikepdf
    res = []
    for src in inputs:
        dst = out_name(out, src, "-unlocked")

        def go():
            try:
                with pikepdf.open(str(src), password=o.get("password") or "") as p:
                    p.save(str(dst))
            except pikepdf.PasswordError:
                raise ValueError("Wrong password — the PDF can't be opened.")
        await asyncio.to_thread(go)
        res.append(dst)
    return res


async def flatten(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    what = o.get("what", "all")
    res = []
    for src in inputs:
        dst = out_name(out, src, "-flat")
        shutil.copy(src, dst)

        def go():
            if what in ("all", "forms"):
                from app.tools.forms import _bake            # ékezetbiztos űrlap-lapítás (ő, ű)
                _bake(dst)
            if what in ("all", "annotations"):
                d = _open(dst)
                d.bake(annots=True, widgets=False)
                tmp = dst.with_suffix(".b.pdf")
                d.save(str(tmp), garbage=3, deflate=True)
                d.close()
                tmp.replace(dst)
        await asyncio.to_thread(go)
        res.append(dst)
    return res


async def extract_images(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    src = inputs[0]
    mn = int(o.get("min_size") or 32)

    def go():
        d = _open(src)
        outs, seen = [], set()
        for pno, pg in enumerate(d, 1):
            for k, img in enumerate(pg.get_images(full=True), 1):
                xref = img[0]
                if xref in seen:
                    continue
                seen.add(xref)
                info = d.extract_image(xref)
                if not info or info["width"] < mn or info["height"] < mn:
                    continue
                p = out_name(out, src, f"-p{pno:03d}-{k:02d}", info["ext"] if info["ext"] != "jpx" else "jp2")
                p.write_bytes(info["image"])
                outs.append(p)
        d.close()
        if not outs:
            raise ValueError("No images found in this PDF.")
        return outs
    return await asyncio.to_thread(go)


# ---------- jegyzetek, szerkesztés, kitakarás, oldalszám ----------

async def annotate(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    import pymupdf
    src = inputs[0]
    items = o.get("annotations") or []
    if not items:
        raise OptionError("annotations: a list like [{\"type\": \"highlight\", \"text\": \"…\"}, {\"type\": \"note\", \"page\": 1, \"x\": 72, \"y\": 72, \"text\": \"…\"}]")

    def go():
        d = _open(src)
        for a in items:
            kind = a.get("type", "note")
            pages = [int(a["page"]) - 1] if a.get("page") else range(d.page_count)
            color = rgb(a.get("color"), (1, 0.85, 0) if kind == "highlight" else (0.85, 0.1, 0.1))
            for pi in pages:
                if not 0 <= pi < d.page_count:
                    raise OptionError(f"page {pi + 1} doesn't exist")
                pg = d[pi]
                if kind in ("highlight", "underline", "strikeout") and a.get("text"):
                    for q in pg.search_for(str(a["text"]), quads=True):
                        an = {"highlight": pg.add_highlight_annot, "underline": pg.add_underline_annot,
                              "strikeout": pg.add_strikeout_annot}[kind](q)
                        an.set_colors(stroke=color)
                        an.update()
                    continue
                if a.get("page") is None:
                    continue
                rect = pymupdf.Rect(*(a.get("rect") or [a.get("x", 72), a.get("y", 72), a.get("x", 72) + 160, a.get("y", 72) + 40]))
                if kind == "note":
                    an = pg.add_text_annot(rect.tl, str(a.get("text", "")))
                elif kind == "text":
                    an = pg.add_freetext_annot(rect, str(a.get("text", "")), fontsize=float(a.get("size", 11)),
                                               fontname="helv", text_color=color)
                elif kind == "rect":
                    an = pg.add_rect_annot(rect)
                    an.set_colors(stroke=color)
                elif kind == "line":
                    an = pg.add_line_annot(rect.tl, rect.br)
                    an.set_colors(stroke=color)
                else:
                    raise OptionError(f"unknown annotation type {kind}")
                an.update()
        return _save(d, out_name(out, src, "-annotated"))
    return [await asyncio.to_thread(go)]


async def edit(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    import pymupdf
    src = inputs[0]
    items = o.get("edits") or []
    if not items:
        raise OptionError("edits: e.g. [{\"type\": \"text\", \"page\": 1, \"x\": 72, \"y\": 100, \"text\": \"Új szöveg ő ű\"}]")
    fp = font_path()
    images = {e.get("file_id"): o["_resolve"](e["file_id"]) for e in items if e.get("type") == "image" and e.get("file_id")}

    def go():
        d = _open(src)
        for e in items:
            pi = int(e.get("page", 1)) - 1
            if not 0 <= pi < d.page_count:
                raise OptionError(f"page {pi + 1} doesn't exist")
            pg = d[pi]
            kind = e.get("type")
            if kind == "text":
                if fp:
                    pg.insert_font(fontname="UniE", fontfile=fp)
                pg.insert_text(pymupdf.Point(float(e.get("x", 72)), float(e.get("y", 72))), str(e.get("text", "")),
                               fontsize=float(e.get("size", 12)), fontname="UniE" if fp else "helv", color=rgb(e.get("color")))
            elif kind == "whiteout":
                pg.draw_rect(pymupdf.Rect(*e["rect"]), color=None, fill=rgb(e.get("color"), (1, 1, 1)), overlay=True)
            elif kind == "image":
                pg.insert_image(pymupdf.Rect(*e["rect"]), filename=str(images[e["file_id"]]), keep_proportion=True)
            else:
                raise OptionError(f"unknown edit type {kind} (text, image, whiteout)")
        return _save(d, out_name(out, src, "-edited"))
    return [await asyncio.to_thread(go)]


PATTERNS = {
    "email": r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
    "phone": r"(?:\+?\d{1,3}[\s/-]?)?(?:\(?\d{1,4}\)?[\s/-]?)?\d{3}[\s-]?\d{3,4}",
    "credit-card": r"\b(?:\d[ -]?){13,19}\b",
    "ssn": r"\b\d{3}[- ]?\d{3}[- ]?\d{3}\b|\b\d{3}-\d{2}-\d{4}\b",
    "date": r"\b\d{4}[.\-/]\s?\d{1,2}[.\-/]\s?\d{1,2}\.?|\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b",
}


async def redact(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    import pymupdf
    src = inputs[0]
    words = [w for w in (o.get("words") or []) if str(w).strip()]
    pats = [p for p in (o.get("patterns") or []) if p in PATTERNS]
    if not words and not pats:
        raise OptionError("Give words to black out, or patterns (email, phone, credit-card, ssn, date).")

    def go():
        d = _open(src)
        hits = 0
        for pg in d:
            rects = []
            for w in words:
                rects += pg.search_for(str(w))
            if pats:
                for line in _lines(pg):
                    text = " ".join(t for t, _ in line)
                    for p in pats:
                        for m in re.finditer(PATTERNS[p], text):
                            rects += _span_rects(line, m.start(), m.end())
            for r in rects:
                pg.add_redact_annot(r, fill=(0, 0, 0))
            hits += len(rects)
            if rects:
                pg.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS)
        if not hits:
            d.close()
            raise ValueError("Nothing to black out — none of those words or patterns appear in this PDF.")
        d.set_metadata({})
        return _save(d, out_name(out, src, "-redacted"))
    return [await asyncio.to_thread(go)]


def _lines(pg):
    """Szavak soronként: [(szó, téglalap), …]."""
    import pymupdf
    rows: dict[tuple, list] = {}
    for x0, y0, x1, y1, w, b, l, _ in pg.get_text("words"):
        rows.setdefault((b, l), []).append((w, pymupdf.Rect(x0, y0, x1, y1)))
    return list(rows.values())


def _span_rects(line, start: int, end: int):
    pos, out = 0, []
    for w, r in line:
        a, b = pos, pos + len(w)
        if b > start and a < end:
            out.append(r)
        pos = b + 1
    return out


async def page_numbers(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    import pymupdf
    src = inputs[0]
    fp = font_path()
    pos = o.get("position", "bottom")
    if pos == "center":
        pos = "bottom"
    fmt = o.get("text_format") or "{n}"
    first = int(o.get("start_number", 1))

    def go():
        d = _open(src)
        total = d.page_count
        font = pymupdf.Font(fontfile=fp) if fp else pymupdf.Font("helv")
        for i, pg in enumerate(d):
            text = fmt.replace("{n}", str(first + i)).replace("{total}", str(total + first - 1))
            size = 10
            w = font.text_length(text, fontsize=size)
            r = pg.rect
            m = 28
            x = {"left": m, "top-left": m, "bottom-left": m, "right": r.width - m - w, "top-right": r.width - m - w,
                 "bottom-right": r.width - m - w}.get(pos, (r.width - w) / 2)
            y = m + size if pos.startswith("top") else r.height - m
            if fp:
                pg.insert_font(fontname="UniN", fontfile=fp)
            pg.insert_text(pymupdf.Point(x, y), text, fontsize=size, fontname="UniN" if fp else "helv", color=(0.25, 0.25, 0.25))
        return _save(d, out_name(out, src, "-numbered"))
    return [await asyncio.to_thread(go)]


async def compare(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    """A második (új) változat másolata: a beszúrt szavak zölddel kiemelve, a törölt szavak oldalankénti jegyzetben;
    elöl egy összesítő oldal."""
    import pymupdf
    old, new = inputs[0], inputs[1]

    def go():
        a, b = _open(old), _open(new)
        fp = font_path()
        summary = []
        for i in range(max(a.page_count, b.page_count)):
            wa = [w[4] for w in a[i].get_text("words")] if i < a.page_count else []
            pb = b[i] if i < b.page_count else None
            wb = pb.get_text("words") if pb is not None else []
            sm = difflib.SequenceMatcher(None, wa, [w[4] for w in wb], autojunk=False)
            ins = dele = 0
            removed = []
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag in ("replace", "insert") and pb is not None:
                    for w in wb[j1:j2]:
                        h = pb.add_highlight_annot(pymupdf.Rect(w[:4]))
                        h.set_colors(stroke=(0.55, 0.9, 0.55))
                        h.update()
                    ins += j2 - j1
                if tag in ("replace", "delete"):
                    removed += wa[i1:i2]
                    dele += i2 - i1
            if removed and pb is not None:
                pb.add_text_annot(pymupdf.Point(20, 20), "Removed: " + " ".join(removed)[:2000]).update()
            summary.append((i + 1, ins, dele))
        rep = b.new_page(0, width=595, height=842)
        if fp:
            rep.insert_font(fontname="UniC", fontfile=fp)
        fn = "UniC" if fp else "helv"
        lines = [f"Comparison: {old.name} → {new.name}", ""]
        lines += [f"Page {p}: {ins} words added, {dl} removed" for p, ins, dl in summary if ins or dl] or ["No text differences."]
        y = 72
        for ln in lines[:60]:
            rep.insert_text(pymupdf.Point(56, y), ln, fontsize=11, fontname=fn)
            y += 16
        a.close()
        return _save(b, out_name(out, new, "-comparison", "pdf"))
    return [await asyncio.to_thread(go)]
