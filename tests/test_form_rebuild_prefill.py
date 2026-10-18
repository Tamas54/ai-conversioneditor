"""Kitöltött nyomtatvány újraszedése: a válaszok mezőkbe kerülnek előtöltve, az aláírás soha; a hosszú bekezdés tördelve.

UI-próba (10-18, országgyűlési vagyonnyilatkozat): a gépelt válaszok (név, foglalkozás, X a rovatban) fix szövegként
égtek be; a kézi aláírásból „Pintér Bence” lett a mezőben; a 3 soros (F) pont egy sorba zsugorodva lelógott a lapról.
"""
from __future__ import annotations

import pymupdf
from PIL import Image

from app.tools.forms import build_pdf

LONG = ("(F) „Nyilatkozom pénzbeli vagy természetbeni juttatásról, amelyet a tiszteletdíjamon és a megbízatásom "
        "ellátásával összefüggésben biztosított juttatáson felül munkám ellátásához vagy azzal szoros összefüggésben "
        "harmadik féltől kapok, akinek kilétét közlöm:”")


def test_prefill_without_signature_and_wrapped_paragraph(tmp_path):
    img = tmp_path / "p.jpg"
    Image.new("RGB", (1240, 1754), "white").save(img)
    lay = {"items": [{"type": "text", "text": LONG, "box": [90, 40, 910, 90], "size": "normal"},
                     {"type": "field", "kind": "signature", "name": "alairas", "label": "Aláírás", "box": [550, 500, 750, 520],
                      "value": "Pintér Bence"},
                     {"type": "field", "kind": "text", "name": "vezeteknev", "label": "Vezetéknév", "box": [100, 300, 400, 320],
                      "value": "Teszt"},
                     {"type": "field", "kind": "checkbox", "name": "kategoria_4", "label": "4", "box": [100, 350, 120, 370],
                      "value": True}]}
    pre: dict = {}
    names = build_pdf([lay], [img], tmp_path / "o.pdf", pre)
    assert set(names) == {"alairas", "vezeteknev", "kategoria_4"}
    assert pre == {"vezeteknev": "Teszt", "kategoria_4": True}
    with pymupdf.open(tmp_path / "o.pdf") as d:
        page = d[0]
        assert "akinek kilétét közlöm" in page.get_text().replace("\n", " ")
        block = next(b for b in page.get_text("dict")["blocks"] if b.get("lines"))
        assert len(block["lines"]) >= 2 and block["bbox"][2] <= page.rect.width - 10


def test_label_does_not_run_into_its_field(tmp_path):
    """UI-próba 10-18: a félkövér „Vezetéknév:” a mellette kezdődő mezőbe lógott (eltakarta a „dr.”-t)."""
    img = tmp_path / "p.jpg"
    Image.new("RGB", (1240, 1754), "white").save(img)
    lay = {"items": [{"type": "text", "text": "Vezetéknév:", "box": [100, 300, 160, 316], "bold": True, "size": "heading"},
                     {"type": "field", "kind": "text", "name": "vezeteknev", "label": "Vezetéknév", "box": [165, 300, 400, 316]}]}
    build_pdf([lay], [img], tmp_path / "o.pdf")
    with pymupdf.open(tmp_path / "o.pdf") as d:
        page = d[0]
        label = next(b for b in page.get_text("dict")["blocks"] if b.get("lines"))
        field = next(page.widgets())
        assert label["bbox"][2] <= field.rect.x0 + 1


def test_fields_fit_between_label_and_unit(tmp_path):
    """UI-próba 10-18: a „tartozás:” felirat vége és a mező utáni „Ft” a mező alá került."""
    img = tmp_path / "p.jpg"
    Image.new("RGB", (1240, 1754), "white").save(img)
    lay = {"items": [{"type": "text", "text": "Magánszemélyekkel szemben fennálló tartozás:", "box": [80, 300, 300, 316]},
                     {"type": "field", "kind": "text", "name": "tartozas", "label": "tartozás", "box": [280, 300, 620, 316]},
                     {"type": "text", "text": "Ft", "box": [600, 300, 640, 316]}]}
    build_pdf([lay], [img], tmp_path / "o.pdf")
    with pymupdf.open(tmp_path / "o.pdf") as d:
        page = d[0]
        field = next(page.widgets()).rect
        words = [pymupdf.Rect(w[:4]) for w in page.get_text("words")]
        assert not any(wr.intersects(field) and (wr & field).get_area() > 0.3 * wr.get_area() for wr in words)


def test_picture_items_and_blanks_known_up_front(tmp_path):
    """Regresszió (10-18): a képelem a sorszámával kerül a lapra; a felirat a CSAK később felsorolt mező elé is fér."""
    img = tmp_path / "p.jpg"
    Image.new("RGB", (1240, 1754), (240, 240, 240)).save(img)
    lay = {"items": [{"type": "picture", "box": [400, 50, 600, 150], "caption": "címer"},
                     {"type": "text", "text": "Magánszemélyekkel szemben fennálló tartozás:", "box": [80, 300, 300, 316]},
                     {"type": "field", "kind": "text", "name": "tartozas", "label": "tartozás", "box": [420, 300, 620, 316]}]}
    build_pdf([lay], [img], tmp_path / "o.pdf")
    with pymupdf.open(tmp_path / "o.pdf") as d:
        page = d[0]
        label = next(b for b in page.get_text("dict")["blocks"] if b.get("lines"))
        assert label["bbox"][2] <= next(page.widgets()).rect.x0 + 1
