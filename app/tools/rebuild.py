"""„Építsd újra ezt a plakátot”: kép vagy PDF-plakát → SZERKESZTHETŐ grafika (VectorCraft) — minden szöveg átírható
szövegdoboz (szöveg, méret, szín, igazítás, közelítő betűtípus), a tiszta színfoltok vektoros alakzatok, a fotók /
logók külön mozgatható képek, alattuk a szöveg nélküli háttér.

Két út:
  • KÉP (fotó / képernyőkép / szken): a papír kiegyenesítése (forms.straighten) → a látómodell (DeepSeek-V4.1-Flash,
    tartalék GLM) JSON-ban leírja az elemeket (0–1000 ezrelék) → a szöveg színét és helyét a PIXELEKBŐL mérjük
    (a doboz körüli gyűrű = háttér, ami attól elüt = festék) → a szöveg eltüntetése a háttérről INGYEN: egyszínű
    háttéren kitöltés a háttérszínnel, máshol OpenCV inpaint; csak kérésre (ai_clean) a FLUX Kontext tünteti el
    (kreditbe kerül, a kimenetet ORB-homográfiával illesztjük vissza, mint a fotó-helyreállításnál).
  • PDF saját szövegréteggel: a szöveg PONTOSAN jön (span: betű, méret, szín, doboz), a háttérből kitakarással
    (redaction, csak a szöveg — kép és vonalrajz marad) vesszük ki; a beágyazott képek külön elemek lesznek.
Az eredmény mellé előtte–utána összevetés (egymás mellett) és ellenőrzés (check): minden szöveg benne van-e a
kész PDF-ben, és mennyire hasonlít a kép az eredetire.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.config import settings

log = logging.getLogger("aice.rebuild")

PROMPT_VER = "rebuild-v1"
A_RATIO = 2 ** 0.5
# a látómodell betűkategóriája → a szerveren meglévő család (fc-list, aice-test:product 10-12)
FONTS = {"sans": "Source Sans 3", "serif": "Noto Serif", "condensed": "Nimbus Sans Narrow", "geometric": "URW Gothic",
         "script": "Z003", "mono": "DejaVu Sans Mono", "slab": "Noto Serif", "display": "Noto Sans Display"}
_STYLE = {("regular", False): None, ("regular", True): "Italic", ("semibold", False): "Semibold", ("semibold", True): "Semibold Italic",
          ("bold", False): "Bold", ("bold", True): "Bold Italic", ("black", False): "Bold", ("black", True): "Bold Italic"}

_PROMPT = """This is a poster, flyer, cover, card or social-media graphic (a photo, a scan or a screenshot of it).
Rebuild it as JSON so it can be re-created as an EDITABLE design.{hint} Reply with ONE json object:
{"items": [
  {"type": "text", "text": "...", "box": [x0, y0, x1, y1], "lines": 1, "weight": "regular|semibold|bold|black", "italic": false,
   "font": "sans|serif|condensed|geometric|script|mono|slab", "align": "left|center|right", "caps": false},
  {"type": "shape", "shape": "rect|ellipse", "box": [x0, y0, x1, y1]},
  {"type": "picture", "box": [x0, y0, x1, y1], "caption": "photo|logo|illustration|icon"}
]}
Coordinates: integers 0-1000, thousandths of the image width and height, origin top left.
Text: one item per block of text that shares one look (a headline, a date line, a paragraph, a button label); "lines" =
how many printed lines it has; the text exactly as printed (language, accents, line breaks as "\\n"); "caps": true when
printed in capitals. The box must hug the letters tightly. Pictures: photos, logos, illustrations and icons placed on the
design — not the background itself. Shapes: only clear solid-colour blocks, bands, badges, buttons or frames.
List items from back to front. Output only the JSON."""


@dataclass
class Item:
    kind: str                                  # text | shape | picture
    box: tuple[float, float, float, float]     # px a forrásképen
    text: str = ""
    lines: int = 1
    weight: str = "regular"
    italic: bool = False
    font: str = "sans"
    align: str = "left"
    caps: bool = False
    size_pt: Optional[float] = None            # PDF-ből pontosan
    color: Optional[str] = None
    caption: str = ""
    extra: dict = field(default_factory=dict)


def _hex(c) -> str:
    return "#%02X%02X%02X" % tuple(0 if v != v else int(max(0, min(255, round(v)))) for v in c[:3])   # NaN → 0


# ---------- forrás ----------

def _doc_size(w: int, h: int) -> tuple[float, float]:
    """A grafika mérete pontban: A-sorozatú arány → A4 (álló/fekvő); máskülönben 1080 pt széles (közösségi méret)."""
    ar = h / w
    if abs(ar - A_RATIO) / A_RATIO < 0.035:
        return 595.3, 841.9
    if abs(ar - 1 / A_RATIO) * A_RATIO < 0.035:
        return 841.9, 595.3
    return 1080.0, round(1080.0 * ar, 1)


def _parse_json(text: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(text[text.find("{"):text.rfind("}") + 1])


async def _read_layout(img: Path, hint: Optional[str]) -> list[dict[str, Any]]:
    from app import db
    from app.llm import vision
    sha = hashlib.sha256(img.read_bytes()).hexdigest()
    ver = PROMPT_VER + (f":{hint}" if hint else "")
    try:
        cached = await db.ocr_cache_get(sha, ver, settings.OCR_MODEL)
    except Exception:
        cached = None
    if cached:
        return json.loads(cached).get("items") or []
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": _PROMPT.replace("{hint}", f" It is in {hint}." if hint else "")},
        {"type": "image_url", "image_url": {"url": vision.image_to_data_url(img, long_edge=2048)}},
    ]}]
    last: Exception = RuntimeError("no answer")
    for model in (settings.OCR_MODEL, settings.LLM_MODEL_VISION_FALLBACK):
        try:
            data = _parse_json(await vision._call(msgs, max_tokens=8000, model=model))
            if isinstance(data.get("items"), list):
                break
        except Exception as e:  # rossz JSON / nem válaszolt → tartalék modell
            last = e
            log.warning("plakát-szerkezet %s: %s", model, e)
    else:
        raise RuntimeError(f"Could not read the design: {last}")
    try:
        await db.ocr_cache_put(sha, ver, settings.OCR_MODEL, json.dumps(data, ensure_ascii=False))
    except Exception:
        log.warning("ocr_cache írás nem sikerült", exc_info=True)
    return data["items"]


def _items_from_layout(raw: list[dict[str, Any]], w: int, h: int) -> list[Item]:
    out = []
    for it in raw:
        try:
            b = [float(v) for v in it.get("box") or []]
            if len(b) != 4:
                continue
            box = (min(b[0], b[2]) * w / 1000, min(b[1], b[3]) * h / 1000, max(b[0], b[2]) * w / 1000, max(b[1], b[3]) * h / 1000)
            if box[2] - box[0] < 3 or box[3] - box[1] < 3:
                continue
            k = it.get("type")
            if k == "text" and str(it.get("text") or "").strip():
                out.append(Item("text", box, str(it["text"]).replace("\\n", "\n").strip(), max(1, int(it.get("lines") or 1)),
                                str(it.get("weight") or "regular"), bool(it.get("italic")), str(it.get("font") or "sans"),
                                str(it.get("align") or "left"), bool(it.get("caps"))))
            elif k in ("shape", "picture"):
                out.append(Item(k, box, caption=str(it.get("caption") or it.get("shape") or ""),
                                extra={"shape": it.get("shape") or "rect"}))
        except (TypeError, ValueError):
            continue
    return out


# ---------- pixelmérés + háttértisztítás (kép-út) ----------

def _ring(img, box, pad) -> tuple["np.ndarray", float]:  # noqa: F821
    """A doboz körüli gyűrű pixelei és a lefedettsége (a kép szélén levágott gyűrű < 1): a háttér becslése."""
    import numpy as np
    H, W = img.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    X0, Y0, X1, Y1 = max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad)
    m = np.ones((max(0, Y1 - Y0), max(0, X1 - X0)), bool)
    m[max(0, y0 - Y0):max(0, y1 - Y0), max(0, x0 - X0):max(0, x1 - X0)] = False
    full = (x1 - x0 + 2 * pad) * (y1 - y0 + 2 * pad) - (x1 - x0) * (y1 - y0)
    return img[Y0:Y1, X0:X1][m].reshape(-1, 3).astype(np.float32), float(m.sum()) / max(1, full)


def _uniform(px, cover: float) -> bool:
    """Egyszínű háttér a doboz körül: a gyűrű zöme a képen belül van, és a gyűrű egyszínű (_solid)."""
    return bool(len(px) and cover >= 0.75 and _solid(px))


def _solid(px, tol: float = 10) -> bool:
    """Egyszínű-e egy terület: a pixelek ≥85%-a ±tol-on belül a mediántól. (A szórás nem elég: egy fotó fölötti sötét,
    áttetsző sáv szórása a szöveg nélkül ~13 — mérve 10-12 —, mégsem egyszínű.)"""
    import numpy as np
    if not len(px):
        return False
    return float((np.abs(px - np.median(px, axis=0)).max(axis=1) <= tol).mean()) >= 0.85


def _refine_small(img, box) -> Optional[tuple[tuple[float, float, float, float], str]]:
    """Kis alakzat (vonal, jelvény, gomb) pontos helye és színe: a kibővített dobozban a környezettől (medián) elütő
    pixelek legnagyobb összefüggő foltja — a látómodell doboza pár pixellel mellémehet."""
    import cv2
    import numpy as np
    H, W = img.shape[:2]
    x0, y0, x1, y1 = box
    pad = max(8, int(min(x1 - x0, y1 - y0)), int(0.015 * min(H, W)))
    X0, Y0, X1, Y1 = int(max(0, x0 - pad)), int(max(0, y0 - pad)), int(min(W, x1 + pad)), int(min(H, y1 + pad))
    reg = img[Y0:Y1, X0:X1].astype(np.float32)
    if reg.size == 0:
        return None
    d = np.abs(reg - np.median(reg.reshape(-1, 3), axis=0)).max(axis=2)
    m = (d > 40).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    if n < 2:
        return None
    k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, w, h, area = stats[k]
    if area < 12 or area < 0.3 * w * h:
        return None
    comp = (lab == k).astype(np.uint8)
    e = max(1, min(int(w), int(h)) // 5)                # a mag: az élsimított szélsorok nélkül
    core = cv2.erode(comp, np.ones((2 * e + 1, 2 * e + 1), np.uint8)).astype(bool)
    px = reg[core] if core.sum() >= 6 else reg[comp.astype(bool)]
    # fotózott plakát: zaj + fényesés + JPEG — egy téglalap alakú folt (vonal, sáv, gomb) magjánál elég, ha a zöme
    # ±30-on belül van (mérve 10-12: a sárga akcentvonal magja 84%); szabálytalan foltnál szigorúbb a próba
    rect_like = area >= 0.8 * w * h
    dev = np.abs(px - np.median(px, axis=0)).max(axis=1)
    if not (float((dev <= 30).mean()) >= 0.6 if rect_like else _solid(px, tol=22)):
        return None
    return (X0 + x, Y0 + y, X0 + x + w, Y0 + y + h), _hex(np.median(px, axis=0))


def _measure_text(img, it: Item) -> tuple[str, "np.ndarray", str, bool]:  # noqa: F821
    """(festékszín, festék-maszk a dobozban, háttérszín, egyszínű-e a háttér) — a doboz körüli gyűrű a háttér."""
    import numpy as np
    x0, y0, x1, y1 = (int(round(v)) for v in it.box)
    pad = max(3, int((y1 - y0) * 0.18))
    ring, cover = _ring(img, it.box, pad)
    bg = np.median(ring, axis=0) if len(ring) else np.array([255, 255, 255], np.float32)
    uniform = _uniform(ring, cover)
    crop = img[max(0, y0):y1, max(0, x0):x1].astype(np.float32)
    if crop.size == 0:
        return "#000000", np.zeros((0, 0), bool), _hex(bg), uniform
    dist = np.linalg.norm(crop - bg, axis=2)
    thr = max(38.0, 0.45 * float(np.percentile(dist, 97)))
    ink = dist > thr
    frac = float(ink.mean())
    if frac < 0.01 or frac > 0.75:                     # nem különül el a háttértől → Otsu a szürke képen
        import cv2
        g = cv2.cvtColor(crop.astype(np.uint8), cv2.COLOR_RGB2GRAY)
        _, th = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        ink = th > 0
        if ink.mean() > 0.5:
            ink = ~ink
    # a festék színe: a háttértől legjobban elütő tizede (az élsimított szélpixelek a háttér felé kevertek — vékony
    # betűnél a medián a háttér felé húz: 10-12, fehér törzsszöveg sötétkéken → szürke lett)
    strong = crop[ink & (dist >= np.percentile(dist[ink], 90))] if ink.any() else crop.reshape(-1, 3)
    if not len(strong):
        strong = crop[ink] if ink.any() else crop.reshape(-1, 3)
    color = np.median(strong.reshape(-1, 3), axis=0)
    return _hex(color), ink, _hex(bg), uniform


def _clean(img, items: list[Item], measures: dict[int, tuple]) -> tuple["np.ndarray", dict[int, bool], dict[int, str]]:  # noqa: F821
    """Szöveg nélküli háttér + vektorizálható alakzatok.
    1. szöveg: egyszínű gyűrűnél kitöltés a háttérszínnel (tiszta), máshol OpenCV inpaint (festék-maszk, kicsit hízlalva);
    2. alakzat: vektoros, ha a belseje (a szöveg nélküli képen) egyszínű — áttetsző sáv egy fotó fölött NEM az;
    3. a kép/alakzat helye csak akkor ürül ki (külön, mozgatható elem lesz), ha körülötte egyszínű a háttér, és a gyűrű
       zöme a képen belül van (a kép szélén álló sávnál a csonka gyűrű félrevezet — 10-12: egy áttetsző sáv alól a
       fotó tűnt el így). Vissza: (háttér, {elem-index: kiürült-e}, {alakzat-index: szín})."""
    import cv2
    import numpy as np
    out = img.copy()
    H, W = img.shape[:2]
    mask = np.zeros((H, W), np.uint8)
    for i, it in enumerate(items):
        if it.kind != "text":
            continue
        color, ink, bg, uniform = measures[i]
        x0, y0, x1, y1 = (int(round(v)) for v in it.box)
        x0, y0 = max(0, x0), max(0, y0)
        if uniform:
            pad = max(2, int((y1 - y0) * 0.08))
            cv2.rectangle(out, (max(0, x0 - pad), max(0, y0 - pad)), (min(W, x1 + pad), min(H, y1 + pad)),
                          tuple(int(bg[k:k + 2], 16) for k in (1, 3, 5)), -1)
        elif ink.size:
            k = max(2, int((y1 - y0) * 0.06))
            sub = cv2.dilate(ink.astype(np.uint8) * 255, np.ones((2 * k + 1, 2 * k + 1), np.uint8))
            hh, ww = sub.shape
            mask[y0:y0 + hh, x0:x0 + ww] = np.maximum(mask[y0:y0 + hh, x0:x0 + ww], sub[:H - y0, :W - x0])
    if mask.any():
        out = cv2.inpaint(out, mask, max(3, int(min(H, W) * 0.006)), cv2.INPAINT_TELEA)
    cleared: dict[int, bool] = {}
    shapes: dict[int, str] = {}
    base = out.copy()                                   # a szöveg nélküli kép: erről mérünk
    for i, it in enumerate(items):
        if it.kind == "text":
            continue
        if it.kind == "shape":
            if (it.box[2] - it.box[0]) * (it.box[3] - it.box[1]) < 0.02 * W * H:
                hit = _refine_small(base, it.box)       # vonal, jelvény: a pixelekből pontosított doboz és szín
                if not hit:
                    continue                            # nem találtuk meg biztosan → a háttér része marad (nem festünk rá)
                it.box, shapes[i] = hit
            else:
                x0, y0, x1, y1 = (int(round(v)) for v in it.box)
                inner = base[max(0, y0):y1, max(0, x0):x1].reshape(-1, 3).astype(np.float32)
                if not _solid(inner):
                    continue                            # nem egyszínű (pl. áttetsző sáv fotó fölött) → a háttér része marad
                shapes[i] = _hex(np.median(inner, axis=0))
        x0, y0, x1, y1 = (int(round(v)) for v in it.box)
        pad = max(3, int(min(x1 - x0, y1 - y0) * 0.06))
        ring, cover = _ring(base, it.box, pad)
        if _uniform(ring, cover):
            cv2.rectangle(out, (max(0, x0 - 1), max(0, y0 - 1)), (min(W, x1 + 1), min(H, y1 + 1)),
                          tuple(int(v) for v in np.median(ring, axis=0)), -1)
            cleared[i] = True
        else:
            cleared[i] = False
    return out, cleared, shapes


# ---------- PDF-út ----------

# PDF-betűnév → a szerveren meglévő család (a beágyazott részhalmaz-előtag nélkül: „ABCDEF+SourceSans3-Bold”)
_PDF_FONTS = {"sourcesans3": "Source Sans 3", "sourcesanspro": "Source Sans 3", "sourceserif4": "Source Serif 4",
              "notoserif": "Noto Serif", "notosansdisplay": "Noto Sans Display", "notosans": "Noto Sans",
              "dejavusansmono": "DejaVu Sans Mono", "dejavusans": "DejaVu Sans", "dejavuserif": "DejaVu Serif",
              "liberationsansnarrow": "Liberation Sans Narrow", "liberationsans": "Liberation Sans",
              "liberationserif": "Liberation Serif", "liberationmono": "Liberation Mono", "carlito": "Carlito",
              "caladea": "Caladea", "urwgothic": "URW Gothic", "nimbussansnarrow": "Nimbus Sans Narrow",
              "nimbussans": "Nimbus Sans", "nimbusroman": "Nimbus Roman", "arialnarrow": "Liberation Sans Narrow",
              "arial": "Liberation Sans", "helvetica": "Nimbus Sans", "timesnewroman": "Liberation Serif",
              "times": "Nimbus Roman", "calibri": "Carlito", "cambria": "Caladea", "couriernew": "Liberation Mono",
              "courier": "Nimbus Mono PS", "georgia": "Noto Serif", "palatino": "P052", "bookman": "URW Bookman",
              "centuryschoolbook": "C059", "avantgarde": "URW Gothic", "zapfchancery": "Z003"}


def _pdf_family(font: str) -> Optional[str]:
    key = re.sub(r"[^a-z0-9]", "", font.split("+")[-1].split("-")[0].split(",")[0].lower())
    for k in sorted(_PDF_FONTS, key=len, reverse=True):
        if key.startswith(k):
            return _PDF_FONTS[k]
    return None


def _pdf_text_items(page, scale: float) -> list[Item]:
    """A PDF szövegrétege → szövegdobozok: egy dobozba csak az EGYMÁS ALATT álló, egyforma kinézetű sorok kerülnek
    (10-12: három egymás melletti kiemelő doboz szövege különben egy elemmé olvadt); a bekezdés tördelt sorai
    szóközzel fűződnek (a keretben újratördelődnek), rövid sor után kemény sortörés marad."""
    items: list[Item] = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        groups: list[list[dict]] = []
        for ln in b["lines"]:
            spans = [s for s in ln["spans"] if s["text"].strip()]
            if not spans:
                continue
            s0 = max(spans, key=lambda s: len(s["text"]))
            row = {"key": (s0["font"], round(s0["size"], 1), s0["color"]), "spans": spans, "bbox": ln["bbox"], "size": s0["size"]}
            if groups:
                prev = groups[-1][-1]
                gap = row["bbox"][1] - prev["bbox"][3]
                ov = min(prev["bbox"][2], row["bbox"][2]) - max(prev["bbox"][0], row["bbox"][0])
                narrow = min(prev["bbox"][2] - prev["bbox"][0], row["bbox"][2] - row["bbox"][0])
                if prev["key"] == row["key"] and -2 <= gap <= row["size"] * 0.9 and ov > 0.3 * narrow:
                    groups[-1].append(row)
                    continue
            groups.append([row])
        for g in groups:
            font, size, color = g[0]["key"]
            lines = ["".join(s["text"] for s in ln["spans"]).strip() for ln in g]
            widths = [ln["bbox"][2] - ln["bbox"][0] for ln in g]
            text = lines[0]
            for k in range(1, len(lines)):
                # tördelt sor (folytatás), ha a következő sor első szava NEM fért volna el az előző végén (rongyos szélnél
                # a sorhossz önmagában félrevezet); különben kemény sortörés volt
                cw = widths[k] / max(1, len(lines[k]))
                first = len(lines[k].split(" ")[0]) + 1
                if widths[k - 1] + cw * first > 0.97 * max(widths):
                    if text.endswith("-") and lines[k][:1].islower():
                        text = text[:-1] + lines[k]
                    else:
                        text += " " + lines[k]
                else:
                    text += "\n" + lines[k]
            x0 = min(ln["bbox"][0] for ln in g); y0 = min(ln["bbox"][1] for ln in g)
            x1 = max(ln["bbox"][2] for ln in g); y1 = max(ln["bbox"][3] for ln in g)
            fl = g[0]["spans"][0]["flags"]
            fn = font.lower()
            fam = ("mono" if fl & 8 or "mono" in fn or "courier" in fn else
                   "script" if any(k in fn for k in ("script", "hand", "brush")) else
                   "condensed" if any(k in fn for k in ("condensed", "narrow", "compressed")) else
                   "serif" if "sans" not in fn and (fl & 4 or any(k in fn for k in ("serif", "times", "georgia", "garamond",
                                                                                 "minion", "caslon", "baskerville", "roman", "book")))
                   else "sans")
            weight = ("bold" if fl & 16 or any(k in fn for k in ("bold", "black", "heavy")) else
                      "semibold" if any(k in fn for k in ("semibold", "demi", "medium")) else "regular")
            starts, ends = [ln["bbox"][0] for ln in g], [ln["bbox"][2] for ln in g]
            mids = [(a + c) / 2 for a, c in zip(starts, ends)]
            align = ("left" if len(g) == 1 or max(starts) - min(starts) < 2 else "right" if max(ends) - min(ends) < 2
                     else "center" if max(mids) - min(mids) < 3 else "left")
            it = Item("text", (x0 * scale, y0 * scale, x1 * scale, y1 * scale), text, len(g), weight,
                      bool(fl & 2 or "italic" in fn or "oblique" in fn or re.search(r"-it\b|italic", fn)), fam, align, False,
                      size_pt=size, color="#%06X" % color)
            fam_exact = _pdf_family(font)
            if fam_exact:
                it.extra["family"] = fam_exact
            items.append(it)
    return items


def _pdf_items(pdf: Path, page_no: int, scale: float, tmp: Path) -> tuple[list[Item], list[tuple[Path, tuple]], Path, tuple[float, float]]:
    """PDF-oldal → (szövegelemek pontosan, beágyazott képek (fájl, doboz px), szöveg nélküli háttérkép, oldalméret pt).
    Sorrend: 1. a szöveg kitakarása (kép és vonalrajz marad) → 2. a képek kivágása (már szöveg nélkül) → 3. a képek
    kitakarása → 4. a háttér renderelése."""
    import pymupdf as fitz
    doc = fitz.open(str(pdf))
    if not 1 <= page_no <= doc.page_count:
        raise ValueError(f"The PDF has {doc.page_count} page(s).")
    page = doc[page_no - 1]
    pw, ph = page.rect.width, page.rect.height
    items = _pdf_text_items(page, scale)
    for it in items:
        page.add_redact_annot(fitz.Rect(*(v / scale for v in it.box)))
    if items:
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)
    pics: list[tuple[Path, tuple]] = []
    for k, info in enumerate(page.get_image_info(xrefs=True)):
        r = fitz.Rect(info["bbox"]) & page.rect
        if r.is_empty or r.width * r.height < 0.004 * pw * ph or r.width * r.height > 0.9 * pw * ph:
            continue                                         # apró díszek és a teljes oldalas háttérkép maradnak a háttérben
        p = tmp / f"pic{k}.png"
        page.get_pixmap(clip=r, matrix=fitz.Matrix(scale * 1.5, scale * 1.5)).save(str(p))
        pics.append((p, (r.x0 * scale, r.y0 * scale, r.x1 * scale, r.y1 * scale)))
    if pics:
        for _, box in pics:
            page.add_redact_annot(fitz.Rect(*(v / scale for v in box)))
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE, graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                              text=fitz.PDF_REDACT_TEXT_NONE)
    bg = tmp / "bg.png"
    page.get_pixmap(matrix=fitz.Matrix(scale, scale)).save(str(bg))
    doc.close()
    return items, pics, bg, (pw, ph)


# ---------- VectorCraft-dokumentum ----------

def _size_for(it: Item, s: float) -> tuple[float, Optional[float]]:
    """Betűméret (pt) és sorköz egy szövegdobozhoz; s = pt / px."""
    if it.size_pt:
        return it.size_pt * 1.0, (it.box[3] - it.box[1]) * s / it.lines if it.lines > 1 else None
    line_h = (it.box[3] - it.box[1]) * s / max(1, it.lines)
    caps = it.caps or (it.text.upper() == it.text and any(c.isalpha() for c in it.text))
    size = line_h / (0.74 if caps and it.lines == 1 else 1.18 if it.lines > 1 else 1.0)
    return round(size, 1), (round(line_h, 1) if it.lines > 1 else None)


def _commands(items: list[Item], measures: dict[int, tuple], bg: Path, pics: dict[int, Path], cleared: dict[int, bool],
              shape_colors: dict[int, str], W: float, H: float, s: float, name: str) -> list[dict[str, Any]]:
    from app.tools.design import Doc
    d = Doc(W, H, name)
    d.image(bg, 0, 0, W, H)
    for i, it in enumerate(items):                      # hátulról előre: alakzatok, képek, végül a szöveg
        x0, y0, x1, y1 = (v * s for v in it.box)
        if it.kind == "shape" and i in shape_colors:
            if it.extra.get("shape") == "ellipse":
                d._add("shape.ellipse", {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}, creates=True)
                d._add("paint.setFill", {"color": shape_colors[i]}); d._add("paint.setStroke", {"none": True})
            else:
                d.rect(x0, y0, x1 - x0, y1 - y0, shape_colors[i])
        elif it.kind == "picture" and i in pics and cleared.get(i, True):
            d.image(pics[i], x0, y0, x1 - x0, y1 - y0)
    for i, it in enumerate(items):
        if it.kind != "text":
            continue
        x0, y0, x1, y1 = (v * s for v in it.box)
        size, leading = _size_for(it, s)
        color = it.color or measures[i][0]
        style = _STYLE.get((it.weight if it.weight in ("regular", "semibold", "bold", "black") else "regular", it.italic))
        fam = it.extra.get("family") or FONTS.get(it.font, FONTS["sans"])
        if fam != FONTS["sans"] and style and "Semibold" in style:
            style = style.replace("Semibold", "Bold")      # a rendszerbetűknek nincs félkövér vágása
        # a PDF-ből jövő szöveg mérete pontos → szűk ráhagyás (ugyanúgy tördeljen); a képről becsült → bővebb
        padx, pady = ((x1 - x0) * 0.01 + 1.5, (y1 - y0) * 0.04 + 1) if it.size_pt else ((x1 - x0) * 0.04 + 2, (y1 - y0) * 0.08 + 1)
        d.text(x0 - padx, y0 - pady, it.text, size=size, font=fam, color=color, style=style, w=(x1 - x0) + 2 * padx,
               h=(y1 - y0) + 2 * pady + size * 0.25, align=it.align if it.align in ("left", "center", "right") else None,
               leading=leading, fit="shrinkText")
    return d.cmds


# ---------- ellenőrzés ----------

def _similarity(a: Path, b: Path) -> float:
    """SSIM a szürke, 320 px-re kicsinyített, enyhén elmosott képeken (0–1) — a szerkezetet méri, nem a fotózajt
    (egy telefonnal fotózott plakát zaja a tiszta újraépítéshez képest különben 0,55-öt adott, mérve 10-12)."""
    import cv2
    import numpy as np
    A = cv2.imread(str(a), cv2.IMREAD_GRAYSCALE)
    B = cv2.imread(str(b), cv2.IMREAD_GRAYSCALE)
    if A is None or B is None:
        return 0.0
    w = 320
    h = max(1, int(A.shape[0] * w / A.shape[1]))
    A = cv2.GaussianBlur(cv2.resize(A, (w, h), interpolation=cv2.INTER_AREA), (0, 0), 1.2).astype(np.float64)
    B = cv2.GaussianBlur(cv2.resize(B, (w, h), interpolation=cv2.INTER_AREA), (0, 0), 1.2).astype(np.float64)
    C1, C2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    g = lambda x: cv2.GaussianBlur(x, (11, 11), 1.5)
    mA, mB = g(A), g(B)
    sA, sB, sAB = g(A * A) - mA ** 2, g(B * B) - mB ** 2, g(A * B) - mA * mB
    ssim = ((2 * mA * mB + C1) * (2 * sAB + C2)) / ((mA ** 2 + mB ** 2 + C1) * (sA + sB + C2))
    return float(ssim.mean())


def _side_by_side(a: Path, b: Path, dst: Path) -> None:
    from PIL import Image, ImageDraw
    A, B = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
    h = 900
    A = A.resize((max(1, int(A.width * h / A.height)), h))
    B = B.resize((max(1, int(B.width * h / B.height)), h))
    out = Image.new("RGB", (A.width + B.width + 30, h + 44), "#F4F5F7")
    out.paste(A, (0, 44)); out.paste(B, (A.width + 30, 44))
    dr = ImageDraw.Draw(out)
    dr.text((10, 12), "Before (original)", fill="#1E2430")
    dr.text((A.width + 40, 12), "After (rebuilt, every text editable)", fill="#1E2430")
    out.save(dst, "PNG")


def _norm(t: str) -> str:
    return re.sub(r"\s+", "", t).lower()


def _is_camera_photo(path: Path) -> bool:
    """Fényképezőgéppel / telefonnal készült fotó (EXIF Make/Model) — csak ezt egyenesítjük ki: egy digitális
    grafikán a papír-kereső egy nagy belső fotókeretet nézhetne papírnak, és levágná a dizájnt."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            ex = im.getexif()
            return bool(ex.get(0x010F) or ex.get(0x0110))
    except Exception:
        return False


@dataclass
class _Prep:
    items: list[Item]
    orig: Path                                  # az eredeti (kiegyenesített) kép az összevetéshez
    bg: Path                                    # szöveg nélküli háttér
    W: float
    H: float
    s: float                                    # pt / px
    measures: dict[int, tuple]
    pics: dict[int, Path]
    cleared: dict[int, bool]
    shapes: dict[int, str]
    notes: list[str]
    charged: bool = False


async def _prep_pdf(src: Path, page: int, tmp: Path) -> _Prep:
    import pymupdf as fitz
    with fitz.open(str(src)) as d0:
        if not 1 <= page <= d0.page_count:
            raise ValueError(f"The PDF has {d0.page_count} page(s).")
        pw0 = d0[page - 1].rect.width
        scale = max(1.0, 2000 / max(pw0, 1))           # ~2000 px széles render
        orig = tmp / "orig.png"
        d0[page - 1].get_pixmap(matrix=fitz.Matrix(scale, scale)).save(str(orig))
    texts, pdf_pics, bg, (W, H) = await asyncio.to_thread(_pdf_items, src, page, scale, tmp)
    if not texts:
        raise ValueError("This PDF page has no text layer — save it as a picture (or scan it) and rebuild that.")
    items = [Item("picture", box, caption="photo") for _, box in pdf_pics] + texts
    pics = {i: p for i, (p, _) in enumerate(pdf_pics)}
    return _Prep(items, orig, bg, W, H, 1 / scale, {i: (it.color,) for i, it in enumerate(items) if it.kind == "text"},
                 pics, {i: True for i in pics}, {},
                 [f"The text came from the PDF's own text layer ({len(texts)} blocks, exact fonts and sizes)."])


async def _prep_image(src: Path, tmp: Path, file_id: str, ai_clean: bool, hint: Optional[str]) -> _Prep:
    import cv2
    import numpy as np
    from app.tools import forms
    straight = tmp / "straight.jpg"
    if _is_camera_photo(src):
        await asyncio.to_thread(forms.straighten, src, straight)
        img = cv2.imread(str(straight))
    else:
        from PIL import Image, ImageOps
        with Image.open(src) as im:
            img = cv2.cvtColor(np.asarray(ImageOps.exif_transpose(im).convert("RGB")), cv2.COLOR_RGB2BGR)
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    ih, iw = img.shape[:2]
    if max(ih, iw) > 2400:                              # elég részlet, gyorsabb mérés
        f = 2400 / max(ih, iw)
        img = cv2.resize(img, (int(iw * f), int(ih * f)), interpolation=cv2.INTER_AREA)
        ih, iw = img.shape[:2]
    W, H = _doc_size(iw, ih)
    if abs(ih - round(iw * H / W)) > 1:                 # A4-re igazított oldal: a kép is pontosan erre az arányra
        ih = round(iw * H / W)                          # (≤3,5% nyújtás) — különben a háttér arányosan illesztve
        img = cv2.resize(img, (iw, ih), interpolation=cv2.INTER_AREA)   # csíkot hagy (10-12: 5 px fekete sáv)
    orig = tmp / "orig.png"
    cv2.imwrite(str(orig), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    items = _items_from_layout(await _read_layout(orig, hint), iw, ih)
    if not any(it.kind == "text" for it in items):
        raise ValueError("No text found on this picture — rebuild needs a poster, flyer or graphic with text.")
    measures = {i: _measure_text(img, it) for i, it in enumerate(items) if it.kind == "text"}
    notes, charged = [], False
    if ai_clean:                                        # kérésre: a Kontext tünteti el a szöveget (kreditbe kerül)
        from app.tools import restore
        charged = await restore._charge(1, f"rebuild:{file_id}", "AI text removal")
        try:
            out = await restore._kontext(orig, "Remove all the text, letters and numbers from this image. Keep everything "
                                               "else exactly the same: the photos, shapes, colours, background and framing.")
            cleaned = tmp / "ai_clean.png"
            pts = await asyncio.to_thread(restore.align, orig, out, cleaned)
            bg_img = cv2.cvtColor(cv2.imread(str(cleaned)), cv2.COLOR_BGR2RGB)
            if bg_img.shape[:2] != img.shape[:2]:
                bg_img = cv2.resize(bg_img, (iw, ih), interpolation=cv2.INTER_CUBIC)
        except Exception:
            if charged:
                await restore._refund(1, f"rebuild:{file_id}")
            raise
        cleared, shapes = {i: False for i, it in enumerate(items) if it.kind != "text"}, {}
        notes.append(f"The old text was removed from the background by AI (aligned on {pts} points).")
    else:
        bg_img, cleared, shapes = await asyncio.to_thread(_clean, img, items, measures)
        notes.append("The old text was removed from the background for free (fill / inpaint); ask for the AI clean-up if traces remain.")
    bg = tmp / "bg.png"
    cv2.imwrite(str(bg), cv2.cvtColor(bg_img, cv2.COLOR_RGB2BGR))
    pics: dict[int, Path] = {}
    for i, it in enumerate(items):
        x0, y0, x1, y1 = (int(round(v)) for v in it.box)
        if it.kind == "picture" and cleared.get(i):
            crop = img[max(0, y0):y1, max(0, x0):x1]
            if crop.size:
                p = tmp / f"pic{i}.png"
                cv2.imwrite(str(p), cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))
                pics[i] = p
    return _Prep(items, orig, bg, W, H, W / iw, measures, pics, cleared, shapes, notes, charged)


async def rebuild(file_id: str, *, page: int = 1, ai_clean: bool = False, hint: Optional[str] = None) -> dict[str, Any]:
    """Kép / PDF-plakát → {files: [.vectorcraft, png, pdf], file_id (.vectorcraft), comparison_url, check, items}."""
    import shutil
    from app.routes.make import _new_group
    from app.storage import display_label, output_path, public_url, write_meta
    from app.storage.files import new_file_id
    from app.tools import crafts
    from app.tools.crafts import existing

    src = existing(file_id)
    ext = src.suffix.lower().lstrip(".")
    if ext != "pdf" and ext not in ("jpg", "jpeg", "png", "webp", "bmp", "tif", "tiff", "gif"):
        raise ValueError("Rebuild works on a picture (JPG, PNG, WebP) or a PDF.")
    name = f"{Path(display_label(file_id)).stem} (rebuilt)"
    tmp = Path(tempfile.mkdtemp(prefix="rebuild-"))
    try:
        P = await (_prep_pdf(src, page, tmp) if ext == "pdf" else _prep_image(src, tmp, file_id, ai_clean, hint))
        cmds = _commands(P.items, P.measures, P.bg, P.pics, P.cleared, P.shapes, P.W, P.H, P.s, name)
        res = await crafts.vector_run(commands=cmds, export=["vectorcraft", "png", "pdf"])
        group = _new_group()
        for f in res["files"]:
            f["label"] = f"{name}.{f['file_id'].rsplit('.', 1)[-1]}"
            write_meta(f["file_id"], source_file_id=file_id, operation="rebuild", label=f["label"], extra={"group": group})
        png = output_path(next(f["file_id"] for f in res["files"] if f["file_id"].endswith(".png")))
        pdf = output_path(next(f["file_id"] for f in res["files"] if f["file_id"].endswith(".pdf")))
        findings, verified = [], list(P.notes)
        # ellenőrzés 1: minden szöveg valódi szövegként olvasható vissza a kész PDF-ből
        proc = await asyncio.create_subprocess_exec("pdftotext", str(pdf), "-", stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        got = _norm((await proc.communicate())[0].decode("utf-8", "replace"))
        texts = [it.text for it in P.items if it.kind == "text"]
        missing = [t for t in texts if _norm(t) not in got]
        if missing:
            findings.append(f"{len(missing)} of {len(texts)} text blocks don't read back from the result: "
                            + "; ".join(f"“{m[:40]}”" for m in missing[:5]))
        else:
            verified.append(f"All {len(texts)} text blocks are real, editable text in the result.")
        # ellenőrzés 2: hasonlóság az eredetihez + összevetés egymás mellett
        sim = await asyncio.to_thread(_similarity, P.orig, png)
        (verified if sim >= 0.6 else findings).append(
            f"Similarity to the original: {sim:.2f} (1 = identical) — compare them side by side.")
        n_all = sum(1 for it in P.items if it.kind == "picture")
        if n_all:
            verified.append(f"{len(P.pics)} of {n_all} pictures are separate, movable images"
                            + ("" if len(P.pics) == n_all else "; the rest stay in the background (they sit on a busy backdrop)") + ".")
        if P.shapes:
            verified.append(f"{len(P.shapes)} solid colour blocks are editable shapes.")
        cmp_id = new_file_id("png")
        await asyncio.to_thread(_side_by_side, P.orig, png, output_path(cmp_id))
        write_meta(cmp_id, source_file_id=file_id, operation="rebuild comparison", label=f"{name} (before and after).png",
                   extra={"ephemeral": True})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"files": res["files"], "file_id": res["file_id"], "template": "rebuild", "comparison": cmp_id,
            "comparison_url": public_url(cmp_id), "charged_credits": 1 if P.charged else 0,
            "check": {"status": "ok" if not findings else "needs_check", "findings": findings, "verified": verified},
            "items": {"text": len(texts), "pictures": len(P.pics), "shapes": len(P.shapes)}}
