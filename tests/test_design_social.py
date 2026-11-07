"""Dizájnsablonok: a social-kártya fotója és szövegei (11-07, egy honlap link-képe a Studióval).

Hiba volt: (1) egy 312×464-es álló portrét a social-sablon 3,9×-re fújt fel teljes háttérnek — elmosódott, a fej
levágva; (2) a handle-sor (telefonszám) 26,4 pt-os betűvel 31,5 pt-os dobozba került — a VectorCraft semmit nem rajzolt;
(3) a check csak annyit mondott: „No automatic check”. Most: a kép méretétől és arányától függ az elrendezés, a doboz
legalább egy sor, és a sablon saját checkje méri, hogy minden szöveg ott van-e és a fotó éles-e."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class _Img:
    path: Path


def _theme():
    from app.tools import design
    return design._theme({}, {"primary": "#2A5244", "accent": "#E2B55A"}, None)


def _social(tmp_path, size, img_px):
    from PIL import Image
    from app.tools import design
    W, H = design.size_for("social", size)
    d = design.Doc(W, H, "x")
    img = None
    if img_px:
        p = tmp_path / f"img_{img_px[0]}x{img_px[1]}.jpg"
        Image.new("RGB", img_px, (90, 120, 160)).save(p)
        img = _Img(p)
    design._social(d, {"title": "Dr. Vajda János", "subtitle": "Ügyfélbarát közös képviselet, elégedett lakók",
                       "handle": "+36 20 267 0877"}, _theme(), img)
    for p in d.tmp:
        p.unlink(missing_ok=True)
    return d


def _texts(d):
    return [c["params"] for c in d.cmds if c["command"] == "text.create"]


def test_text_box_is_at_least_one_line_high():
    from app.tools import design
    d = design.Doc(1200, 630, "x")
    d.text(10, 10, "+36 20 267 0877", size=26.4, font="Source Sans 3", color="#fff", w=500, h=31.5)
    assert _texts(d)[0]["area"]["height"] >= 26.4 * 1.2          # a régi 31,5 pt-os dobozban nem látszott
    d.text(10, 100, "Cím", size=20, font="Noto Serif", color="#fff", w=500, h=200)
    assert _texts(d)[1]["area"]["height"] == 200                  # ami elég magas, az nem változik


@pytest.mark.parametrize("size", ["landscape", "square", "story"])
def test_portrait_photo_is_never_blown_up(tmp_path, size):
    from app.tools import design
    d = _social(tmp_path, size, (312, 464))
    assert d.layout["photo"] == "split"
    (p, w, h), = d.images
    assert w / 312 <= design.UPSCALE_MAX + 1e-6 and abs(w / h - 312 / 464) < 0.01      # egészben, arányosan
    assert 0 <= min(c["params"]["rect"][0] for c in d.cmds if c["command"] == "file.place")
    assert all(t["y"] + t["area"]["height"] <= d.h + 0.5 for t in _texts(d))           # minden szöveg a lapon
    assert [t["text"] for t in _texts(d)] == ["Dr. Vajda János", "Ügyfélbarát közös képviselet, elégedett lakók",
                                              "+36 20 267 0877"]


def test_big_photo_of_the_same_shape_fills_the_background(tmp_path):
    d = _social(tmp_path, "landscape", (1600, 860))
    assert d.layout["photo"] == "full"
    (p, w, h), = d.images
    assert (w, h) == (1200, 630)


def test_small_photo_of_the_right_shape_is_not_blown_up_either(tmp_path):
    from app.tools import design
    d = _social(tmp_path, "landscape", (480, 252))             # fekvő, de 2,5×-re kellene nagyítani
    assert d.layout["photo"] == "split"
    (p, w, h), = d.images
    assert w / 480 <= design.UPSCALE_MAX + 1e-6


def test_social_without_photo_still_shows_every_text(tmp_path):
    d = _social(tmp_path, "landscape", None)
    assert not d.images and len(_texts(d)) == 3
    assert all(t["y"] + t["area"]["height"] <= d.h + 0.5 for t in _texts(d))


def test_missing_texts_reads_the_pdf_text_layer_tolerantly():
    from app.tools import design
    pdf = "Dr. Vajda\nJános\nÜgyfélbarát közös képviselet,\nelégedett lakók\nT Á R S A S H Á Z K E Z E L Ő"
    assert design.missing_texts(["Dr. Vajda János", "Ügyfélbarát közös képviselet, elégedett lakók",
                                 "TÁRSASHÁZKEZELŐ"], pdf) == []                          # tördelés, ritkítás nem hiba
    assert design.missing_texts(["+36 20 267 0877"], pdf) == ["+36 20 267 0877"]
    two_cols = "első oszlop vége második\noszlop eleje harmadik szó"                     # hasábok más sorrendben
    assert design.missing_texts(["második oszlop eleje harmadik szó első oszlop vége"], two_cols) == []


def test_check_reports_missing_text_and_soft_photo(tmp_path, monkeypatch):
    from PIL import Image
    from app.tools import design
    d = design.Doc(1200, 630, "x")
    d.text(10, 10, "Látszik", size=30, font="Source Sans 3", color="#fff", w=400, h=60)
    d.text(10, 90, "Nem látszik", size=30, font="Source Sans 3", color="#fff", w=400, h=60)
    p = tmp_path / "kicsi.jpg"
    Image.new("RGB", (300, 158)).save(p)
    d.images.append((p, 1200, 630))                             # 4× nagyítva

    async def fake_probe(d_, layout=True):                      # a PDF szövegrétege: csak az első szöveg látszik
        return "Látszik"
    monkeypatch.setattr(design, "_probe_text", fake_probe)
    c = asyncio.run(design._check(d, "social", {"files": []}, 1))
    assert c["status"] == "failed"
    assert any("Nem látszik" in f for f in c["findings"])
    assert any("enlarged 4.0×" in f for f in c["findings"])


def _has_vector() -> bool:
    from app.tools import crafts
    return bool(crafts.vector_cli())


@pytest.mark.skipif(not _has_vector(), reason="the VectorCraft CLI is only in the production image")
def test_real_social_card_with_portrait_shows_everything(tmp_path, put_file):
    from PIL import Image
    from app.tools import design
    src = tmp_path / "portre.jpg"
    Image.new("RGB", (312, 464), (200, 210, 230)).save(src)
    fid = put_file(src)
    res = asyncio.run(design.render("social", {"title": "Dr. Vajda János", "subtitle": "Társasházkezelő, Pécs",
                                               "handle": "+36 20 267 0877", "image": fid},
                                    size="landscape", export=["png", "pdf"]))
    assert res["layout"] == {"photo": "split"}
    assert res["check"]["status"] == "ok", res["check"]
    assert any("3 texts" in v for v in res["check"]["verified"])
