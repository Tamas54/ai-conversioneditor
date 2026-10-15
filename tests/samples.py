"""Mintafájlok a tesztekhez — minden futáskor generálva (a repóban nincs bináris fixture).

Minden szöveges minta ékezetes (ő, ű), hogy a kimenetekben ellenőrizni lehessen az épségüket.
"""
from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

HU = "Árvíztűrő tükörfúrógép — őszi űrlap"


class Samples:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _p(self, name: str) -> Path:
        return self.root / name

    # ---------- dokumentumok ----------

    def docx(self, name: str = "level.docx", text: str = HU) -> Path:
        from docx import Document
        d = Document()
        d.add_heading("Címsor: " + text, 1)
        d.add_paragraph("Első bekezdés: " + text)
        d.add_paragraph("Második bekezdés, ő és ű betűkkel.")
        p = self._p(name)
        d.save(p)
        return p

    def txt(self, name: str = "jegyzet.txt", text: str = HU) -> Path:
        p = self._p(name)
        p.write_text(text + "\nMásodik sor ő ű.\n", "utf-8")
        return p

    def md(self, name: str = "jegyzet.md") -> Path:
        p = self._p(name)
        p.write_text(f"# {HU}\n\nBekezdés **félkövér** ő ű.\n\n- egy\n- kettő\n", "utf-8")
        return p

    def html(self, name: str = "oldal.html") -> Path:
        p = self._p(name)
        p.write_text(f"<!doctype html><html lang=hu><meta charset=utf-8><title>{HU}</title><body><h1>{HU}</h1>"
                     "<p>Bekezdés ő ű.</p></body></html>", "utf-8")
        return p

    def pdf(self, name: str = "dok.pdf", pages: int = 2, text: str = HU) -> Path:
        import pymupdf as fitz
        d = fitz.open()
        font = _font()
        for i in range(pages):
            pg = d.new_page(width=595, height=842)
            if font:
                pg.insert_font(fontname="F0", fontfile=font)
            pg.insert_text((72, 100), f"{i + 1}. oldal: {text}", fontsize=14, fontname="F0" if font else "helv")
            pg.draw_rect(fitz.Rect(72, 150, 300, 300), color=(0.2, 0.4, 0.8), fill=(0.8, 0.9, 1))
        p = self._p(name)
        d.save(p)
        d.close()
        return p

    def fillable_pdf(self, name: str = "urlap.pdf") -> Path:
        """Kitölthető PDF: szövegmező (név, cím), jelölőnégyzet (hozzájárul), aláírásmező helye."""
        import pymupdf as fitz
        d = fitz.open()
        pg = d.new_page(width=595, height=842)
        font = _font()
        if font:
            pg.insert_font(fontname="F0", fontfile=font)
        fn = "F0" if font else "helv"
        pg.insert_text((72, 90), "Jelentkezési lap — őszi tábor", fontsize=16, fontname=fn)
        for i, (label, fname) in enumerate((("Név:", "nev"), ("Lakcím:", "lakcim"), ("Születési dátum:", "szuletesi_datum"))):
            y = 140 + i * 40
            pg.insert_text((72, y + 14), label, fontsize=11, fontname=fn)
            w = fitz.Widget()
            w.field_name, w.field_type = fname, fitz.PDF_WIDGET_TYPE_TEXT
            w.rect = fitz.Rect(200, y, 500, y + 20)
            pg.add_widget(w)
        pg.insert_text((72, 274), "Hozzájárulok az adatkezeléshez", fontsize=11, fontname=fn)
        cb = fitz.Widget()
        cb.field_name, cb.field_type = "hozzajarul", fitz.PDF_WIDGET_TYPE_CHECKBOX
        cb.rect = fitz.Rect(300, 260, 316, 276)
        pg.add_widget(cb)
        pg.insert_text((72, 340), "Aláírás:", fontsize=11, fontname=fn)
        pg.draw_line((140, 345), (400, 345))
        p = self._p(name)
        d.save(p)
        d.close()
        return p

    def flat_form_pdf(self, name: str = "lapos_urlap.pdf") -> Path:
        """Lapos (mező nélküli) nyomtatvány: címkék + aláhúzás-vonalak + jelölőnégyzet-keret."""
        import pymupdf as fitz
        d = fitz.open()
        pg = d.new_page(width=595, height=842)
        font = _font()
        if font:
            pg.insert_font(fontname="F0", fontfile=font)
        fn = "F0" if font else "helv"
        pg.insert_text((72, 90), "Adatlap", fontsize=16, fontname=fn)
        for i, label in enumerate(("Név:", "Lakcím:", "Adóazonosító jel:", "Dátum:")):
            y = 150 + i * 40
            pg.insert_text((72, y), label, fontsize=11, fontname=fn)
            pg.draw_line((200, y + 2), (500, y + 2), width=0.8)
        pg.insert_text((72, 330), "Hozzájárulok", fontsize=11, fontname=fn)
        pg.draw_rect(fitz.Rect(200, 318, 212, 330), width=0.8)
        pg.insert_text((72, 380), "Aláírás:", fontsize=11, fontname=fn)
        pg.draw_line((200, 382), (420, 382), width=0.8)
        p = self._p(name)
        d.save(p)
        d.close()
        return p

    def form_docx(self, name: str = "urlap.docx") -> Path:
        from docx import Document
        d = Document()
        d.add_heading("Meghatalmazás", 1)
        d.add_paragraph("Név: ____________________")
        d.add_paragraph("Születési dátum: __________")
        d.add_paragraph("Adóazonosító jel: [          ]")
        t = d.add_table(rows=2, cols=2)
        t.cell(0, 0).text, t.cell(1, 0).text = "Lakcím", "Telefonszám"
        d.add_paragraph("Hozzájárulok: ☐")
        d.add_paragraph("Aláírás: ____________________")
        p = self._p(name)
        d.save(p)
        return p

    def form_xlsx(self, name: str = "urlap.xlsx") -> Path:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Adatlap"
        rows = [("Név", None), ("Lakcím", None), ("Születési dátum", None), ("IBAN", None)]
        for i, (k, v) in enumerate(rows, 1):
            ws.cell(i, 1, k)
        p = self._p(name)
        wb.save(p)
        return p

    def csv_rows(self, name: str = "adatok.csv") -> Path:
        p = self._p(name)
        p.write_text("nev;lakcim;hozzajarul\nKőváry Ödön;Győr, Fő utca 1.;igen\nŰrmös Éva;Pécs, Ős tér 2.;nem\n", "utf-8")
        return p

    def xlsx(self, name: str = "tabla.xlsx") -> Path:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["Név", "Összeg"])
        ws.append(["Őrs", 12])
        ws.append(["Űr", 30])
        p = self._p(name)
        wb.save(p)
        return p

    def pptx(self, name: str = "dia.pptx") -> Path:
        from pptx import Presentation
        prs = Presentation()
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = HU
        s.placeholders[1].text = "Pont ő ű"
        p = self._p(name)
        prs.save(p)
        return p

    # ---------- képek ----------

    def png(self, name: str = "kep.png", size=(320, 200), alpha: bool = False) -> Path:
        from PIL import Image, ImageDraw
        im = Image.new("RGBA" if alpha else "RGB", size, (0, 0, 0, 0) if alpha else (240, 240, 230))
        dr = ImageDraw.Draw(im)
        dr.rectangle((20, 20, size[0] - 20, size[1] - 20), outline=(200, 30, 30), width=6)
        dr.ellipse((60, 40, 180, 160), fill=(30, 120, 200))
        p = self._p(name)
        im.save(p)
        return p

    def jpg(self, name: str = "foto.jpg", size=(640, 480)) -> Path:
        from PIL import Image
        im = Image.open(self.png("tmp_src.png", size)).convert("RGB")
        p = self._p(name)
        exif = Image.Exif()
        exif[0x010F] = "TesztGyártó"   # Make
        exif[0x0110] = "Modell ő"       # Model
        im.save(p, "JPEG", quality=92, exif=exif.tobytes())
        return p

    def signature_photo(self, name: str = "alairas.jpg") -> Path:
        """Papírra írt aláírás fotója: halvány, egyenetlen háttér + sötét tollvonás."""
        from PIL import Image, ImageDraw, ImageFilter
        import math
        im = Image.new("RGB", (600, 260), (236, 232, 222))
        dr = ImageDraw.Draw(im)
        for x in range(600):                    # árnyékos papír
            dr.line([(x, 0), (x, 260)], fill=(236 - x // 30, 232 - x // 30, 222 - x // 40))
        pts = [(60 + t * 4.6, 140 + 40 * math.sin(t / 7.0) - 20 * math.cos(t / 3.0)) for t in range(100)]
        dr.line(pts, fill=(20, 25, 70), width=5)
        dr.line([(80, 200), (520, 190)], fill=(25, 30, 80), width=3)
        im = im.filter(ImageFilter.GaussianBlur(0.6))
        p = self._p(name)
        im.save(p, "JPEG", quality=90)
        return p

    def svg(self, name: str = "abra.svg") -> Path:
        p = self._p(name)
        p.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="120"><rect x="10" y="10" width="180" '
                     'height="100" fill="#3a7"/><text x="20" y="70" font-size="20">ő ű</text></svg>', "utf-8")
        return p

    # ---------- média ----------

    def wav(self, name: str = "hang.wav", seconds: float = 1.0, rate: int = 16000) -> Path:
        import math
        import struct
        p = self._p(name)
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                                   for i in range(int(seconds * rate))))
        return p

    def mp4(self, name: str = "video.mp4", seconds: int = 2) -> Path:
        p = self._p(name)
        _ff("-f", "lavfi", "-i", f"testsrc=size=320x240:rate=15:duration={seconds}", "-f", "lavfi", "-i",
            f"sine=frequency=440:duration={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(p))
        return p

    # ---------- archívum, font, CAD ----------

    def zip(self, name: str = "csomag.zip") -> Path:
        import zipfile
        p = self._p(name)
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("ő/első.txt", "első ő")
            z.writestr("második.txt", "második ű")
        return p

    def ttf(self, name: str = "betu.ttf") -> Path:
        """Egy rendszer-TTF másolata (DejaVu / Liberation), ha van."""
        for cand in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                     "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"):
            if Path(cand).exists():
                p = self._p(name)
                shutil.copy(cand, p)
                return p
        import pytest
        pytest.skip("no TTF font on this machine")

    def dxf(self, name: str = "rajz.dxf") -> Path:
        import ezdxf
        doc = ezdxf.new()
        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0))
        msp.add_circle((50, 50), 25)
        msp.add_text("ő ű", dxfattribs={"height": 5}).set_placement((10, 80))
        p = self._p(name)
        doc.saveas(p)
        return p


def _font() -> str | None:
    for cand in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"):
        if Path(cand).exists():
            return cand
    return None


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)
