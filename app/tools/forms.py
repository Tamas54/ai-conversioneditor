"""Űrlapok: fotóból/szkennből kitölthető PDF, mezők kiolvasása, kitöltés (egyenként vagy táblázatból).

    rebuild  — oldalképenként: papír megkeresése és kiegyenesítése (OpenCV) → a vision-modell (OCR_MODEL)
               JSON-ban leírja az űrlapot (feliratok, mezők, vonalak, keretek, logók; 0–1000 ezrelék) →
               PyMuPDF-fel tiszta A4 oldal, valódi mezőkkel → a mezők betűje egy beágyazott Unicode-betű
               (AcroForm /DR + /DA), különben az ő/ű elveszne (Helvetica/WinAnsi) a kitöltéskor.
    fields   — PdfCraft form_fields (név, típus, érték, opciók, oldal)
    fill     — PdfCraft form_fill (megjelenés újragenerálva) + opcionálisan doc_flatten (nyomtatható)
    fill_batch — CSV/XLSX soronként egy kitöltött példány, egy PDF-be fűzve

Mérve 10-11 (szintetikus, megdöntött, „fotózott” jelentkezési lap): 25 elem, minden felirat pontos, 11 mező
helyes típussal (5 szöveg, 3 jelölő, többsoros, dátum, aláírás), 10,7 s, ~0,1 ¢.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from app.config import settings

log = logging.getLogger("aice.forms")

FORM_PROMPT_VER = "form-v1"
_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_SIZE = {"title": 18, "heading": 13, "normal": 10.5, "small": 8.5}

_PROMPT = """This is a photo or scan of a printed form. Rebuild it as JSON so it can be re-typeset as a clean,
fillable PDF.{hint} Reply with ONE JSON object:
{"items": [
  {"type": "text", "text": "...", "box": [x0, y0, x1, y1], "size": "title|heading|normal|small", "bold": true|false, "align": "left|center|right"},
  {"type": "field", "kind": "text|multiline|checkbox|date|signature", "name": "short_snake_case", "label": "the printed label it belongs to", "box": [x0, y0, x1, y1]},
  {"type": "line", "box": [x0, y0, x1, y1]},
  {"type": "rect", "box": [x0, y0, x1, y1]},
  {"type": "picture", "box": [x0, y0, x1, y1], "caption": "logo / emblem / photo"}
]}
Coordinates: integers 0–1000, thousandths of the form's width and height, origin top left.
Every printed word goes into a text item, exactly as printed (keep the language and spelling). A blank line,
an empty box, a table cell to fill in or a small square to tick is a field (its box = the blank area, not the
label). Table borders and separator lines are line or rect items. Logos and printed pictures are picture items.
Do not include library stamps, handwriting or anything already written into the blanks. Keep the reading order.
Output only the JSON."""


# ---------- papír kiegyenesítése ----------

def straighten(src: Path, dst: Path) -> tuple[int, int]:
    """A legnagyobb, a kép ≥35%-át kitevő négyszög (a papír) perspektívája kiegyenesítve; ha nincs ilyen
    (szken, képernyőkép), a kép marad. Visszaad: (szélesség, magasság)."""
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(src.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    scale = 1000 / max(h, w)
    small = cv2.resize(img, (int(w * scale), int(h * scale)))
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    edges = cv2.dilate(cv2.Canny(gray, 40, 120), np.ones((3, 3), np.uint8))
    quad = None
    for c in sorted(cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0], key=cv2.contourArea, reverse=True)[:5]:
        approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        if len(approx) == 4 and cv2.contourArea(approx) > 0.35 * small.shape[0] * small.shape[1]:
            quad = approx.reshape(4, 2).astype("float32") / scale
            break
    if quad is None:
        cv2.imwrite(str(dst), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        return w, h
    s, d = quad.sum(1), np.diff(quad, axis=1).ravel()
    tl, br, tr, bl = quad[np.argmin(s)], quad[np.argmax(s)], quad[np.argmin(d)], quad[np.argmax(d)]
    W = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    H = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    M = cv2.getPerspectiveTransform(np.array([tl, tr, br, bl]), np.array([[0, 0], [W, 0], [W, H], [0, H]], dtype="float32"))
    cv2.imwrite(str(dst), cv2.warpPerspective(img, M, (W, H)), [cv2.IMWRITE_JPEG_QUALITY, 92])
    return W, H


# ---------- szerkezet kiolvasása ----------

def _parse_json(text: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


async def read_layout(page: Path, hint: Optional[str] = None) -> dict[str, Any]:
    from app import db
    from app.llm import vision
    sha = hashlib.sha256(page.read_bytes()).hexdigest()
    ver = FORM_PROMPT_VER + (f":{hint}" if hint else "")
    try:
        cached = await db.ocr_cache_get(sha, ver, settings.OCR_MODEL)
    except Exception:
        cached = None
    if cached:
        return json.loads(cached)
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": _PROMPT.replace("{hint}", f" The form is in {hint}." if hint else "")},   # .format nem: JSON-kapcsosok
        {"type": "image_url", "image_url": {"url": vision.image_to_data_url(page, long_edge=2048)}},
    ]}]
    last: Exception = RuntimeError("no answer")
    for model in (settings.OCR_MODEL, settings.LLM_MODEL_VISION):
        try:
            data = _parse_json(await vision._call(msgs, max_tokens=8000, model=model))
            if isinstance(data.get("items"), list):
                break
        except Exception as e:  # rossz JSON / nem válaszolt → tartalék modell
            last = e
            log.warning("űrlap-szerkezet %s: %s", model, e)
    else:
        raise RuntimeError(f"Could not read the form layout: {last}")
    try:
        await db.ocr_cache_put(sha, ver, settings.OCR_MODEL, json.dumps(data, ensure_ascii=False))
    except Exception:
        log.warning("ocr_cache írás nem sikerült", exc_info=True)
    return data


# ---------- tiszta PDF építése ----------

def build_pdf(layouts: list[dict[str, Any]], pages: list[Path], dst: Path) -> list[str]:
    """Oldalanként egy A4 (fekvő, ha a lap fekvő), feliratok + vonalak + logók + valódi mezők."""
    import pymupdf as fitz
    from PIL import Image
    from app.tools.digitize import _crop_figure
    doc = fitz.open()
    names: list[str] = []
    for n, (layout, img) in enumerate(zip(layouts, pages), 1):
        with Image.open(img) as im:
            iw, ih = im.size
        W, H = (842.0, 595.0) if iw > ih * 1.05 else (595.0, 842.0)
        page = doc.new_page(width=W, height=H)
        page.insert_font(fontname="UniF", fontfile=_FONT)
        page.insert_font(fontname="UniB", fontfile=_FONT_BOLD)
        R = lambda b: fitz.Rect(*(float(b[i]) * (W if i % 2 == 0 else H) / 1000 for i in range(4))).normalize()
        font, bold_font = fitz.Font(fontfile=_FONT), fitz.Font(fontfile=_FONT_BOLD)
        for k, it in enumerate(layout.get("items") or []):
            try:
                box = it.get("box") or [0, 0, 0, 0]
                if max(float(v) for v in box) <= 1.0:
                    box = [float(v) * 1000 for v in box]
                r = R(box)
            except (TypeError, ValueError, IndexError):
                continue
            t = it.get("type")
            if t == "text" and (it.get("text") or "").strip():
                text = it["text"].strip()
                size = _SIZE.get(it.get("size"), _SIZE["normal"])
                bold = bool(it.get("bold")) or size >= _SIZE["heading"]
                f = bold_font if bold else font
                room = (W - 28 - r.x0) if it.get("align", "left") == "left" else max(r.width, W - 56)
                while size > 6 and f.text_length(text, fontsize=size) > max(room, r.width):
                    size -= 0.5
                tl = f.text_length(text, fontsize=size)
                align = it.get("align", "left")
                x = r.x0 if align == "left" else (r.x1 - tl if align == "right" else (r.x0 + r.x1 - tl) / 2)
                y = r.y0 + (r.height + size * 0.7) / 2 if r.height > 0 else r.y0 + size
                page.insert_text(fitz.Point(max(14, x), y), text, fontsize=size, fontname="UniB" if bold else "UniF")
            elif t == "line":
                page.draw_line(fitz.Point(r.x0, r.y0), fitz.Point(r.x1, r.y1 if r.height > 2 else r.y0), width=0.7)
            elif t == "rect":
                page.draw_rect(r, width=0.8)
            elif t == "picture":
                crop = dst.with_name(f"{dst.stem}-p{n}-{k}.jpg")
                if _crop_figure(img, tuple(int(v) for v in box), crop):
                    page.insert_image(r, filename=str(crop), keep_proportion=True)
                    crop.unlink(missing_ok=True)
            elif t == "field":
                kind = (it.get("kind") or "text").lower()
                base = re.sub(r"[^a-z0-9_]+", "_", (it.get("name") or it.get("label") or "field").lower()).strip("_") or "field"
                name, i = base, 2
                while name in names:
                    name, i = f"{base}_{i}", i + 1
                names.append(name)
                w = fitz.Widget()
                w.field_name, w.field_label = name, (it.get("label") or "")[:120]
                w.text_font, w.text_fontsize = "Helv", 0
                if kind == "checkbox":
                    s = max(8, min(r.width, r.height, 14))
                    r = fitz.Rect(r.x0, r.y0, r.x0 + s, r.y0 + s)
                    page.draw_rect(r, width=0.8)
                    w.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
                elif kind == "multiline":
                    page.draw_rect(r, width=0.8)
                    w.field_type, w.field_flags = fitz.PDF_WIDGET_TYPE_TEXT, fitz.PDF_TX_FIELD_IS_MULTILINE
                else:
                    page.draw_line(fitz.Point(r.x0, r.y1), fitz.Point(r.x1, r.y1), width=0.7)
                    r = fitz.Rect(r.x0, r.y1 - 15, r.x1, r.y1 - 1)
                    w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
                if r.width < 4 or r.height < 4:
                    continue
                w.rect = r
                page.add_widget(w)
    doc.save(str(dst), garbage=3, deflate=True)
    doc.close()
    unicode_fields(dst)
    return names


def _is_uni(basefont: str) -> bool:
    """A beágyazott DejaVu Sans (normál) — a PyMuPDF „DejaVu Sans Book”, más eszköz „DejaVuSans” néven írja."""
    b = basefont.replace(" ", "").replace("/", "").split("+")[-1].lower()
    return b.startswith("dejavusans") and "bold" not in b and "oblique" not in b


def unicode_fields(path: Path) -> None:
    """A mezők betűje egy beágyazott, teljes Unicode-betű (AcroForm /DR + /DA): a PdfCraft form_fill ezzel
    generálja a megjelenést, így az ő/ű is látszik (Helvetica/WinAnsi-val „Brös rsula” lenne)."""
    import pikepdf
    import pymupdf as fitz
    d = fitz.open(str(path))
    if not any(_is_uni(f[3]) for f in d[0].get_fonts()):
        d[0].insert_font(fontname="UniF", fontfile=_FONT)    # teljes betű (nem subset: bármit be lehessen írni)
        tmp = Path(str(path) + ".tmp")
        d.save(str(tmp))
        d.close()
        tmp.replace(path)
    else:
        d.close()
    pdf = pikepdf.open(str(path), allow_overwriting_input=True)
    font = None
    for k, v in pdf.pages[0].Resources.Font.items():
        if _is_uni(str(v.get("/BaseFont", ""))):
            font = v
            break
    af = pdf.Root.get("/AcroForm")
    if font is None or af is None:
        pdf.close()
        return
    # a meglévő betű-erőforrások maradnak (jelölőnégyzet: ZapfDingbats), csak hozzáadjuk a Unicode-betűt
    dr = af.get("/DR") or pikepdf.Dictionary()
    fonts = dr.get("/Font") or pikepdf.Dictionary()
    fonts.UniF = font
    if "/ZaDb" not in fonts:
        fonts.ZaDb = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                                          BaseFont=pikepdf.Name.ZapfDingbats))
    dr.Font = fonts
    af.DR = dr

    def fix(field, ft=None) -> None:
        ft = field.get("/FT", ft)
        if ft in (pikepdf.Name.Tx, pikepdf.Name.Ch):   # csak szöveg/választó mező; a jelölő ZaDb-je marad
            # „/UniF <méret> Tf 0 g” — a PdfCraft csak a betű-elöl alakot olvassa („0 0 0 rg /UniF 0 Tf”-et nem)
            m = re.search(r"/\S+\s+([\d.]+)\s+Tf", str(field.get("/DA", "")))
            field.DA = pikepdf.String(f"/UniF {m.group(1) if m else 0} Tf 0 g")
        for kid in field.get("/Kids", []):
            fix(kid, ft)
    for f in af.get("/Fields", []):
        fix(f)
    af.DA = pikepdf.String("/UniF 0 Tf 0 g")
    af.NeedAppearances = True
    pdf.save(str(path))
    pdf.close()


# ---------- mezők, kitöltés ----------

async def fields(file_id: str) -> list[dict[str, Any]]:
    from app.tools import crafts
    out = await crafts.pdf_script([file_id], [{"tool": "form_fields", "args": {}}], Path("/dev/null"), save=False)
    found = _last_json(out)
    items = found.get("fields") if isinstance(found, dict) else found
    return [{k: f.get(k) for k in ("name", "type", "value", "options", "page", "required", "read_only", "tooltip")}
            for f in (items or []) if isinstance(f, dict) and f.get("name")]


def _last_json(out: str) -> Any:
    """A pdfcraft-cli `run --script` lépésenként egy JSON-objektumot ír; az utolsó kell."""
    dec, i, last = json.JSONDecoder(), 0, None
    while True:
        j = out.find("{", i)
        k = out.find("[", i)
        starts = [x for x in (j, k) if x >= 0]
        if not starts:
            return last
        try:
            last, i = dec.raw_decode(out, min(starts))
        except ValueError:
            i = min(starts) + 1


def _coerce(values: dict[str, Any], known: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Felhasználói értékek → PdfCraft: jelölőnégyzet bool, a többi szöveg; ismeretlen név kimarad.
    A kulcs lehet mezőnév vagy (kisbetűs, ékezet-érzéketlen) címke-közeli alak."""
    import unicodedata
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower())
    by = {norm(f["name"]): f for f in known}
    out: dict[str, Any] = {}
    skipped: list[str] = []
    for k, v in values.items():
        f = by.get(norm(k))
        if not f:
            skipped.append(str(k))
            continue
        if v is None or v == "":
            continue
        if f.get("type") == "checkbox":
            out[f["name"]] = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "igen", "x", "✓", "on")
        else:
            out[f["name"]] = str(v)
    return out, skipped


async def fill(file_id: str, values: dict[str, Any], flatten: bool, dst: Path) -> dict[str, Any]:
    from app.tools import crafts
    known = await fields(file_id)
    if not known:
        raise ValueError("This PDF has no form fields. Rebuild it as a fillable form first (Scan to text → Fillable form).")
    vals, skipped = _coerce(values, known)
    if not vals:
        raise ValueError("None of the values match a field. Fields: " + ", ".join(f["name"] for f in known[:30]))
    src = crafts.existing(file_id)
    work = dst.with_suffix(".src.pdf")
    work.write_bytes(src.read_bytes())
    try:
        await asyncio.to_thread(unicode_fields, work)
        # lapítás NEM PdfCraft doc_flatten-nel: az újrarajzolja a mezőt a saját Helveticájával (ő/ű → ?);
        # a PyMuPDF bake a form_fill által már helyesen (UniF-fel) megrajzolt megjelenést égeti be
        steps = [{"tool": "form_fill", "args": {"values": vals}}]
        script = [{"tool": "doc_open", "args": {"path": str(work)}}] + [{"tool": s["tool"], "args": {**s["args"], "doc": 1}} for s in steps] + \
                 [{"tool": "doc_save", "args": {"doc": 1, "path": str(dst), "full": True}}]
        await _run_script(script)
        if flatten:
            await asyncio.to_thread(_bake, dst)
    finally:
        work.unlink(missing_ok=True)
    return {"filled": sorted(vals), "skipped": skipped}


def _bake(path: Path) -> None:
    """Nyomtatható lapítás: az értékeket MI szedjük ki a mezők helyére (DejaVu: ő/ű is), a jelölőbe pipa kerül,
    a mezők törlődnek. Sem a PdfCraft doc_flatten, sem a PyMuPDF bake / QPDF flatten nem jó: a PdfCraft
    form_fill a mező megjelenését a saját Helveticájával rajzolja (ő/ű → ?), és azok azt égetik be."""
    import pymupdf as fitz
    font = fitz.Font(fontfile=_FONT)
    tmp = path.with_suffix(".baked.pdf")
    d = fitz.open(str(path))
    for page in d:
        widgets = list(page.widgets())
        if not widgets:
            continue
        page.insert_font(fontname="UniP", fontfile=_FONT)
        for w in widgets:
            r, v = w.rect, w.field_value
            if w.field_type in (fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.PDF_WIDGET_TYPE_RADIOBUTTON):
                if v not in (None, False, "", "Off", "off"):
                    s = min(r.width, r.height)
                    page.insert_text(fitz.Point(r.x0 + s * 0.12, r.y1 - s * 0.15), "✓", fontsize=s * 0.95, fontname="UniP")
            elif v:
                text = str(v)
                if w.field_flags & fitz.PDF_TX_FIELD_IS_MULTILINE:
                    page.insert_textbox(r + (3, 3, -3, -3), text, fontsize=10, fontname="UniP")
                else:
                    size = w.text_fontsize or min(11.0, r.height * 0.72)
                    while size > 5 and font.text_length(text, fontsize=size) > r.width - 4:
                        size -= 0.5
                    page.insert_text(fitz.Point(r.x0 + 2, r.y1 - max(2.0, (r.height - size) / 2) - size * 0.15), text,
                                     fontsize=size, fontname="UniP")
            page.delete_widget(w)
    d.save(str(tmp), garbage=3, deflate=True)
    d.close()
    tmp.replace(path)


async def _run_script(script: list[dict[str, Any]]) -> str:
    from app import tenancy
    from app.storage import temp_path
    from app.storage.files import new_file_id
    from app.tools import crafts
    tmp = temp_path(new_file_id("json"))
    tmp.write_text(json.dumps(script, ensure_ascii=False))
    try:
        return await crafts.run_cli(crafts.office_cli("pdf"), "pdfcraft-cli", "run", "--script", str(tmp), "--root", str(tenancy.user_root()))
    finally:
        tmp.unlink(missing_ok=True)


def read_rows(path: Path) -> list[dict[str, Any]]:
    """CSV/TSV/XLSX → sorok (első sor = fejléc)."""
    ext = path.suffix.lower()
    if ext in (".csv", ".tsv"):
        import csv
        raw = path.read_bytes().decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t") if ext == ".csv" else csv.excel_tab
        return [dict(r) for r in csv.DictReader(raw.splitlines(), dialect=dialect)]
    from openpyxl import load_workbook
    ws = load_workbook(str(path), read_only=True, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    head = [str(h or "").strip() for h in rows[0]]
    return [{h: v for h, v in zip(head, r) if h} for r in rows[1:] if any(c not in (None, "") for c in r)]


async def fill_batch(file_id: str, rows: list[dict[str, Any]], flatten: bool, dst: Path, limit: int = 500) -> dict[str, Any]:
    """Soronként egy kitöltött példány, egy PDF-be fűzve (PdfCraft doc_combine)."""
    from app.storage import temp_path
    from app.storage.files import new_file_id
    rows = rows[:limit]
    if not rows:
        raise ValueError("The table has no rows.")
    parts: list[Path] = []
    try:
        for i, row in enumerate(rows):
            part = temp_path(new_file_id("pdf"))
            await fill(file_id, row, flatten, part)
            parts.append(part)
        steps = [{"tool": "doc_combine", "args": {"paths": [str(p) for p in parts], "out": str(dst)}}]
        await _run_script(steps)
    finally:
        for p in parts:
            p.unlink(missing_ok=True)
    return {"copies": len(parts)}


async def rebuild(pages: list[Path], hint: Optional[str], dst: Path, progress=None) -> dict[str, Any]:
    """Oldalképek → kitölthető PDF. A lapokat előbb kiegyenesíti (papír-négyszög), aztán kiolvassa és szedi."""
    flat: list[Path] = []
    for p in pages:
        q = p.with_name(p.stem + "-flat.jpg")
        await asyncio.to_thread(straighten, p, q)
        flat.append(q)
    gate = asyncio.Semaphore(6)
    done = 0

    async def one(p: Path) -> dict[str, Any]:
        nonlocal done
        async with gate:
            lay = await read_layout(p, hint)
        done += 1
        if progress:
            progress(done, len(flat), "reading the form")
        return lay
    layouts = await asyncio.gather(*(one(p) for p in flat))
    names = await asyncio.to_thread(build_pdf, list(layouts), flat, dst)
    return {"fields": names, "pages": len(flat)}
