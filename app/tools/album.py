"""Fotóalbum / kiadvány a DesignCraftban (InDesign-szerű tördelő): kézirat + fotók → oldalpárok, a fotók a SAJÁT
arányukban (nincs levágott fej), képaláírás, tartalomjegyzék, borító, nyomdai PDF kifutóval és vágójelekkel, mellé
a szerkeszthető .designcraft (a layout-szerkesztőben nyílik) és egy képernyős PDF.

Miért a DesignCraft (és nem a Word vagy a VectorCraft): valódi oldalpárok (bal/jobb oldal tükörmargóval), a
dokumentum kifutója és vágójelek a PDF-ben, tartalomjegyzék bekezdésstílusból, képkeret + arányos illesztés,
szövegfolyatás új oldalakra, IDML-be is kiírható (nyomdának). A Wordnek nincs kifutója és pontos képkerete,
a VectorCraft nem folyat szöveget oldalról oldalra, és nincs tartalomjegyzéke.

A designcraft-cli (0.6.0) tapasztalatai (10-12, kísérletekkel):
  • a frame.create az új keretet KIJELÖLI → a file.place / object.fit / style.paragraph.apply id nélkül rá hat;
  • a bekezdésstílus csak a kurzor bekezdésére hat → minden címsor / képaláírás SAJÁT keretben, egy bekezdés
    (sortörés: U+2028); a törzsszöveg sima .txt-ként folyik, a [Basic Paragraph] átszabva a törzsbetűre. A docx-ból
    hozott „heading 1”-re se a styleMap, se a stílusszerkesztés nem hatott, és a tartalomjegyzék sem találta;
  • a file.place autoflow és a layout.pages.insert a dokumentum VÉGÉRE tesz oldalt → sorrendben építünk, és az
    autoflow hozzáadott oldalait egy előzetes MÉRŐFUTÁS adja meg (ugyanaz a keret, ugyanaz a betű), így minden
    későbbi keret oldalszáma előre ismert;
  • oldalpár-koordináták: az 1. oldal egyedül áll (0. spread, jobb oldal, x 0…W); a p. oldal (p ≥ 2) a p//2.
    spreaden van, páros = bal (x 0…W), páratlan = jobb (x W…2W).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.config import settings

log = logging.getLogger("aice.album")

PT_PER_MM = 72 / 25.4
BLEED = round(3 * PT_PER_MM, 2)                 # 3 mm kifutó
SIZES = {"A4": (595.3, 841.9), "A4-landscape": (841.9, 595.3), "square": (595.3, 595.3), "A5": (419.5, 595.3)}
# a DesignCraftba BEÉPÍTETT betűk (a böngészős layout-szerkesztő is ezeket mutatja — a szerver Noto Serif-je ott
# hiányzó betűként, rózsaszínnel jelenne meg; mérve 10-12)
F_HEAD, F_BODY = "Source Serif 4", "Source Sans 3"
MIN_PPI = 150                                     # ez alatt a nyomat láthatóan lágy
MAX_PHOTOS = 120
LINE_BREAK = " "                             # kényszerített sortörés egy bekezdésen belül
_CAMERA = re.compile(r"^(img|dsc|dscn|dscf|pxl|mvimg|photo|image|picture|whatsapp|screenshot|scan|p\d|\d)[\W_]*\d", re.I)


@dataclass
class Photo:
    path: Path
    w: int
    h: int
    caption: str
    credit: Optional[str]
    file_id: Optional[str]

    @property
    def ar(self) -> float:
        return self.w / self.h

    @property
    def name(self) -> str:
        from app.storage import display_label
        return self.caption or (display_label(self.file_id) if self.file_id else "a photo")


# ---------- bemenet ----------

def _caption_from_name(label: str) -> str:
    """Fájlnévből képaláírás — a fényképezőgép-nevekből (IMG_1234, DSC01234, PXL_…) nem."""
    stem = Path(label or "").stem
    if not stem or _CAMERA.match(stem) or sum(c.isdigit() for c in stem) > len(stem) * 0.4:
        return ""
    text = re.sub(r"\s*\(\d+\)$", "", re.sub(r"[_\-]+", " ", stem)).strip()
    return text[:1].upper() + text[1:]


def chapters_from(path: Path) -> list[tuple[str, str]]:
    """Kézirat → [(fejezetcím, szöveg)]. docx: a legfelső címszint (Title / Heading 1, ha nincs, Heading 2) kezd
    fejezetet; md: „# ”; txt: egy fejezet cím nélkül. A bekezdések sortöréssel elválasztva maradnak."""
    ext = path.suffix.lower().lstrip(".")
    paras: list[tuple[Optional[int], str]] = []          # (címszint vagy None, szöveg)
    if ext == "docx":
        from docx import Document
        for p in Document(str(path)).paragraphs:
            t = p.text.strip()
            if not t:
                continue
            name = (p.style.name or "").lower()
            m = re.match(r"heading (\d)", name)
            paras.append((1 if name == "title" else int(m.group(1)) if m else None, t))
    elif ext in ("txt", "md", "markdown", "text"):
        for line in path.read_text("utf-8", errors="replace").splitlines():
            t = line.strip()
            if not t:
                continue
            m = re.match(r"^(#{1,3})\s+(.*)$", t) if ext != "txt" else None
            paras.append((len(m.group(1)), m.group(2).strip()) if m else (None, t))
    else:
        raise ValueError("The manuscript must be a .docx, .md or .txt file.")
    levels = [lv for lv, _ in paras if lv]
    top = min(levels) if levels else None
    out: list[tuple[str, str]] = []
    for lv, t in paras:
        if top is not None and lv == top:
            out.append((t, ""))
        elif not out:
            out.append(("", t))
        else:
            out[-1] = (out[-1][0], (out[-1][1] + "\n" + t).strip())
    return [(h, b) for h, b in out if h or b]


async def _load_photos(specs: list[Any], captions: list[Optional[str]]) -> list[Photo]:
    from PIL import Image
    from app.storage import display_label
    from app.tools import images
    sem = asyncio.Semaphore(4)

    async def one(i: int, spec: Any) -> Photo:
        async with sem:
            r = await images.resolve(spec, max_px=3600)       # nyomtatáshoz nagy felbontás (A4 kifutva ~300 ppi)
        with Image.open(r.path) as im:
            w, h = im.size
        fid = spec if isinstance(spec, str) else (spec.get("file_id") if isinstance(spec, dict) else None)
        given = captions[i] if i < len(captions) else None
        cap = given if given is not None else (_caption_from_name(display_label(fid)) if fid else "")
        return Photo(r.path, w, h, (cap or "").strip(), r.attribution, fid)
    return list(await asyncio.gather(*(one(i, s) for i, s in enumerate(specs))))


async def _auto_captions(photos: list[Photo], sample: str) -> int:
    """Rövid képaláírás a látómodelltől (DeepSeek-V4.1-Flash, tartalék GLM), ahol nincs — a kézirat nyelvén."""
    from app.llm import vision
    lang = f"in the same language as this sample text: «{sample[:240]}»" if sample.strip() else "in English"
    prompt = (f"Write a short, warm photo-album caption for this picture, at most 8 words, {lang}. "
              "Say what is in the picture; no quotes, no hashtags, no full stop at the end. Reply with the caption only.")
    sem = asyncio.Semaphore(4)

    async def one(p: Photo) -> int:
        if p.caption:
            return 0
        msgs = [{"role": "user", "content": [{"type": "text", "text": prompt},
                                             {"type": "image_url", "image_url": {"url": vision.image_to_data_url(p.path, long_edge=768)}}]}]
        async with sem:
            for model in (settings.LLM_MODEL_VISION, settings.LLM_MODEL_VISION_FALLBACK):
                try:
                    txt = (await vision._call(msgs, max_tokens=60, model=model)).strip().strip('"„”«»').strip()
                    if txt:
                        p.caption = txt.splitlines()[0][:90].rstrip(".")
                        return 1
                except Exception as e:  # a képaláírás nem kötelező — a tördelés enélkül is megy
                    log.warning("képaláírás (%s): %s", model, e)
        return 0
    return sum(await asyncio.gather(*(one(p) for p in photos)))


# ---------- tördelés ----------

class Layout:
    """Parancslista-építő oldalpárokhoz; az oldalszámokat MI tartjuk nyilván (a CLI a végére tesz oldalt)."""

    def __init__(self, w: float, h: float, title: str):
        self.w, self.h = w, h
        m = min(w, h) * 0.075
        self.mg = {"top": round(m * 0.95, 1), "bottom": round(m * 1.25, 1), "inside": round(m * 1.2, 1), "outside": round(m, 1)}
        self.pages = 1
        self.cmds: list[tuple[str, Optional[dict]]] = [("file.new", {
            "width": w, "height": h, "pages": 1, "facingPages": True, "bleed": BLEED, "margins": self.mg,
            "title": title[:120]})]

    def add(self, cid: str, params: Optional[dict] = None) -> None:
        self.cmds.append((cid, params))

    def new_page(self) -> int:
        self.add("layout.pages.insert", {"count": 1})
        self.pages += 1
        return self.pages

    def box(self, p: int) -> tuple[int, float, float, float, float]:
        """A p. oldal szedéstükre spread-koordinátában: (spread, x0, y0, x1, y1)."""
        if p == 1:
            s, ox, right = 0, 0.0, True
        else:
            s, ox, right = p // 2, (self.w if p % 2 else 0.0), bool(p % 2)
        lm = self.mg["inside"] if right else self.mg["outside"]
        rm = self.mg["outside"] if right else self.mg["inside"]
        return s, ox + lm, self.mg["top"], ox + self.w - rm, self.h - self.mg["bottom"]

    def frame(self, spread: int, rect: list[float], **kw) -> None:
        self.add("frame.create", {"spread": spread, "rect": [round(v, 2) for v in rect], **kw})

    def text(self, spread: int, rect: list[float], text: str, style: str) -> None:
        """Egybekezdéses szövegkeret (a stílus a kurzor bekezdésére hat — több sor U+2028-cal)."""
        self.frame(spread, rect, content="text", text=re.sub(r"\s*\n\s*", LINE_BREAK, text.strip()))
        self.add("style.paragraph.apply", {"name": style})

    def place(self, spread: int, rect: list[float], path: Path) -> None:
        self.frame(spread, rect, content="graphic")
        self.add("file.place", {"path": str(path)})
        self.add("object.fit", {"mode": "fillProportionally"})

    def block(self, spread: int, rect: list[float], color: str, opacity: Optional[float] = None) -> None:
        self.frame(spread, rect, content="unassigned")
        self.add("object.color", {"color": color})
        if opacity is not None:
            self.add("object.opacity", {"opacity": opacity})


def _styles(L: Layout, t: dict[str, str]) -> None:
    m = min(L.w, L.h)
    for name, color in (("Album Ink", t["text"]), ("Album Muted", t["muted"]), ("Album White", "#FFFFFF")):
        L.add("swatch.create", {"name": name, "color": color})
    L.add("style.paragraph.edit", {"name": "[Basic Paragraph]", "chars": {
        "fontFamily": F_BODY, "size": round(m * 0.0185, 1), "leading": {"kind": "points", "value": round(m * 0.0275, 1)},
        "fill": "Album Ink"}, "para": {"spaceAfter": round(m * 0.009, 1)}})
    for name, chars in (
            ("Album Heading", {"fontFamily": F_HEAD, "fontStyle": "Bold", "size": round(m * 0.048, 1), "fill": "Album Ink",
                               "leading": {"kind": "points", "value": round(m * 0.056, 1)}}),
            ("Album Caption", {"fontFamily": F_BODY, "fontStyle": "Italic", "size": round(m * 0.0155, 1), "fill": "Album Muted"}),
            ("Album Title", {"fontFamily": F_HEAD, "fontStyle": "Bold", "size": round(m * 0.072, 1), "fill": "Album White",
                             "leading": {"kind": "points", "value": round(m * 0.08, 1)}}),
            ("Album Cover Line", {"fontFamily": F_BODY, "fontStyle": "Semibold", "size": round(m * 0.026, 1), "fill": "Album White"}),
            ("Album Small", {"fontFamily": F_BODY, "size": round(m * 0.016, 1), "fill": "Album Muted",
                             "leading": {"kind": "points", "value": round(m * 0.024, 1)}})):
        L.add("style.paragraph.create", {"name": name, "chars": chars})


def _fit(ar: float, w: float, h: float) -> tuple[float, float]:
    return (w, w / ar) if w / ar <= h else (h * ar, h)


def _arrange(photos: list[Photo], bw: float, bh: float, cap_h: float, gap: float) -> list[tuple[float, float, float, float]]:
    """Egy oldalnyi (1–2) fotó a (bw × bh) tükörben → [(x, y, w, h)] a tükörhöz képest, képaláírás-sávval; a fotó a
    saját arányában (a keret a képhez igazodik, nincs vágás)."""
    ch = [cap_h if p.caption else 0 for p in photos]
    if len(photos) == 1:
        w, h = _fit(photos[0].ar, bw, bh - ch[0])
        return [((bw - w) / 2, (bh - h - ch[0]) / 2, w, h)]
    a, b = photos
    options = []
    sh = (bh - gap - ch[0] - ch[1]) / 2                   # egymás alatt
    if sh > 0:
        (wa, ha), (wb, hb) = _fit(a.ar, bw, sh), _fit(b.ar, bw, sh)
        y0 = (bh - (ha + hb + gap + ch[0] + ch[1])) / 2
        options.append((min(wa * ha, wb * hb), [((bw - wa) / 2, y0, wa, ha), ((bw - wb) / 2, y0 + ha + ch[0] + gap, wb, hb)]))
    sw, row_h = (bw - gap) / 2, bh - max(ch)              # egymás mellett, alul egy vonalban
    (wa, ha), (wb, hb) = _fit(a.ar, sw, row_h), _fit(b.ar, sw, row_h)
    top = (bh - max(ha, hb) - max(ch)) / 2
    base = top + max(ha, hb)
    options.append((min(wa * ha, wb * hb), [((sw - wa) / 2, base - ha, wa, ha), (sw + gap + (sw - wb) / 2, base - hb, wb, hb)]))
    return max(options, key=lambda o: o[0])[1]


def _pack(photos: list[Photo], bw: float, bh: float, cap_h: float, gap: float) -> list[list[Photo]]:
    """Oldalakra osztás: két fotó egy oldalra, ha így is legalább ~42%-nyi helyet kapnak ahhoz képest, mintha egyedül
    állnának (két fekvő egymás alatt, két álló egymás mellett); különben egy fotó egy oldal."""
    pages, i = [], 0
    while i < len(photos):
        if i + 1 < len(photos):
            pair = _arrange(photos[i:i + 2], bw, bh, cap_h, gap)
            single = _arrange(photos[i:i + 1], bw, bh, cap_h, gap)[0]
            if min(w * h for _, _, w, h in pair) >= 0.42 * single[2] * single[3]:
                pages.append(photos[i:i + 2]); i += 2
                continue
        pages.append(photos[i:i + 1]); i += 1
    return pages


async def _run(L_cmds: list[tuple[str, Optional[dict]]], tail: list[str]) -> list[dict[str, Any]]:
    from app.tools import crafts
    args = ["run"]
    for c, p in L_cmds:
        args += ["--cmd", f"{c}={json.dumps(p, ensure_ascii=False)}" if p else c]
    out = await crafts.run_cli(crafts.office_cli("layout"), "designcraft-cli", *args, *tail)
    return crafts._json_lines(out)


async def _measure(L: Layout, bodies: list[tuple[float, float, str]], tmpdir: Path) -> list[int]:
    """MÉRŐFUTÁS: hány új oldalt ad hozzá az autoflow az egyes fejezetszövegekhez — ugyanaz a keretméret és betű,
    mint a valódi albumban (egy futás, fejezetenként egy-egy dokumentum)."""
    if not bodies:
        return []
    style_cmds = [(c, p) for c, p in L.cmds if c in ("swatch.create", "style.paragraph.edit")]
    cmds: list[tuple[str, Optional[dict]]] = []
    bw = L.w - L.mg["inside"] - L.mg["outside"]
    for k, (y0, y1, text) in enumerate(bodies):
        f = tmpdir / f"m{k}.txt"
        f.write_text(text, "utf-8")
        cmds += [("file.new", {**L.cmds[0][1], "facingPages": False})] + style_cmds + [
            ("frame.create", {"spread": 0, "rect": [L.mg["inside"], round(y0, 2), round(L.mg["inside"] + bw, 2), round(y1, 2)], "content": "text"}),
            ("file.place", {"path": str(f), "autoflow": True})]
    added = [int(r.get("pagesAdded") or 0) for r in await _run(cmds, []) if "pagesAdded" in r]
    if len(added) != len(bodies):
        raise RuntimeError(f"designcraft-cli: the text measuring run returned {len(added)} of {len(bodies)} results")
    return added


async def make(photos: list[Any], *, manuscript_file_id: Optional[str] = None, title: Optional[str] = None,
               subtitle: Optional[str] = None, author: Optional[str] = None, size: Optional[str] = None,
               captions: Optional[list[Optional[str]]] = None, auto_captions: bool = False, cover: Any = None,
               theme: Optional[dict] = None, brand: Optional[str] = None) -> dict[str, Any]:
    """Fotóalbum / kiadvány → {files: [.designcraft, nyomdai PDF, képernyős PDF], file_id (.designcraft), pages, check}."""
    from app.storage import display_label, output_path, public_url, write_meta
    from app.storage.files import new_file_id
    from app.tools import design
    from app.tools.crafts import existing

    photos = [p for p in (photos or []) if p]
    if not photos:
        raise ValueError("Add the photos for the album.")
    if len(photos) > MAX_PHOTOS:
        raise ValueError(f"At most {MAX_PHOTOS} photos in one album.")
    W, H = SIZES.get(size or "A4", SIZES["A4"])
    t = design._theme({}, theme, brand)
    chapters = chapters_from(existing(manuscript_file_id)) if manuscript_file_id else []
    if manuscript_file_id and not chapters:
        raise ValueError("The manuscript has no text — add a .docx, .md or .txt with your chapters.")
    title = ((title or "").strip() or (chapters[0][0] if len(chapters) == 1 and chapters[0][0] else "")
             or (Path(display_label(manuscript_file_id)).stem if manuscript_file_id else "") or "Photo album")
    photo_list = await _load_photos(photos, list(captions or []))
    cover_photo = (await _load_photos([cover], [""]))[0] if cover else None
    auto_n = 0
    try:
        if auto_captions:
            auto_n = await _auto_captions(photo_list, " ".join(b for _, b in chapters)[:400] or title)
        L = Layout(W, H, title)
        _styles(L, t)
        m = min(W, H)
        cap_h, gap, head_h = round(m * 0.034, 1), round(m * 0.03, 1), round(m * 0.19, 1)
        bw, bh = W - L.mg["inside"] - L.mg["outside"], H - L.mg["top"] - L.mg["bottom"]
        placed: list[tuple[Photo, float, float]] = []      # (fotó, keret-szélesség, -magasság pt-ben)

        # 1. oldal: borító — kifutó fotó, alul áttetsző sötét sáv a címmel
        cv = cover_photo or photo_list[0]
        L.place(0, [-BLEED, -BLEED, W + BLEED, H + BLEED], cv.path)
        if cover_photo:
            placed.append((cover_photo, W + 2 * BLEED, H + 2 * BLEED))
        L.block(0, [-BLEED, H * 0.64, W + BLEED, H + BLEED], "#000000", 0.5)
        mx = W * 0.09
        L.text(0, [mx, H * 0.69, W - mx, H * 0.87], title, "Album Title")
        line = " · ".join(x for x in ((subtitle or "").strip(), (author or "").strip()) if x)
        if line:
            L.text(0, [mx, H * 0.88, W - mx, H * 0.95], line, "Album Cover Line")

        # 2. oldal: belső borító — impresszum alul; 3. oldal: tartalomjegyzék (ha legalább két fejezet van)
        p = L.new_page()
        s, x0, y0, x1, y1 = L.box(p)
        imprint = "\n".join(x for x in (title, (subtitle or "").strip(), (author or "").strip()) if x)
        L.text(s, [x0, y1 - m * 0.12, x1, y1], imprint, "Album Small")
        toc_page = L.new_page() if sum(1 for h, _ in chapters if h) >= 2 else None

        # fotók sorrendben, a fejezetek között egyenletesen elosztva
        groups: list[tuple[str, str, list[Photo]]] = []
        if chapters:
            k, r = divmod(len(photo_list), len(chapters))
            i = 0
            for c, (h, b) in enumerate(chapters):
                take = k + (1 if c < r else 0)
                groups.append((h, b, photo_list[i:i + take])); i += take
        else:
            groups.append(("", "", photo_list))

        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            body_top = lambda h: L.mg["top"] + (head_h if h else 0)
            added = iter(await _measure(L, [(body_top(h), H - L.mg["bottom"], b) for h, b, _ in groups if b], tdir))
            heads: list[str] = []
            for ci, (h, b, ph) in enumerate(groups):
                if b:                                # fejezetnyitó: cím + akcentvonal + a szöveg, ami kell, új oldalakon folytatódik
                    p = L.new_page()
                    s, x0, y0, x1, y1 = L.box(p)
                    if h:
                        L.text(s, [x0, y0, x1, y0 + head_h * 0.8], h, "Album Heading")
                        L.block(s, [x0, y0 + head_h * 0.84, x0 + m * 0.08, y0 + head_h * 0.84 + 2.2], t["accent"])
                        heads.append(h)
                    f = tdir / f"c{ci}.txt"
                    f.write_text(b, "utf-8")
                    L.frame(s, [x0, body_top(h), x1, y1], content="text")
                    L.add("file.place", {"path": str(f), "autoflow": True})
                    L.pages += next(added)
                head_on_photos = bool(h) and not b
                for pi, page_photos in enumerate(_pack(ph, bw, bh - (head_h if head_on_photos else 0), cap_h, gap)):
                    p = L.new_page()
                    s, x0, y0, x1, y1 = L.box(p)
                    if head_on_photos and pi == 0:     # szöveg nélküli fejezet: a cím az első fotóoldal tetején
                        L.text(s, [x0, y0, x1, y0 + head_h * 0.8], h, "Album Heading")
                        heads.append(h)
                        y0 += head_h
                    for pho, (x, y, w, hh) in zip(page_photos, _arrange(page_photos, x1 - x0, y1 - y0, cap_h, gap)):
                        L.place(s, [x0 + x, y0 + y, x0 + x + w, y0 + y + hh], pho.path)
                        placed.append((pho, w, hh))
                        if pho.caption:
                            L.text(s, [x0 + x, y0 + y + hh + cap_h * 0.15, x0 + x + w, y0 + y + hh + cap_h], pho.caption, "Album Caption")
                if h and not b and not ph:              # cím szöveg és fotó nélkül: saját oldal
                    p = L.new_page()
                    s, x0, y0, x1, y1 = L.box(p)
                    L.text(s, [x0, y0, x1, y0 + head_h * 0.8], h, "Album Heading")
                    heads.append(h)
            if L.pages % 2:                          # oldalpáros nyomtatás: páros oldalszám (a hátsó belső borító üres)
                L.new_page()
            if toc_page:
                L.add("toc.generate", {"styles": ["Album Heading"], "title": "Contents", "page": toc_page})

            # kimenetek: szerkeszthető .designcraft + nyomdai PDF (kifutó + vágójel) + képernyős PDF
            ids = {"dc": new_file_id("designcraft"), "print": new_file_id("pdf"), "screen": new_file_id("pdf")}
            meta = {"title": title, "author": (author or "").strip()}
            lines = await _run(L.cmds + [("document.inspect", None)], [
                "--pdf-options", json.dumps({"bleed": True, "marks": {"crop": True}, **meta}), "--export", str(output_path(ids["print"])),
                "--pdf-options", json.dumps(meta), "--export", str(output_path(ids["screen"])),
                "--cmd", f"file.saveAs={json.dumps({'path': str(output_path(ids['dc']))})}"])
    finally:
        for pho in photo_list + ([cover_photo] if cover_photo else []):
            pho.path.unlink(missing_ok=True)
    for k in ids.values():
        if not output_path(k).exists():
            raise RuntimeError("designcraft-cli: the album was not written")

    # ellenőrzés: minden fotó a helyén, nyomtatási felbontás, tartalomjegyzék, kifutott szöveg, oldalszám
    inspect = next((r for r in lines if "pageCount" in r and "spreads" in r), {})
    toc = next((r for r in lines if "entries" in r and "story" in r), None)
    findings, verified = [], []
    n_placed = sum(1 for pho, _, _ in placed if pho in photo_list)
    (verified if n_placed == len(photo_list) else findings).append(
        f"{n_placed} of {len(photo_list)} photos are placed, each in its own proportions (nothing cropped).")
    soft = [f"{pho.name} ({int(min(pho.w / (fw / 72), pho.h / (fh / 72)))} ppi)" for pho, fw, fh in placed
            if min(pho.w / (fw / 72), pho.h / (fh / 72)) < MIN_PPI]
    if soft:
        findings.append(f"Will print soft (under {MIN_PPI} ppi at this size): " + "; ".join(soft[:8]))
    else:
        verified.append(f"Every photo prints at {MIN_PPI} ppi or more.")
    if toc_page:
        n_toc = int((toc or {}).get("entries") or 0)
        (verified if n_toc == len(heads) else findings).append(f"The contents page lists {n_toc} of {len(heads)} chapters.")
    overset = [st.get("id") for st in inspect.get("stories") or [] if st.get("overset")]
    if overset:
        findings.append(f"{len(overset)} text frame(s) have text that doesn't fit — check them in the layout editor.")
    pages = int(inspect.get("pageCount") or L.pages)
    (verified if pages == L.pages else findings).append(
        f"{pages} pages as facing pages; the print PDF has 3 mm bleed and crop marks." if pages == L.pages
        else f"{pages} pages, the plan had {L.pages} — check the text flow.")
    caps = sum(1 for pho in photo_list if pho.caption)
    (verified if caps == len(photo_list) else findings).append(
        f"{caps} of {len(photo_list)} photos have a caption" + (f" ({auto_n} written by AI — check them)." if auto_n else
        "." if caps == len(photo_list) else " — type the rest in the layout editor, or let AI write them (auto captions)."))
    check = {"status": "ok" if not findings else "needs_check", "findings": findings, "verified": verified}

    from app.routes.make import _new_group
    group, stem = _new_group(), title[:60]
    src = manuscript_file_id or photo_list[0].file_id
    labels = {"dc": f"{stem} (album).designcraft", "print": f"{stem} (album, print).pdf", "screen": f"{stem} (album).pdf"}
    files = []
    for k in ("dc", "print", "screen"):
        write_meta(ids[k], source_file_id=src, operation="album", label=labels[k],
                   extra={"group": group, **({"variant": k} if k != "dc" else {})})
        files.append({"file_id": ids[k], "url": public_url(ids[k]), "label": labels[k],
                      "size_kb": output_path(ids[k]).stat().st_size // 1024})
    return {"files": files, "file_id": ids["dc"], "print_pdf": ids["print"], "pdf": ids["screen"], "pages": pages,
            "photos": len(photo_list), "chapters": len(heads), "check": check, "template": "album",
            "credits": sorted({pho.credit for pho in photo_list if pho.credit})}
