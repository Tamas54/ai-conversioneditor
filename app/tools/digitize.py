"""Régi könyv, fotózott/szkennelt oldal → szöveg, Word, új PDF, kereshető PDF.

Lépések: oldalak (képek sorban; PDF → oldalanként 200 dpi JPEG) → OCR oldalanként (Kimi vision, „könyv” prompt,
Postgres ocr_cache: ugyanazt az oldalt nem olvassuk ki kétszer) → oldalak összefűzése Markdownná (oldalhatáron
átnyúló bekezdés és elválasztás visszaillesztve, lábjegyzetek oldalanként egyedi azonosítóval) → kimenetek:
    md · txt · docx (pandoc) · pdf (új, tiszta szedés: docx → WordCraft) · searchable_pdf (eredeti oldalképek +
    láthatatlan szövegréteg: a Ctrl+F és a kijelölés-másolás működik, a külalak a régi marad).
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import shutil
import textwrap
from pathlib import Path
from typing import Any, Callable, Optional

from app.config import settings
from app.storage import display_label, output_path, public_url, temp_path, write_meta
from app.storage.files import new_file_id

log = logging.getLogger("aice.digitize")

PROMPT_VER = "book-v5"
# Mérve 10-11 (tests/ocr_book_bench.py; etalon: Ammianus 1916 kézi átirat + 3 Jókai-fotó pontos szöveggel, 2048 px):
#   DeepSeek-V4.1-Flash 1,8% régi / 4,6% össz., 14 s/oldal, ~0,09 ¢/oldal  ← alap
#   Kimi-K2.6           2,2% / 4,5%, 37 s, ~0,65 ¢      GLM-5.3-Flash 3,5% / 5,8%, 30 s, ~0,16 ¢
#   Qwen3.8-27B 4,9–40% (modernizál, kihagy), ABBYY-réteg 9,3%, Kimi-K3 üres szöveg.
# A DeepSeek ábrát csak „először a képek” sorrendű utasítással jelöl megbízhatóan (ezért ilyen a prompt).
OCR_EDGE = 2048
FIGURE_KEEP = {"illustration", "photo", "map", "diagram", "ornament", "emblem", "chart", "table"}
OUTPUTS = {"docx", "pdf", "searchable_pdf", "txt", "md", "fillable_pdf"}
_IMG = {"png", "jpg", "jpeg", "webp", "tif", "tiff", "bmp", "gif"}
_CONCURRENCY = 8
_FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
          "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf")

_PROMPT = """You are digitising a scanned page.{hint}
Step 1 — pictures. Look for anything that is not running text: illustrations, engravings, photos, maps,
diagrams, charts, decorative ornaments, vignettes, headpieces, publisher's emblems, and also library stamps and
handwriting. For EACH one write exactly one line in this exact format (integers 0–1000 = thousandths of the page
width and height, origin top left; draw the box generously so nothing is cut off):
[[FIGURE kind x0 y0 x1 y1 | caption]]
kind is one word: illustration, photo, map, diagram, chart, ornament, emblem, stamp, handwriting or other;
caption = the printed caption, or a few words saying what it shows.
Step 2 — transcribe the page:
- Copy the text exactly as printed: keep the original spelling, punctuation and language. Do not translate,
  modernise or correct anything.
- Join the lines of a paragraph into one line; rejoin words hyphenated at the end of a line.
- Leave one blank line between paragraphs.
- Mark a chapter title with "# " and a section heading with "## ".
- Centred display text (title page, half-title, dedication, motto, imprint) goes between a line ":::center"
  and a line ":::", one printed line per line.
- Leave out running heads (the book or chapter title repeated at the top of the page), page numbers and
  printer's marks. Never transcribe the text of stamps or handwriting.
- Footnotes go after the page text, each on its own line as "[^1]: text"; mark the place in the text as [^1].
- A table becomes a Markdown table. An illegible word becomes [?].
- Put each [[FIGURE …]] line from step 1 where the picture stands in the text (before the text that follows it).
- If the page has no text and no pictures, reply with nothing.
Output only the result: no comments, no code fences."""


# ---------- oldalak ----------

def _image_page(src: Path, dst: Path) -> None:
    from PIL import Image, ImageOps
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        if max(im.size) > 3000:          # telefonfotó: elég a 3000 px (OCR 1280-ra kicsinyít úgyis)
            im.thumbnail((3000, 3000))
        im.save(dst, "JPEG", quality=90)


async def _pdf_pages(src: Path, workdir: Path, limit: int) -> list[Path]:
    proc = await asyncio.create_subprocess_exec(
        "pdftoppm", "-r", "300", "-jpeg", "-jpegopt", "quality=90", "-l", str(limit), str(src), str(workdir / "p"),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise ValueError(f"Could not read this PDF: {err.decode(errors='replace')[:200]}")
    return sorted(workdir.glob("p-*.jpg"))


async def collect_pages(file_ids: list[str], workdir: Path, limit: int) -> list[Path]:
    from app.tools.crafts import existing
    pages: list[Path] = []
    for i, fid in enumerate(file_ids):
        src = existing(fid)
        ext = src.suffix.lstrip(".").lower()
        if ext == "pdf":
            sub = workdir / f"f{i:03d}"
            sub.mkdir()
            pages += await _pdf_pages(src, sub, limit - len(pages))
        elif ext in _IMG:
            dst = workdir / f"f{i:03d}.jpg"
            await asyncio.to_thread(_image_page, src, dst)
            try:   # telefonnal fotózott oldal: papír megkeresése, perspektíva kiegyenesítése (szkennen nincs változás)
                from app.tools.forms import straighten
                await asyncio.to_thread(straighten, dst, dst)
            except Exception:
                log.warning("kiegyenesítés nem sikerült: %s", dst.name, exc_info=True)
            pages.append(dst)
        else:
            raise ValueError(f"{display_label(fid)}: use photos/scans (JPG, PNG, TIFF…) or a scanned PDF.")
        if len(pages) >= limit:
            return pages[:limit]
    return pages


# ---------- OCR ----------

async def _ocr_page(path: Path, hint: Optional[str]) -> tuple[str, bool]:
    from app import db
    from app.llm import vision
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    ver = PROMPT_VER + (f":{hint}" if hint else "")
    try:
        cached = await db.ocr_cache_get(sha, ver, settings.OCR_MODEL)
    except Exception:
        cached = None
    if cached is not None:
        return cached, True
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": _PROMPT.format(hint=f" The text is in {hint}." if hint else "")},
        {"type": "image_url", "image_url": {"url": vision.image_to_data_url(path, long_edge=OCR_EDGE)}},
    ]}]
    text = _clean(await _vision_text(msgs))
    try:
        await db.ocr_cache_put(sha, ver, settings.OCR_MODEL, text)
    except Exception:
        log.warning("ocr_cache írás nem sikerült", exc_info=True)
    return text, False


async def _vision_text(msgs: list[dict[str, Any]]) -> str:
    """OCR_MODEL (alap: DeepSeek-V4.1-Flash), hibánál a régi vision-modell (Kimi-K2.6)."""
    from app.llm import vision
    try:
        return await vision._call(msgs, max_tokens=6000, model=settings.OCR_MODEL)
    except Exception as e:
        if settings.OCR_MODEL == settings.LLM_MODEL_VISION:
            raise
        log.warning("OCR %s nem válaszolt (%s) — tartalék: %s", settings.OCR_MODEL, e, settings.LLM_MODEL_VISION)
        return await vision._call(msgs, max_tokens=6000)


def _clean(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```(?:markdown|md|text)?\s*\n?", "", s)
    s = re.sub(r"\n?```$", "", s)
    return s.strip()


# ---------- ábrák ----------

# engedékeny: a „FIGURE” szó és a kind elmaradhat, a koordináta lehet 0–1 tört is (a modellek néha így írják)
_NUM = r"(\d+(?:\.\d+)?)"
_FIG = re.compile(r"^\s*\[\[(?:FIGURE\s+)?(?:([a-z]+)\s+)?" + r"[,\s]+".join([_NUM] * 4) + r"\s*(?:\|\s*(.*?))?\]\]\s*$", re.M | re.I)


def _box(m: re.Match) -> tuple[int, int, int, int]:
    v = [float(m.group(i)) for i in range(2, 6)]
    if max(v) <= 1.0:
        v = [x * 1000 for x in v]
    return tuple(int(x) for x in v)


def _crop_figure(page: Path, box: tuple[int, int, int, int], dst: Path) -> bool:
    """A modell doboza (ezrelék) kicsit tágítva, aztán a festékhez vágva (a dobozok gyakran szűkek)."""
    from PIL import Image, ImageOps
    with Image.open(page) as im:
        w, h = im.size
        x0, y0, x1, y1 = (v / 1000 for v in box)
        if x1 <= x0 or y1 <= y0 or (x1 - x0) * (y1 - y0) < 0.002:
            return False
        px, py = (x1 - x0) * 0.12, (y1 - y0) * 0.12
        region = (max(0, int((x0 - px) * w)), max(0, int((y0 - py) * h)), min(w, int((x1 + px) * w)), min(h, int((y1 + py) * h)))
        crop = im.crop(region).convert("RGB")
        gray = ImageOps.grayscale(crop)
        hist = gray.histogram()
        total, acc, median = sum(hist), 0, 128
        for i, n in enumerate(hist):
            acc += n
            if acc >= total / 2:
                median = i; break
        ink = gray.point(lambda v: 1 if v < median - 45 else 0)
        px_ = ink.load()
        cw, ch = ink.size
        rows = [sum(px_[x, y] for x in range(0, cw, 2)) for y in range(ch)]
        cols = [sum(px_[x, y] for y in range(0, ch, 2)) for x in range(cw)]
        y0c, y1c = _ink_span(rows, ch // 2, max(12, int(0.012 * h)))
        x0c, x1c = _ink_span(cols, cw // 2, max(12, int(0.012 * w)))
        if y1c - y0c < 30 or x1c - x0c < 30:
            return False
        m = int(0.015 * max(w, h))
        crop = crop.crop((max(0, x0c - m), max(0, y0c - m), min(cw, x1c + m), min(ch, y1c + m)))
        # 300 dpi-vel kivágva → a Word/PDF az eredeti nyomtatott méretben teszi be (dpi nélkül 96-tal számolna: 3× nagy)
        dpi = 300 if page.parent.name.startswith("f") else max(w, h) / 11.69
        crop.save(dst, "JPEG", quality=90, dpi=(dpi, dpi))
        return True


def _ink_span(profile: list[int], center: int, gap: int) -> tuple[int, int]:
    """A középponttól kifelé addig tart, amíg `gap` hosszú üres sáv (fehér rés) nem jön — így a kép alatti
    betűsor nem kerül bele. Ha a közép éppen üres, a legközelebbi festékes ponttól indul."""
    n = len(profile)
    thr = max(1, max(profile) * 0.01)
    full = [v > thr for v in profile]
    if not any(full):
        return 0, 0
    if not full[center]:
        center = min((i for i in range(n) if full[i]), key=lambda i: abs(i - center))
    lo = hi = center
    run = 0
    for i in range(center, -1, -1):
        run = 0 if full[i] else run + 1
        if run >= gap:
            break
        if full[i]:
            lo = i
    run = 0
    for i in range(center, n):
        run = 0 if full[i] else run + 1
        if run >= gap:
            break
        if full[i]:
            hi = i
    return lo, hi + 1


def place_figures(texts: list[str], pages: list[Path], figdir: Path) -> tuple[list[str], list[str], int]:
    """[[FIGURE …]] → Markdown-kép (docx/pdf-hez) és „[Figure: …]” (txt/md-hez); a pecsét/kézírás kiesik."""
    with_img, plain, kept = [], [], 0
    for n, (text, page) in enumerate(zip(texts, pages), 1):
        k = 0

        def sub(m: re.Match, as_image: bool) -> str:
            nonlocal k, kept
            kind = (m.group(1) or "illustration").lower()
            caption = (m.group(6) or "").strip()
            if kind not in FIGURE_KEEP:
                return ""
            if not as_image:
                return f"[Figure: {caption}]" if caption else "[Figure]"
            k += 1
            dst = figdir / f"p{n:04d}-{k}.jpg"
            if not _crop_figure(page, _box(m), dst):
                return ""
            kept += 1
            return f"\n\n![{caption}]({dst.name})\n\n"     # külön bekezdés → pandoc ábraként, középre
        with_img.append(_FIG.sub(lambda m: sub(m, True), text))
        plain.append(_FIG.sub(lambda m: sub(m, False), text))
    return with_img, plain, kept


# ---------- összefűzés ----------

_END = re.compile(r"[.!?:;…\"'”»)\]]$")


def assemble(pages: list[str]) -> str:
    """Oldalak → egy Markdown. Oldalhatáron folytatódó bekezdés/elválasztás összeillesztve;
    lábjegyzet-azonosítók oldalanként egyediek ([^1] → [^p3-1])."""
    # élőfej, amit a modell címsornak jelölt („## BEVEZETÉS I.”): ugyanaz a (számok nélküli) első sor ≥2 oldalon
    keys = [_head_key(p) for p in pages]
    repeated = {k for k in keys if k and keys.count(k) >= 2}
    seen: set[str] = set()
    out = ""
    for n, page in enumerate(pages, 1):
        page = page.strip()
        k = keys[n - 1]
        if k in repeated:
            if k in seen:            # az első előfordulás a valódi fejezetcím, a későbbiek élőfejek
                page = page.partition("\n")[2].lstrip("\n")
            seen.add(k)
        page = re.sub(r"\[\^(\w+)\]", lambda m: f"[^p{n}-{m.group(1)}]", _drop_running_head(page) if n > 1 else page)
        if not page:
            continue
        body, notes = _split_notes(page)
        if out and body:
            tail = out.rstrip()
            first = body.lstrip()
            if tail.endswith("-") and first[:1].islower():
                out = tail[:-1] + first
            elif not _END.search(tail) and first[:1].islower() and not tail.endswith("|"):
                out = tail + " " + first
            else:
                out = tail + "\n\n" + first
        else:
            out = (out.rstrip() + "\n\n" if out else "") + body
        if notes:
            out += "\n\n" + notes
    # definíció nélküli jel (a könyv végén lévő jegyzetekre mutat) → felső index, ne „[^p3-2]” maradjon
    defined = set(re.findall(r"^\[\^([\w-]+)\]:", out, flags=re.M))
    out = re.sub(r"\[\^(p\d+-(\w+))\](?!:)", lambda m: m.group(0) if m.group(1) in defined else f"^{m.group(2)}^", out)
    return out.strip() + "\n"


def _head_key(page: str) -> str:
    """Az oldal első sora összehasonlítható formában (# jelek, számok, római számok, írásjelek nélkül)."""
    first = page.strip().partition("\n")[0]
    if len(first) > 80 or first.startswith(("!", "[[", "|", ":::")):
        return ""
    s = re.sub(r"[#\d.,;:()\[\]\-–—]+", " ", first).lower()
    s = re.sub(r"\b[ivxlcdm]+\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_ROMAN = re.compile(r"^[\dIVXLCDM\s.,;:\-–—]+$")


def _drop_running_head(page: str) -> str:
    """Az oldal első sora, ha élőfej: rövid, nem címsor (#), és csupa nagybetű vagy oldalszám/fejezetszám
    („ELŐSZÓ.”, „80 XV, 13, 2–4.”, „CONSTANTIUS ÉS JULIANUS. 356. 97”). A modell néha a tiltás ellenére átírja."""
    first, _, rest = page.partition("\n")
    line = first.strip()
    if not line or line.startswith(("#", "!", "[[", "|")) or len(line) > 70:
        return page
    letters = [c for c in line if c.isalpha()]
    if (letters and all(c.isupper() for c in letters)) or _ROMAN.match(line) or re.match(r"^\d+\s", line) or re.search(r"\s\d+$", line):
        return rest.lstrip("\n")
    return page


def _split_notes(page: str) -> tuple[str, str]:
    lines = page.split("\n")
    i = len(lines)
    while i > 0 and (re.match(r"\[\^[\w-]+\]:", lines[i - 1]) or not lines[i - 1].strip()):
        i -= 1
    return "\n".join(lines[:i]).strip(), "\n".join(l for l in lines[i:] if l.strip())


_CENTER = re.compile(r"^:::\s*center\s*$\n(.*?)\n^:::\s*$", re.M | re.S)


def to_pandoc(md: str) -> str:
    """:::center blokk → pandoc custom-style div (soronként külön bekezdés, középre zárt „Centered” stílus)."""
    return _CENTER.sub(lambda m: '::: {custom-style="Centered"}\n' + "\n\n".join(l for l in m.group(1).split("\n") if l.strip()) + "\n:::", md)


def to_plain(md: str) -> str:
    s = _CENTER.sub(lambda m: m.group(1), md)
    s = re.sub(r"^#{1,6}\s+", "", s, flags=re.M)
    s = re.sub(r"\[\^[\w-]+\]:\s*", "* ", s)
    s = re.sub(r"\[\^[\w-]+\]", "", s)
    return s


def title_of(md: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", md, flags=re.M)
    return (m.group(1).strip() if m else fallback)[:120]


# ---------- kimenetek ----------

def _searchable_pdf(pages: list[Path], texts: list[str], dst: Path) -> None:
    """Eredeti oldalkép + láthatatlan (render_mode=3) szöveg a lap teljes területén, sorokra tördelve.
    Pozícióhű szavakat a vision-OCR nem ad; a cél a keresés és a másolás, nem a pontos kijelölés."""
    import fitz
    from PIL import Image
    font = next((f for f in _FONTS if Path(f).exists()), None)
    doc = fitz.open()
    for img, text in zip(pages, texts):
        with Image.open(img) as im:
            w, h = im.size
            # PDF-ből 300 dpi-vel rendereltük (f###/p-*.jpg); fotónál a hosszabb oldal = A4 hosszanti (842 pt)
            dpi = 300 if img.parent.name.startswith("f") else max(w, h) / 11.69
            small = im.convert("RGB")
            if max(w, h) > 2000:
                small.thumbnail((2000, 2000))
            buf = io.BytesIO()
            small.save(buf, "JPEG", quality=72, optimize=True)   # ~4× kisebb, mint az OCR-hez használt kép
        pw, ph = w * 72 / dpi, h * 72 / dpi
        page = doc.new_page(width=pw, height=ph)
        page.insert_image(page.rect, stream=buf.getvalue())
        plain = to_plain(text).strip()
        if not plain:
            continue
        if font:
            page.insert_font(fontname="ocr", fontfile=font)
        margin = pw * 0.06
        for size in (10, 8, 6, 5, 4, 3, 2):
            per_line = max(20, int((pw - 2 * margin) / (size * 0.5)))
            lines = [l for para in plain.split("\n") for l in (textwrap.wrap(para, per_line) or [""])]
            if len(lines) * size * 1.2 <= ph - 2 * margin or size == 2:
                break
        y = margin + size
        for line in lines:
            if line:
                page.insert_text((margin, y), line, fontsize=size, fontname="ocr" if font else "helv", render_mode=3)
            y += size * 1.2
    doc.subset_fonts()          # csak a használt betűk (különben ~700 kB DejaVu minden PDF-ben)
    doc.save(str(dst), garbage=3, deflate=True)
    doc.close()


_REF_DOCX: Optional[Path] = None


def _book_reference() -> Path:
    """Pandoc referencia-docx könyvszedéshez: A5, Caladea (Cambria-metrika) 11 pt, sorkizárt, első soros
    behúzás, fekete középre zárt címsorok — a pandoc alap-docx kék, Calibri, Letter méretű."""
    global _REF_DOCX
    if _REF_DOCX and _REF_DOCX.exists():
        return _REF_DOCX
    import subprocess
    import tempfile
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Mm, Pt, RGBColor
    path = Path(tempfile.gettempdir()) / "aice_book_reference.docx"
    subprocess.run(["pandoc", "-o", str(path), "--print-default-data-file", "reference.docx"], capture_output=True, check=True)
    doc = Document(str(path))
    for sec in doc.sections:
        sec.page_width, sec.page_height = Mm(148), Mm(210)
        sec.left_margin = sec.right_margin = Mm(18)
        sec.top_margin, sec.bottom_margin = Mm(18), Mm(20)
    st = {s.name: s for s in doc.styles}   # név szerint (a python-docx st["Heading 1"] keresése itt elhasal)
    for name in ("Normal", "Body Text", "First Paragraph", "Compact", "Footnote Text", "Image Caption", "Captioned Figure"):
        if name not in st:
            continue
        s = st[name]
        s.font.name, s.font.size = "Caladea", Pt(9 if name == "Footnote Text" else 11)
        s.font.color.rgb = RGBColor(0x15, 0x15, 0x15)
        pf = s.paragraph_format
        if name in ("Body Text", "First Paragraph"):
            pf.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            pf.first_line_indent = Mm(0 if name == "First Paragraph" else 5)
            pf.space_before, pf.space_after, pf.line_spacing = Pt(0), Pt(2), 1.15
        if name in ("Image Caption", "Captioned Figure"):
            pf.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for lvl, size in ((1, 16), (2, 13), (3, 11)):
        name = f"Heading {lvl}"
        if name in st:
            s = st[name]
            s.font.name, s.font.size, s.font.bold = "Caladea", Pt(size), True
            s.font.color.rgb = RGBColor(0x15, 0x15, 0x15)
            s.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
            s.paragraph_format.space_before, s.paragraph_format.space_after = Pt(18 if lvl == 1 else 12), Pt(8)
            s.paragraph_format.page_break_before = lvl == 1
    from docx.enum.style import WD_STYLE_TYPE
    from docx.oxml.ns import qn
    if "Centered" not in st:
        c = doc.styles.add_style("Centered", WD_STYLE_TYPE.PARAGRAPH)
        c.base_style = st["Normal"]
        c.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
        c.paragraph_format.space_before, c.paragraph_format.space_after = Pt(0), Pt(4)
    # a pandoc-alap témabetűt (asciiTheme…) használ, ami felülírja a megadott betűt → kivesszük
    for el in doc.styles.element.iter(qn("w:rFonts")):
        for a in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
            el.attrib.pop(qn(a), None)
        for a in ("w:ascii", "w:hAnsi", "w:cs"):
            el.set(qn(a), "Caladea")
    doc.save(str(path))
    _REF_DOCX = path
    return path


async def build_outputs(md: str, md_img: str, figdir: Path, title: str, outputs: list[str], pages: list[Path],
                        texts: list[str], source_id: str, stem: str, style_file_id: Optional[str] = None) -> list[dict[str, Any]]:
    import pypandoc
    from app.tools import wordcraft
    files: list[tuple[str, str]] = []           # (file_id, címke)
    work = temp_path(new_file_id("d"))
    work.mkdir(parents=True)
    try:
        docx_path = None
        if {"docx", "pdf"} & set(outputs):
            # a képes változat az ábrák mappájában, hogy a pandoc a relatív képútvonalakat megtalálja
            md_path = figdir / "book.md"
            md_path.write_text(to_pandoc(md_img), "utf-8")
            docx_path = work / "book.docx"
            if style_file_id:                     # „olyan legyen, mint ez a dokumentumom” — a minta a sablon
                from app.tools import doc_style
                ref = await doc_style.reference_for(style_file_id)
            else:
                ref = await asyncio.to_thread(_book_reference)
            await asyncio.to_thread(pypandoc.convert_file, str(md_path), "docx", outputfile=str(docx_path),
                                    extra_args=["--wrap=none", f"--resource-path={figdir}", f"--reference-doc={ref}",
                                                "--metadata", f"title-meta={title}"])
        for kind in outputs:
            if kind == "md":
                fid = new_file_id("md"); output_path(fid).write_text(_CENTER.sub(lambda m: m.group(1), md), "utf-8"); files.append((fid, f"{stem}.md"))
            elif kind == "txt":
                fid = new_file_id("txt"); output_path(fid).write_text(to_plain(md), "utf-8"); files.append((fid, f"{stem}.txt"))
            elif kind == "docx":
                fid = new_file_id("docx"); shutil.copy(docx_path, output_path(fid)); files.append((fid, f"{stem}.docx"))
            elif kind == "pdf":
                fid = new_file_id("pdf")
                await wordcraft.convert_file(docx_path, output_path(fid))
                files.append((fid, f"{stem} (new).pdf"))
            elif kind == "searchable_pdf":
                fid = new_file_id("pdf")
                await asyncio.to_thread(_searchable_pdf, pages, texts, output_path(fid))
                files.append((fid, f"{stem} (searchable).pdf"))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    out = []
    for fid, label in files:
        write_meta(fid, source_file_id=source_id, operation="digitize", label=label)
        out.append({"file_id": fid, "url": public_url(fid), "label": label, "size_kb": output_path(fid).stat().st_size // 1024})
    return out


async def digitize(file_ids: list[str], outputs: list[str], language: Optional[str], max_pages: int,
                   progress: Callable[[int, int, str], None], on_pages=None, style_file_id: Optional[str] = None) -> dict[str, Any]:
    outputs = [o for o in outputs if o in OUTPUTS] or ["docx"]
    work = temp_path(new_file_id("scan"))
    work.mkdir(parents=True)
    try:
        progress(0, 0, "reading pages")
        pages = await collect_pages(file_ids, work, max_pages + 1)
        if not pages:
            raise ValueError("No pages found.")
        truncated = len(pages) > max_pages
        pages = pages[:max_pages]
        if on_pages:          # havi keret / kredit (számlázás) — mielőtt bármi drága elindul
            text_out = [o for o in outputs if o != "fillable_pdf"]
            await on_pages(len(pages) if text_out else 0, len(pages) if "fillable_pdf" in outputs else 0)
        texts: list[str] = [""] * len(pages)
        cached, done = 0, 0
        stem = Path(display_label(file_ids[0])).stem
        files: list[dict[str, Any]] = []
        form_info: dict[str, Any] = {}
        if "fillable_pdf" in outputs:
            from app.tools import forms
            fid = new_file_id("pdf")
            progress(0, len(pages), "reading the form")
            form_info = await forms.rebuild(pages, language, output_path(fid), progress)
            write_meta(fid, source_file_id=file_ids[0], operation="fillable form", label=f"{stem} (fillable).pdf")
            files.append({"file_id": fid, "url": public_url(fid), "label": f"{stem} (fillable).pdf",
                          "size_kb": output_path(fid).stat().st_size // 1024})
            outputs = [o for o in outputs if o != "fillable_pdf"]
            if not outputs:
                return {"files": files, "file_id": fid, "pages": len(pages), "pages_cached": 0, "figures": 0,
                        "truncated": truncated, "words": 0, "fields": form_info.get("fields", [])}
        gate = asyncio.Semaphore(_CONCURRENCY)
        progress(0, len(pages), "reading text")

        async def one(i: int) -> None:
            nonlocal cached, done
            async with gate:
                try:
                    texts[i], hit = await _ocr_page(pages[i], language)
                except Exception as e:
                    log.warning("OCR oldal %d: %s", i + 1, e)
                    raise RuntimeError(f"The text reader did not answer for page {i + 1}. Try again in a minute — "
                                       "pages already read are saved and won't be read (or paid for) again.") from None
            cached += hit
            done += 1
            progress(done, len(pages), "reading text")

        await asyncio.gather(*(one(i) for i in range(len(pages))))
        figdir = work / "fig"
        figdir.mkdir()
        progress(len(pages), len(pages), "building files")
        with_img, plain_pages, figures = await asyncio.to_thread(place_figures, texts, pages, figdir)
        md_img, md = assemble(with_img), assemble(plain_pages)
        if not md.strip():
            raise ValueError("No text found on these pages.")
        files += await build_outputs(md, md_img, figdir, title_of(md, stem), outputs, pages, plain_pages, file_ids[0], stem,
                                     style_file_id=style_file_id)
        return {"files": files, "file_id": files[0]["file_id"], "pages": len(pages), "pages_cached": cached, "figures": figures,
                "fields": form_info.get("fields", []),
                "truncated": truncated, "words": len(to_plain(md).split())}
    finally:
        shutil.rmtree(work, ignore_errors=True)
