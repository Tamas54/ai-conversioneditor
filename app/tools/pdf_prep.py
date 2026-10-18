"""PDF előkészítése szerkeszthető formátumba (Word) alakítás előtt.

A lapszámozási „bútorzat” — vízjel, fejléc, lábléc, oldalszám — a PDF-szabványban
`/Artifact <</Type /Pagination …>> BDC … EMC` jelölt tartalom: NEM a dokumentum szövege. A Word-exportálók
(PdfCraft, pdf2docx) mégis törzsszövegként hozzák át: a ferde „DRAFT” óriás címsor lesz a bekezdések között,
az „1 of 3” külön sor minden oldalon (UI-próba 10-18). Ezt a jelölt tartalmat vesszük ki egy másolatból.
Ami nincs így jelölve, ahhoz nem nyúlunk (nem találgatunk).
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("aice.pdf_prep")

# a /Subtype a gyakorlatban nem megbízható (a PdfCraft a láblécet is /Header-nek jelöli) → fejléc/lábléc együtt
_KINDS = {"watermark": "the watermark", "header": "the page numbers and headers",
          "footer": "the page numbers and headers"}


def strip_pagination(src: Path, dst: Path) -> list[str]:
    """`src` lapszámozási artifactjai nélkül `dst`-be. Vissza: mi maradt ki (pl. ["watermark", "header"]);
    üres lista = nem volt ilyen, ekkor `dst` nem készül."""
    import pikepdf
    kinds: set[str] = set()
    with pikepdf.open(src) as pdf:
        changed = False
        for page in pdf.pages:
            ops = pikepdf.parse_content_stream(page)
            out, depth = [], 0
            for operands, op in ops:
                name = str(op)
                if depth:                        # egy kihagyott blokk belsejében (beágyazott BDC/BMC … EMC is)
                    depth += name in ("BDC", "BMC")
                    depth -= name == "EMC"
                    continue
                if name == "BDC" and len(operands) == 2 and operands[0] == pikepdf.Name.Artifact:
                    props = operands[1]
                    if isinstance(props, pikepdf.Dictionary) and props.get("/Type") == pikepdf.Name.Pagination:
                        kinds.add(str(props.get("/Subtype", pikepdf.Name.Pagination))[1:].lower())
                        depth = 1
                        continue
                out.append((operands, op))
            if len(out) != len(ops):
                page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(out))
                changed = True
        if changed:
            pdf.save(dst)
    return sorted(kinds)


def left_out_note(kinds: list[str]) -> str:
    """Felhasználónak: mi maradt ki a Word-fájlból (látható kizárás)."""
    if not kinds:
        return ""
    parts = list(dict.fromkeys(_KINDS.get(k, "the page numbers and headers") for k in sorted(kinds, key=lambda k: k != "watermark")))
    what = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"Left out {what} — they're PDF page furniture, not part of the text"


def json_objects(cli_out: str) -> list:
    """A `pdfcraft-cli run --script` kimenete egymás után írt JSON-objektumok (lépésenként egy)."""
    import json
    dec, i, out = json.JSONDecoder(), 0, []
    while True:
        i = cli_out.find("{", i)
        if i < 0:
            return out
        try:
            obj, i = dec.raw_decode(cli_out, i)
            out.append(obj)
        except ValueError:
            i += 1


def drop_table_edges(path: Path, fields: list[dict], tol: float = 1.5) -> tuple[list[dict], list[dict]]:
    """A PdfCraft mezőfelismerése a vonalas táblázat FELSŐ keretét is „kitöltendő vonalnak” nézi, és fölé tesz
    mezőt — a táblázaton kívülre (UI-próba 10-18: jelentés táblázata fölött két üres mező). Egy kitöltendő cella
    mezője mindig a cellán BELÜL van (az alsó éléből a függőlegesek felfelé indulnak). Kiszűrjük azt a jelöltet,
    amelynek alsó éle olyan vízszintes vonalon ül, amelynek mindkét végéből függőleges indul LEFELÉ, felfelé
    egyik sem. → (megtartott, kiszűrt); rect = PdfCraft-féle bal-alsó origó."""
    import pymupdf
    kept, dropped = [], []
    with pymupdf.open(path) as doc:
        verticals: dict[int, list[tuple[float, float, float]]] = {}
        for pno in {f.get("page", 1) for f in fields}:
            vs = []
            for dr in doc[pno - 1].get_drawings():
                for it in dr["items"]:
                    if it[0] == "l" and abs(it[1].x - it[2].x) <= tol and abs(it[1].y - it[2].y) > tol:
                        vs.append((it[1].x, min(it[1].y, it[2].y), max(it[1].y, it[2].y)))
                    elif it[0] == "re":
                        r = it[1]
                        vs += [(r.x0, r.y0, r.y1), (r.x1, r.y0, r.y1)]
            verticals[pno] = vs
        for f in fields:
            pno = f.get("page", 1)
            h = doc[pno - 1].rect.height
            x0, y0, x1, _ = f["rect"]
            edge = h - y0                              # a mező alsó éle, felülről mérve
            near = lambda x: [v for v in verticals[pno] if abs(v[0] - x) <= tol]
            down = lambda x: any(abs(top - edge) <= tol + 1 for _, top, _ in near(x))       # innen indul lefelé
            up = lambda x: any(abs(bot - edge) <= tol + 1 for _, _, bot in near(x))         # ide érkezik felülről
            (dropped if down(x0) and down(x1) and not (up(x0) or up(x1)) else kept).append(f)
    return kept, dropped


def retitle_outline(path: Path, titles: dict[str, str]) -> int:
    """Egyesítés után a felső szintű könyvjelzők a fájlok SAJÁT nevét kapják: a PdfCraft doc_combine a fájlnevet
    teszi címnek, ami nálunk a belső azonosító („13ac8a0b…” — UI-próba 10-18). titles: régi cím → új cím."""
    import pikepdf
    n = 0
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        with pdf.open_outline() as ol:
            for item in ol.root:
                if item.title in titles:
                    item.title = titles[item.title]
                    n += 1
        if n:
            pdf.save(path)
    return n


# textlang-címke → Tesseract-nyelvkód (a Dockerfile tesseract-ocr-* csomagjai)
_TESS = {"en-US": "eng", "hu-HU": "hun", "de-DE": "deu", "fr-FR": "fra", "es-ES": "spa", "it-IT": "ita", "pt-PT": "por",
         "nl-NL": "nld", "pl-PL": "pol", "cs-CZ": "ces", "sk-SK": "slk", "ro-RO": "ron"}


async def guess_ocr_language(pdf: Path) -> str:
    """A szkennelt PDF nyelve az első oldal gyors angol olvasatából (a funkciószavak és a betűk az angol modellel is
    átjönnek) → Tesseract-kód, pl. "hun+eng". Kétesnél "eng". (UI-próba 10-18: a PdfCraft OCR-ja csak angol — egy
    magyar levélben minden ékezetes betű „?” lett, a „számla” keresésre sem volt találat.)"""
    import asyncio
    import tempfile
    from app.tools import textlang
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "p"
        proc = await asyncio.create_subprocess_exec("pdftoppm", "-r", "150", "-png", "-f", "1", "-l", "1", "-singlefile",
                                                    str(pdf), str(png), stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        if not png.with_suffix(".png").exists():
            return "eng"
        proc = await asyncio.create_subprocess_exec("tesseract", str(png.with_suffix(".png")), "-", "-l", "eng",
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
    lang = _TESS.get(textlang.detect(out.decode("utf-8", "replace"), min_hits=5) or "en-US", "eng")
    return lang if lang == "eng" else f"{lang}+eng"
