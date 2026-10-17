"""Történeti forrás: a kétes olvasat jelölve, sosem csendben kitalálva (a ChatGPT-értékelés 10-17). A modell
[[?olvasat|másik]] / [?] jelölését minden kimenet a maga módján viszi tovább — itt modell nélkül, kész OCR-szöveggel."""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

PAGES = ["# Első fejezet\n\nA követ [[?Kolozsvár|Kolozsvan]] felől érkezett, és a [?] kapunál várt.",
         "A második oldalon a [[?bíró]] ítéletet hirdetett."]


def test_numbering_and_renderings():
    from app.tools import digitize as dz
    pages, items = dz.number_uncertain(PAGES)
    assert [(i["page"], i["kind"], i["reading"]) for i in items] == [(1, "uncertain", "Kolozsvár"), (1, "illegible", ""),
                                                                     (2, "uncertain", "bíró")]
    assert items[0]["also"] == ["Kolozsvan"] and "#" not in items[0]["context"] and "Kolozsvár felől" in items[0]["context"]
    md = dz.assemble(pages)
    assert "[Kolozsvár? / Kolozsvan]" in dz._render(md, items, "editorial") and "[bíró?]" in dz._render(md, items, "editorial")
    assert dz.best_reading(md, items).count("[") == 0                    # a szövegréteg: legjobb olvasat, jelölés nélkül
    sent = dz._render(md, items, "sentinel")
    assert sent.count(dz._OPEN) == 3 and "Kolozsvan" not in sent


def test_word_file_gets_highlights_and_comments(tmp_path):
    pypandoc = pytest.importorskip("pypandoc")
    from app.tools import digitize as dz
    pages, items = dz.number_uncertain(PAGES)
    md = tmp_path / "b.md"
    md.write_text(dz.to_pandoc(dz._render(dz.assemble(pages), items, "sentinel")), "utf-8")
    docx = tmp_path / "b.docx"
    try:
        pypandoc.convert_file(str(md), "docx", outputfile=str(docx))
    except OSError:
        pytest.skip("pandoc not installed")
    assert dz.mark_docx(docx, items, True) == 3
    with zipfile.ZipFile(docx) as z:
        doc, com = z.read("word/document.xml").decode(), z.read("word/comments.xml").decode()
    assert dz._OPEN not in doc and dz._CLOSE not in doc
    assert re.findall(r'w:highlight w:val="(\w+)"', doc) == ["yellow", "red", "yellow"]
    assert "also possible: “Kolozsvan”" in com and "page 2" in com


def test_review_pdf_puts_page_and_transcript_side_by_side(tmp_path):
    fitz = pytest.importorskip("fitz")
    from PIL import Image
    from app.tools import digitize as dz
    pages, items = dz.number_uncertain(PAGES)
    imgs = []
    for n in range(2):
        p = tmp_path / f"f{n:03d}.jpg"
        Image.new("RGB", (600, 850), "white").save(p)
        imgs.append(p)
    out = tmp_path / "r.pdf"
    dz._review_pdf(imgs, pages, items, out)
    doc = fitz.open(str(out))
    assert len(doc) == 2 and len(doc[0].get_images()) == 1
    notes = [a.info.get("content") for a in doc[0].annots()]
    assert any("Kolozsvan" in n for n in notes) and any("Illegible" in n for n in notes)
    assert "Kolozsvár" in doc[0].get_text() and "[[" not in doc[0].get_text()


def test_partial_and_empty_marks_from_the_real_model():
    """Valódi DeepSeek-válaszok (10-17, foltos tesztlap): csak a kétes betűket jelölte, vagy üres jelölést adott."""
    from app.tools import digitize as dz
    _, items = dz.number_uncertain(["[[FIGURE other 1 2 3 4 | folt]]\n\nA mint Koloz[[?svárra|svárra]] a szekér, és Koloz[[?]] felől."])
    assert items[0]["word"] == "Kolozsvárra" and items[0]["reading"] == "svárra" and items[0]["also"] == []
    assert items[1]["kind"] == "illegible" and "FIGURE" not in items[0]["context"]
