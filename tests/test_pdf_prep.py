"""PDF → Word: a lapszámozási „bútorzat” (vízjel, fejléc, oldalszám) nem lesz törzsszöveg.

UI-próba (10-18): vízjelezett, oldalszámozott PDF Word-be alakítva → a ferde „BIZALMAS ÁTTEKINTÉS” óriás
Heading 1 lett a bekezdések között, az „1 of 1” külön sor.
"""
from __future__ import annotations

import pikepdf

from app.tools.pdf_prep import left_out_note, strip_pagination

_BODY = b"BT /F1 12 Tf 72 700 Td (Body text) Tj ET"
_MARK = (b"q /Artifact <</Type /Pagination /Subtype /Watermark>> BDC "
         b"BT /F1 48 Tf 0.7 0.7 -0.7 0.7 150 300 Tm (DRAFT) Tj ET EMC Q")
_FOOT = b"/Artifact <</Type /Pagination /Subtype /Footer>> BDC BT /F1 8 Tf 300 30 Td (1 of 1) Tj ET EMC"
_OTHER = b"/Artifact <</Type /Layout>> BDC 0 0 1 rg 10 10 5 5 re f EMC"       # nem lapszámozás: marad


def _pdf(path, content: bytes):
    pdf = pikepdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                                BaseFont=pikepdf.Name.Helvetica))
    page = pikepdf.Page(pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=[0, 0, 612, 792],
                                           Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)),
                                           Contents=pdf.make_stream(content)))
    pdf.pages.append(page)
    pdf.save(path)


def _text(path) -> bytes:
    with pikepdf.open(path) as pdf:
        return b"".join(pikepdf.unparse_content_stream(pikepdf.parse_content_stream(p)) for p in pdf.pages)


def test_strips_only_pagination_artifacts(tmp_path):
    src, dst = tmp_path / "in.pdf", tmp_path / "out.pdf"
    _pdf(src, b"\n".join([_BODY, _MARK, _FOOT, _OTHER]))
    assert strip_pagination(src, dst) == ["footer", "watermark"]
    out = _text(dst)
    assert b"Body text" in out and b"DRAFT" not in out and b"1 of 1" not in out
    assert b"/Layout" in out                                   # más artifactot nem bántunk
    lines = out.split(b"\n")
    assert lines.count(b"q") == lines.count(b"Q") == 1        # a blokkot közrefogó q/Q egyensúlya megmarad


def test_untouched_pdf_is_not_rewritten(tmp_path):
    src, dst = tmp_path / "in.pdf", tmp_path / "out.pdf"
    _pdf(src, _BODY)
    assert strip_pagination(src, dst) == [] and not dst.exists()


def test_note_says_what_was_left_out():
    assert left_out_note([]) == ""
    note = left_out_note(["header", "watermark"])
    assert note.startswith("Left out the watermark and the page numbers")


def test_merged_bookmarks_get_file_names(tmp_path):
    """UI-próba 10-18: egyesítés után a könyvjelzők a belső azonosítók voltak („13ac8a0b…”)."""
    from app.tools.pdf_prep import retitle_outline
    p = tmp_path / "merged.pdf"
    _pdf(p, _BODY)
    with pikepdf.open(p, allow_overwriting_input=True) as pdf:
        with pdf.open_outline() as ol:
            ol.root.extend([pikepdf.OutlineItem("13ac8a0b03bb4e0cb214878ade70463f", 0),
                            pikepdf.OutlineItem("Saját fejezet", 0)])
        pdf.save(p)
    assert retitle_outline(p, {"13ac8a0b03bb4e0cb214878ade70463f": "urlap"}) == 1
    with pikepdf.open(p) as pdf, pdf.open_outline() as ol:
        assert [i.title for i in ol.root] == ["urlap", "Saját fejezet"]


def test_ocr_language_is_the_pages_own(tmp_path):
    """UI-próba 10-18: a „Make searchable” a PdfCraft angol modelljével minden ékezetes betűt „?”-re olvasott."""
    import asyncio
    import shutil
    import pytest
    from PIL import Image, ImageDraw, ImageFont
    from app.tools.pdf_prep import guess_ocr_language
    if not (shutil.which("tesseract") and shutil.which("pdftoppm")):
        pytest.skip("tesseract / pdftoppm needed")
    font = next((f for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
                             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf") if __import__("os").path.exists(f)), None)
    if not font:
        pytest.skip("no font")
    im = Image.new("L", (1240, 900), 255)
    d = ImageDraw.Draw(im)
    lines = ["Tisztelt Ügyfelünk! Értesítjük, hogy a számla", "összege 48 500 Ft, és a fizetési határidő", "november 2. Kérjük, hogy a díjat időben",
             "fizesse be, mert a késés után kamat jár.", "Az áruk listája nem változott, de a szállítás", "ideje egy héttel később lesz."]
    for k, line in enumerate(lines):
        d.text((60, 60 + 70 * k), line, fill=0, font=ImageFont.truetype(font, 40))
    pdf = tmp_path / "scan.pdf"
    im.save(pdf, "PDF", resolution=150)
    assert asyncio.run(guess_ocr_language(pdf)) == "hun+eng"
