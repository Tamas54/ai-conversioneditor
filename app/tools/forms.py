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
import collections
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from app.config import settings

log = logging.getLogger("aice.forms")

FORM_PROMPT_VER = "form-v2"      # v2 (10-18): a kitöltött válasz mező lesz, a tartalma előtöltve
_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_SIZE = {"title": 18, "heading": 13, "normal": 10.5, "small": 8.5}

_PROMPT = """This is a photo or scan of a printed form. Rebuild it as JSON so it can be re-typeset as a clean,
fillable PDF.{hint} Reply with ONE JSON object:
{"items": [
  {"type": "text", "text": "...", "box": [x0, y0, x1, y1], "size": "title|heading|normal|small", "bold": true|false, "align": "left|center|right"},
  {"type": "field", "kind": "text|multiline|checkbox|date|signature", "name": "short_snake_case", "label": "the printed label it belongs to", "box": [x0, y0, x1, y1], "value": "what is already written in it, if anything"},
  {"type": "line", "box": [x0, y0, x1, y1]},
  {"type": "rect", "box": [x0, y0, x1, y1]},
  {"type": "picture", "box": [x0, y0, x1, y1], "caption": "logo / emblem / photo"}
]}
Coordinates: integers 0–1000, thousandths of the form's width and height, origin top left.
Every printed word goes into a text item, exactly as printed (keep the language and spelling). A blank line,
an empty box, a table cell to fill in or a small square to tick is a field (its box = the blank area, not the
label). Table borders and separator lines are line or rect items. Logos and printed pictures are picture items.
If the form is already filled in, the answers are NOT printed text: every filled-in answer — typed (often in another
style, e.g. italics), handwritten, or a tick / X in a box or column — is the FIELD of that blank, with what is
written there as its "value" (exactly as written; a tick or X in a checkbox is "value": true). Never put a filled-in
answer into a text item. A handwritten signature is never transcribed: its field stays without a value.
Do not include library stamps. Keep the reading order.
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
    for model in (settings.OCR_MODEL, settings.LLM_MODEL_VISION_FALLBACK):
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

def build_pdf(layouts: list[dict[str, Any]], pages: list[Path], dst: Path,
              prefill: Optional[dict[str, Any]] = None) -> list[str]:
    """Oldalanként egy A4 (fekvő, ha a lap fekvő), feliratok + vonalak + logók + valódi mezők."""
    import pymupdf as fitz
    from PIL import Image
    from app.tools.digitize import _crop_figure
    from app.tools import pdffonts
    doc = fitz.open()
    names: list[str] = []
    # a lapszöveg betűje: csak a lapokon álló jelek (a mezőké külön, a gépelési részhalmaz — unicode_fields)
    chars = "".join(str(it.get("text") or "") + str(it.get("label") or "") for L in layouts for it in (L.get("items") or []))
    reg, bold = pdffonts.subset_for(chars, _FONT), pdffonts.subset_for(chars, _FONT_BOLD)
    for n, (layout, img) in enumerate(zip(layouts, pages), 1):
        with Image.open(img) as im:
            iw, ih = im.size
        W, H = (842.0, 595.0) if iw > ih * 1.05 else (595.0, 842.0)
        page = doc.new_page(width=W, height=H)
        page.insert_font(fontname="UniF", fontbuffer=reg)
        page.insert_font(fontname="UniB", fontbuffer=bold)
        R = lambda b: fitz.Rect(*(float(b[i]) * (W if i % 2 == 0 else H) / 1000 for i in range(4))).normalize()
        font, bold_font = fitz.Font(fontbuffer=reg), fitz.Font(fontbuffer=bold)
        pending: list = []                          # a mezők: a lap minden szövege után kerülnek fel
        blanks = []                                 # a mezők dobozai: az üres hely kezdete a nyomtatványon (a felirat ne nyúljon bele)
        for it in layout.get("items") or []:
            if it.get("type") == "field":
                try:
                    b = [float(v) for v in it.get("box") or []]
                    blanks.append(R([v * 1000 for v in b] if b and max(b) <= 1.0 else b))
                except (TypeError, ValueError, IndexError):
                    pass
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
                if it.get("align", "left") == "left":     # a sorban utána kezdődő mező elé férjen (min. 7 pt)
                    nxt = [b.x0 for b in blanks if b.x0 > r.x0 + 4 and min(b.y1, r.y1) - max(b.y0, r.y0) > 0.4 * min(b.height, max(r.height, 1))]
                    if nxt:
                        while size > 7 and r.x0 + f.text_length(text, fontsize=size) > min(nxt) - 3:
                            size -= 0.25
                tl = f.text_length(text, fontsize=size)
                align = it.get("align", "left")
                if r.height >= 2.2 * _SIZE["normal"] and f.text_length(text, fontsize=_SIZE.get(it.get("size"), _SIZE["normal"])) > r.width:
                    # több soros bekezdés (a dobozba több sor fér): tördelve, nem egy sorba zsugorítva — különben
                    # 6 pontra kicsinyül és még így is lelóg a lapról (UI-próba 10-18, vagyonnyilatkozat (F) pont)
                    size = _SIZE.get(it.get("size"), _SIZE["normal"])
                    box = fitz.Rect(max(14, r.x0), r.y0, min(W - 14, max(r.x1, r.x0 + 40)), r.y1 + size * 1.5)
                    al = {"left": 0, "center": 1, "right": 2}.get(align, 0)
                    while size > 6 and page.insert_textbox(box, text, fontsize=size, fontname="UniB" if bold else "UniF",
                                                           align=al) < 0:
                        size -= 0.5
                    if size <= 6:
                        page.insert_textbox(box, text, fontsize=6, fontname="UniB" if bold else "UniF", align=al)
                    continue
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
                pending.append((it, r))           # a mezők a lap MINDEN szövege után (lásd lent)
        # a mezők a ténylegesen kirajzolt szavakhoz igazítva: ami a mező bal végébe lóg (a felirat vége, több soros
        # felirat is), azután kezdődik; ami a jobb végébe (mértékegység: „… Ft”), az előtt ér véget
        # (UI-próba 10-18: „tartozás:”, „kölcsön stb.):”, „Ft” a mezők alatt)
        words = [fitz.Rect(x[:4]) for x in page.get_text("words")]
        for it, r in pending:
            kind = (it.get("kind") or "text").lower()
            for wr in words:
                if min(wr.y1, r.y1) - max(wr.y0, r.y0) < 0.5 * min(wr.height, max(r.height, 1)) or not (wr.x0 < r.x1 and r.x0 < wr.x1):
                    continue
                if (wr.x0 + wr.x1) / 2 <= (r.x0 + r.x1) / 2:
                    nr = fitz.Rect(wr.x1 + 3, r.y0, r.x1, r.y1)
                else:
                    nr = fitz.Rect(r.x0, r.y0, wr.x0 - 3, r.y1)
                if nr.width >= 20:
                    r = nr
            base = re.sub(r"[^a-z0-9_]+", "_", (it.get("name") or it.get("label") or "field").lower()).strip("_") or "field"
            name, i = base, 2
            while name in names:
                name, i = f"{base}_{i}", i + 1
            names.append(name)
            w = fitz.Widget()
            w.field_name, w.field_label = name, (it.get("label") or "")[:120]
            w.text_font, w.text_fontsize = "Helv", 0
            if kind == "checkbox":
                s_ = max(8, min(r.width, r.height, 14))
                r = fitz.Rect(r.x0, r.y0, r.x0 + s_, r.y0 + s_)
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
            v = it.get("value")
            # a kitöltött nyomtatvány válasza — az aláírás SOSEM (UI-próba 10-18: a firkából „Pintér Bence” lett)
            if prefill is not None and v not in (None, "", False) and kind != "signature" \
                    and not re.search(r"al[aá][ií]r|signat", f"{name} {it.get('label') or ''}", re.I):
                prefill[name] = v
    doc.save(str(dst), garbage=3, deflate=True)
    doc.close()
    unicode_fields(dst)
    return names


def _is_uni(basefont: str) -> bool:
    """A beágyazott DejaVu Sans (normál) — a PyMuPDF „DejaVu Sans Book”, más eszköz „DejaVuSans” néven írja."""
    b = basefont.replace(" ", "").replace("/", "").split("+")[-1].lower()
    return b.startswith("dejavusans") and "bold" not in b and "oblique" not in b


def unicode_fields(path: Path) -> None:
    """A mezők betűje egy beágyazott Unicode-betű (AcroForm /DR + /DA): a PdfCraft form_fill ezzel generálja a
    megjelenést, így az ő/ű is látszik (Helvetica/WinAnsi-val „Brös rsula” lenne). Külön betű-objektum, nem a
    lapszövegé: a „gépelési” részhalmaz (pdffonts.typing_font — latin, görög, cirill, írásjelek), így a később
    begépelt ékezet is megvan, a fájl mégsem hízik a teljes DejaVuval (423 kB → < 100 kB)."""
    import pikepdf
    import pymupdf as fitz
    from app.tools import pdffonts
    with pikepdf.open(str(path)) as pdf:
        af = pdf.Root.get("/AcroForm")
        if af is None:
            return
        cur = (af.get("/DR") or {}).get("/Font", {}).get("/UniF")
        try:                                  # már a részhalmaz-betű a mezőké (pl. a kitöltő újra hívja) → az marad
            ff = cur.DescendantFonts[0].FontDescriptor.FontFile2
            reuse = int(ff.get("/Length1", 0) or 10 ** 9) < 200_000
        except (AttributeError, IndexError, KeyError, TypeError):
            reuse = False
    xref = None
    if not reuse:
        d = fitz.open(str(path))
        xref = d[0].insert_font(fontname="UniT", fontbuffer=pdffonts.typing_font(_FONT))
        tmp = Path(str(path) + ".tmp")
        d.save(str(tmp))
        d.close()
        tmp.replace(path)
    pdf = pikepdf.open(str(path), allow_overwriting_input=True)
    af = pdf.Root.get("/AcroForm")
    if xref is not None:
        font = pdf.get_object((xref, 0))
        res = pdf.pages[0].Resources.get("/Font")
        if res is not None and "/UniT" in res:
            del res.UniT                      # csak a mezőké: lapítás után (mezők nélkül) a fájlból is kikerül
    else:
        font = af.DR.Font.UniF
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
    exact = {f["name"]: f for f in known}
    by: dict[str, Any] = {}
    for f in known:                       # a normalizált alak csak tartalék, és csak ha EGYÉRTELMŰ: „telepules_nev_1_2”
        by.setdefault(norm(f["name"]), []).append(f)   # és „telepules_nev_12” ugyanaz lenne → rossz mezőbe ment (10-18)
    out: dict[str, Any] = {}
    skipped: list[str] = []
    for k, v in values.items():
        cands = by.get(norm(k)) or []
        f = exact.get(k) or (cands[0] if len(cands) == 1 else None)
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
    await _fill_file(crafts.existing(file_id), vals, flatten, dst)
    return {"filled": sorted(vals), "skipped": skipped}


async def _fill_file(src: Path, vals: dict[str, Any], flatten: bool, dst: Path) -> None:
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


def _bake(path: Path) -> None:
    """Nyomtatható lapítás: az értékeket MI szedjük ki a mezők helyére (DejaVu: ő/ű is), a jelölőbe pipa kerül,
    a mezők törlődnek. Sem a PdfCraft doc_flatten, sem a PyMuPDF bake / QPDF flatten nem jó: a PdfCraft
    form_fill a mező megjelenését a saját Helveticájával rajzolja (ő/ű → ?), és azok azt égetik be."""
    import pymupdf as fitz
    from app.tools import pdffonts
    tmp = path.with_suffix(".baked.pdf")
    d = fitz.open(str(path))
    buf = pdffonts.subset_for("".join(str(w.field_value or "") for pg in d for w in pg.widgets()), _FONT)  # csak a beírt jelek
    font = fitz.Font(fontbuffer=buf)
    for page in d:
        widgets = list(page.widgets())
        if not widgets:
            continue
        page.insert_font(fontname="UniP", fontbuffer=buf)
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
    if not any(True for pg in d for _ in pg.widgets()):
        d.xref_set_key(d.pdf_catalog(), "AcroForm", "null")    # mező nem maradt: a mezőbetű (/DR) sem kell
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
        from app.tools import pdf_prep           # könyvjelző példányonként: „3. Kovács Anna”, nem az ideiglenes fájlnév
        pdf_prep.retitle_outline(dst, {p.stem: f"{i + 1}. " + str(next((v for v in row.values() if v not in (None, "")), ""))[:60]
                                       for i, (p, row) in enumerate(zip(parts, rows))})
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
    prefill: dict[str, Any] = {}
    names = await asyncio.to_thread(build_pdf, list(layouts), flat, dst, prefill)
    if prefill:          # kitöltött nyomtatvány: a válaszok a mezőkben, átírhatóan (ő/ű-biztos kitöltéssel)
        vals = {k: (v if isinstance(v, bool) else str(v)) for k, v in prefill.items()}
        filled = dst.with_suffix(".filled.pdf")
        await _fill_file(dst, vals, False, filled)
        filled.replace(dst)
    return {"fields": names, "pages": len(flat), "prefilled": len(prefill)}


_FILL_PROMPT = """You fill in a PDF form for the user. Its fields, one per line: name | type | page | label | current value
{fields}

The user asks: {request}

Reply with ONE JSON object {{"values": {{"field name": value, ...}}, "summary": "one short sentence in the language of
the request"}}. For a small change give only the fields that change. But when the user asks to fill in the WHOLE form
(their own data, new data, made-up / sample / test data), give a value for EVERY field: the new answer, or — where
nothing applies — the form's own "nothing" mark (e.g. "-----", as the current values use it) or "". The current
answers then belong to someone else and must not stay. One item is one block: one flat fills the first property block
only, one car the first vehicle block; the other blocks get the "nothing" mark. Text in the form's own language and
format (dates, units, numbers like the current values). A checkbox is true or false. Never fill a signature field.
Use only data the user gave — but when the user asks for made-up, sample or test data, invent plausible values that fit
each field (consistent with each other). Output only the JSON."""


async def has_fields(file_id: str) -> bool:
    try:
        return bool(await fields(file_id))
    except Exception:
        return False


async def fill_from_request(file_id: str, request: str, dst: Path) -> dict[str, Any]:
    """„Ask for a change” egy KITÖLTHETŐ PDF-en: a kérés → mezőértékek (modell), a kitöltés a mezőkbe (ő/ű-biztos).
    UI-próba 10-18: ugyanez a kérés eddig a szerkesztő-ügynökhöz ment, az a PDF-et Wordön át újraszedte —
    a 266 mező eltűnt, 25 oldalból 42 lett."""
    from app.llm.siliconflow import _call_model, _strip_json_fences
    from app.config import settings
    from app.tools import crafts
    known = await fields(file_id)
    if not known:
        raise ValueError("This PDF has no form fields.")
    row = lambda f: (f"{f['name']} | {f.get('type')} | {f.get('page')} | {(f.get('tooltip') or '')[:90]} | "
                     f"{str(f.get('value') if f.get('value') is not None else '')[:60]}")
    # csomagokban (a válasz 4096 tokenes: 266 mező egyben csonka JSON lenne); a későbbi csomag látja a korábban
    # eldöntött értékeket, hogy a kitalált adatok összeilljenek (ugyanaz a név, ugyanaz az autó)
    batches = [known[i:i + 90] for i in range(0, min(len(known), 1500), 90)]
    planned: dict[str, Any] = {}
    summary = ""

    async def ask(prompt: str) -> dict[str, Any]:
        last: Exception = RuntimeError("no model")
        for model in (settings.LLM_MODEL_PRIMARY, settings.LLM_MODEL_FALLBACK):
            try:
                got = json.loads(_strip_json_fences(await _call_model(model, [{"role": "user", "content": prompt}],
                                                                       thinking=False)))
                if isinstance(got, dict):
                    return got
            except Exception as e:
                last = e
        raise last
    for n, batch in enumerate(batches):
        prompt = _FILL_PROMPT.format(fields="\n".join(row(f) for f in batch), request=request)
        if planned:
            prompt += ("\nOther fields of the same form were already filled like this — stay consistent with them:\n"
                       + json.dumps(planned, ensure_ascii=False)[:3000])
        got = await ask(prompt)
        planned.update({k: v for k, v in (got.get("values") or {}).items() if isinstance(k, str)})
        summary = summary or str(got.get("summary") or "")
    plan = {"values": planned, "summary": summary}
    # egész űrlap újraírása (a mezők ≥40%-a kapott értéket — egy apró javítás 1–3 mező): a kihagyott vagy szó szerint
    # visszaadott régi válasz se maradjon a régi gazdáé — az űrlap saját „nincs” jelét kapja (UI-próba 10-18:
    # „Csenger”, „10625 m2”, „legelő, erdő” bent maradt)
    log.info("kitöltés kérésből: %d/%d mezőre jött érték", len(planned), len(known))
    if len(planned) >= 0.4 * len(known):
        marks = collections.Counter(v for v in planned.values() if isinstance(v, str) and re.fullmatch(r"[-–—_.]{3,}", v.strip()))
        nothing = marks.most_common(1)[0][0] if marks else ""
        req = re.sub(r"\s+", " ", request.lower())
        for f in known:
            old_v = str(f.get("value") or "").strip()
            if f.get("type") == "checkbox" or not old_v or re.fullmatch(r"[-–—_.]{3,}", old_v):
                continue
            if f["name"] not in planned:
                planned[f["name"]] = nothing
            elif str(planned[f["name"]]).strip() == old_v and old_v.lower() not in req:
                planned[f["name"]] = nothing        # a régi válasz szó szerint visszajött, és nem a felhasználótól való
    sig = {f["name"] for f in known if f.get("type") == "signature"
           or re.search(r"al[aá][ií]r|signat", f"{f['name']} {f.get('tooltip') or ''}", re.I)}
    asked = {k: v for k, v in (plan.get("values") or {}).items() if k not in sig}
    vals, _ = _coerce(asked, known)
    text_fields = {f["name"] for f in known if f.get("type") != "checkbox"}
    vals.update({k: "" for k, v in asked.items() if v == "" and k in text_fields})     # „” = a régi válasz törlése
    if not vals:
        raise ValueError("Nothing to fill in from that request — say which answers to write, or ask for test data.")
    await _fill_file(crafts.existing(file_id), vals, False, dst)
    return {"filled": sorted(vals), "summary": str(plan.get("summary") or f"{len(vals)} field(s) filled in.")[:300]}
