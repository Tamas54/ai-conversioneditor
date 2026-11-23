"""A pakli az Anthropic pptx-skill mércéjén (11-23, a robotos bemutató előtt) — ami ott szabály, nálunk kód vagy gépi
ellenőrzés:

- valódi felsorolásjel függő behúzással (a második sor a szöveg alá kerül, nem a jel alá), a jel nem betű a szövegben;
- a szám és a mértékegysége (és az ezres tagolás) nem törhet két sorba — nem törő szóköz;
- nincs „AI-jel”: akcentusvonal a cím alatt, színes csík a kártya szélén, sáv a címlapon;
- a statisztika-kártyákon egyforma a számméret; az egy sorozatú oszlop/sáv értékfelirattal jön; a lábléc egy sor;
- a check gépileg nézi: maradék helykitöltő szöveg, csak szöveges dia, ponttal záruló cím, gyenge kontraszt, a szám és
  az egysége két sorban, kettétört szó;
- a find_replace a sima szóközös mintával is megtalálja a nem törő szóközzel kötött „90 %”-ot."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

NB = " "
WJ = "\u2060"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _prs(fid: str):
    from pptx import Presentation
    from app.storage import output_path
    return Presentation(str(output_path(fid)))


async def _build(slides: list[dict], **kw) -> object:
    from app.tools import pptx_deck
    return _prs((await pptx_deck.build({"title": "Próba", "slides": slides, **kw}))["file_id"])


# ---------------------------------------------------------------- építés

def test_keep_units_binds_figures_to_their_units_only():
    from app.tools.pptx_deck import keep_units as k
    assert k("90 % felett") == f"90{NB}% felett"
    assert k("4,3 millió robot") == f"4,3{NB}millió robot"
    assert k("10 000 ipari dolgozó") == f"10{NB}000 ipari dolgozó"
    assert k("15–25 %-kal nő") == f"15{WJ}–{WJ}25{NB}%-kal nő"
    assert k("€ 5 és 26 M") == f"€{NB}5 és 26{NB}M"
    # ami nem egység, az törhet: szám + szó, a betűvel folytatódó „egység” („26 Magyar”, „5 évvel”)
    assert k("2019 óta") == "2019 óta" and k("26 Magyar") == "26 Magyar" and k("5 évvel") == "5 évvel"


async def test_bullets_are_real_bullets_with_a_hanging_indent():
    prs = await _build([{"layout": "bullets", "title": "Három állítás",
                         "bullets": ["A hegesztés és a festés 90 % felett automatizált", "Második pont"]}])
    box = next(sh for sh in prs.slides[0].shapes if sh.has_text_frame and "hegesztés" in sh.text_frame.text)
    assert "■" not in box.text_frame.text                                    # a jel nem betű a szövegben
    assert f"90{NB}%" in box.text_frame.text
    for p in box.text_frame.paragraphs:
        pPr = p._p.pPr
        ch = pPr.find(f"{A}buChar")
        assert ch is not None and ch.get("char") == "■"
        assert int(pPr.get("marL")) > 0 and int(pPr.get("indent")) == -int(pPr.get("marL"))   # függő behúzás
        # a séma sorrendje: lnSpc/spcAft előbb, aztán buClr, buSzPct, buFont, buChar
        tags = [el.tag.split("}")[1] for el in pPr]
        assert tags.index("spcAft") < tags.index("buClr") < tags.index("buSzPct") < tags.index("buFont") < tags.index("buChar")


async def test_no_accent_rule_under_titles_no_card_edge_stripes_no_cover_bar():
    from pptx.util import Inches
    prs = await _build([{"layout": "cover", "title": "Robotok a munkahelyen", "subtitle": "Mit mutatnak a számok"},
                        {"layout": "bullets", "title": "Három állítás", "bullets": ["Egy", "Kettő", "Három"]},
                        {"layout": "stats", "title": "Számokban",
                         "stats": [{"value": "4,3 millió", "label": "robot"}, {"value": "162", "label": "sűrűség"}]}])
    for slide in prs.slides:
        thin = [sh for sh in slide.shapes if not sh.has_text_frame or not sh.text_frame.text.strip()
                if sh.width and sh.height and min(sh.width, sh.height) < Inches(0.15)]
        assert not thin, [(sh.name, sh.width, sh.height) for sh in thin]


def _stat_boxes(prs, n: int = 0) -> dict[str, object]:
    """A statisztika-dia szám- és címkedobozai szöveg szerint (a nem törő szóköz sima szóközként)."""
    return {sh.text_frame.text.replace(NB, " "): sh for sh in prs.slides[n].shapes if sh.has_text_frame and sh.text_frame.text}


def _pt(sh) -> float:
    return sh.text_frame.paragraphs[0].runs[0].font.size.pt


async def test_stat_numbers_are_big_and_one_size_per_slide():
    """A skill mércéje: 60–72 pt-os nagy szám, alatta olvasható címke; egy dián egy méret, a legnagyobb, ami kifér."""
    from app.tools import pptx_deck
    prs = await _build([{"layout": "stats", "title": "Három szám",
                         "stats": [{"value": "162", "label": "robot jut 10 000 dolgozóra"},
                                   {"value": "12 %", "label": "évi növekedés"}, {"value": "2030", "label": "humanoidok"}]},
                        {"layout": "stats", "title": "Négy szám",
                         "stats": [{"value": "4,3 M", "label": "millió ipari robot"}, {"value": "162", "label": "sűrűség"},
                                   {"value": "12 %", "label": "növekedés"}, {"value": "2030", "label": "humanoid"}]}])
    for n, vals in ((0, ("162", "12 %", "2030")), (1, ("4,3 M", "162", "12 %", "2030"))):
        b = _stat_boxes(prs, n)
        sizes = {_pt(b[v]) for v in vals}
        assert len(sizes) == 1, sizes
        size = sizes.pop()
        assert 60 <= size <= 72, size
        # tényleg egy sorba fér (a valódi betűmetrikával), és a következő lépcső (size + 2) már nem férne — vagy 72
        w = b[vals[0]].width / 12700
        widest = max(pptx_deck.text_width(b[v].text_frame.text, "Cambria", size, bold=True) for v in vals)
        assert widest <= w and (size == 72 or max(pptx_deck.text_width(b[v].text_frame.text, "Cambria", size + 2, True)
                                                  for v in vals) > w * 0.97)
    label = _stat_boxes(prs, 0)["évi növekedés"]
    assert _pt(label) >= 16 and label.top > _stat_boxes(prs, 0)["12 %"].top      # a címke alatta, olvasható méretben


async def test_a_figure_too_long_even_at_44pt_sends_its_unit_word_to_the_label_and_never_breaks():
    """11-23: a 44 pt-on sem férő szám régen a szóközénél két sorba tört — most a szava a címkébe kerül, a check szól."""
    from app.tools import pptx_deck
    res = await pptx_deck.build({"title": "Próba", "slides": [
        {"layout": "stats", "title": "A robotizáció számokban",
         "stats": [{"value": "4,3 millió", "label": "robot"}, {"value": "162", "label": "sűrűség"},
                   {"value": "12 %", "label": "növekedés"}, {"value": "2030", "label": "humanoid"}]}]})
    b = _stat_boxes(_prs(res["file_id"]))
    assert "4,3" in b and "millió robot" in b and "4,3 millió" not in b
    sizes = {_pt(b[v]) for v in ("4,3", "162", "12 %", "2030")}
    assert len(sizes) == 1 and sizes.pop() >= 44                                 # egy méret, a padló fölött
    assert b["12 %"].text_frame.text == f"12{NB}%"                                # a többi kötve marad
    assert any("“4,3 millió”" in f and "millió" in f for f in res["check"]["findings"])


def test_a_split_big_figure_gets_a_shorten_fix():
    from app.tools.validate import _break_fix
    it = {"id": 7, "box": [50, 200, 210, 330], "size": 44}
    fix = _break_fix("split_number", 4, it, "4,3", "millió")
    assert "Shorten" in fix and '"label"' in fix and "text.set" in fix
    assert "no-break space" in _break_fix("split_number", 4, {**it, "size": 18}, "90", "%")


async def test_built_slides_are_named_after_their_layout_and_three_alike_in_a_row_are_flagged(tmp_path):
    from app.storage import output_path
    from app.tools import pptx_deck, validate
    bl = lambda t: {"layout": "bullets", "title": t, "bullets": ["Egy pont", "Két pont"]}  # noqa: E731
    out = await pptx_deck.build({"title": "Próba", "slides": [
        bl("Egy"), bl("Kettő"), bl("Három"), {"layout": "quote", "quote": "Idézet."}, bl("Négy"), bl("Öt"),
        {"layout": "closing"}]})
    prs = _prs(out["file_id"])
    assert [s._element.cSld.get("name") for s in prs.slides][:4] == ["bullets", "bullets", "bullets", "quote"]
    pages, sw, sh = validate._pptx_shapes(output_path(out["file_id"]))
    runs = [i for i in validate.design_issues(pages, sw, sh) if i["kind"] == "same_layout"]
    assert [(i["page"], i["severity"]) for i in runs] == [(1, "warning")]       # 1–3 igen, a 5–6 (kettő) nem
    assert "Slides 1–3" in runs[0]["message"] and "image_left" in runs[0]["fix"]


def test_same_layout_by_arrangement_when_every_slide_sits_on_one_layout(tmp_path):
    from app.tools import validate
    pic = {"fill": "DDDDDD", "box": (7, 0, 6.3, 7.5)}
    p = _deck(tmp_path / "d.pptx", [[TITLE, BODY], [TITLE, BODY], [TITLE, BODY], [TITLE, pic, {**BODY, "box": (0.6, 2, 6, 3)}],
                                    [TITLE, BODY], [TITLE, BODY]])
    pages, sw, sh = validate._pptx_shapes(p)
    runs = [i for i in validate.design_issues(pages, sw, sh) if i["kind"] == "same_layout"]
    assert [i["page"] for i in runs] == [1] and "Slides 1–3" in runs[0]["message"]


async def test_single_series_column_has_value_labels_unless_turned_off():
    prs = await _build([{"layout": "chart", "title": "Robotsűrűség", "chart_type": "column",
                         "categories": ["Dél-Korea", "Japán", "Magyarország"], "series": [{"name": "2024", "values": [1012, 419, 142]}]},
                        {"layout": "chart", "title": "Ki", "chart_type": "column", "data_labels": False,
                         "categories": ["A", "B"], "series": [{"name": "x", "values": [1, 2]}]},
                        {"layout": "chart", "title": "Két sorozat", "chart_type": "line", "categories": ["2016", "2026"],
                         "series": [{"name": "a", "values": [100, 231]}, {"name": "b", "values": [100, 106]}]}])
    charts = [next(sh.chart for sh in s.shapes if sh.has_chart) for s in prs.slides]
    assert charts[0].plots[0].has_data_labels
    assert not charts[1].plots[0].has_data_labels and not charts[2].plots[0].has_data_labels


async def test_footer_stays_on_one_line_on_a_half_slide(put_file, tmp_path):
    from PIL import Image
    from app.tools.video import _wrap
    Image.new("RGB", (800, 900), (90, 120, 160)).save(tmp_path / "kep.jpg")
    long = "Robotok és munkaerőpiac · minta-diasor egy nagyon hosszú lábléccel, amely a fél dián nem fér el egy sorban"
    prs = await _build([{"layout": "image_right", "title": "Raktár", "bullets": ["Egy"],
                         "image": put_file(tmp_path / "kep.jpg")}], footer=long)
    foot = next(sh for sh in prs.slides[0].shapes if sh.has_text_frame and sh.text_frame.text.startswith("Robotok"))
    run = foot.text_frame.paragraphs[0].runs[0]
    assert _wrap(run.text, run.font.size.pt, foot.width / 12700 * 1.1)[0] == 1 and run.font.size.pt >= 9
    assert run.text.endswith("…") and len(run.text) < len(long)


# ---------------------------------------------------------------- gépi ellenőrzés (fájlból)

def _deck(path: Path, slides: list[list[dict]], bg: str = "FFFFFF") -> Path:
    """slides: diánként [{text, size, color?, box (in), fill?}] — szövegdoboz vagy (fill-lel) kitöltött téglalap."""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches, Pt
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    for items in slides:
        s = prs.slides.add_slide(prs.slide_layouts[6])
        s.background.fill.solid(); s.background.fill.fore_color.rgb = RGBColor.from_string(bg)
        for it in items:
            x, y, w, h = (Inches(v) for v in it["box"])
            if it.get("fill"):
                sh = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, h)
                sh.fill.solid(); sh.fill.fore_color.rgb = RGBColor.from_string(it["fill"])
                continue
            tb = s.shapes.add_textbox(x, y, w, h)
            tb.text_frame.word_wrap = True
            tb.text_frame.text = it["text"]
            r = tb.text_frame.paragraphs[0].runs[0]
            r.font.size = Pt(it.get("size", 20))
            r.font.color.rgb = RGBColor.from_string(it.get("color", "1E2430"))
    prs.save(str(path))
    return path


def _kinds(path: Path) -> dict[str, list[dict]]:
    from app.tools import validate
    pages, sw, sh = validate._pptx_shapes(path)
    out: dict[str, list[dict]] = {}
    for it in validate.design_issues(pages, sw, sh):
        out.setdefault(it["kind"], []).append(it)
    return out


TITLE = {"text": "Mit tegyünk 2030-ig", "size": 36, "box": (0.6, 0.5, 12, 1.0)}
BODY = {"text": "Átképzési utalvány minden olyan dolgozónak, akinek a munkakörét a cége robotizálja, és kötelező "
                "robotkezelői modul a szakképzésben", "size": 20, "box": (0.6, 2.0, 12, 3)}


def test_text_only_slide_is_flagged_but_a_picture_card_or_dark_divider_is_not(tmp_path):
    p = _deck(tmp_path / "d.pptx", [
        [TITLE, BODY],                                                                   # 1: csak szöveg → jelez
        [TITLE, {"fill": "F4F5F7", "box": (0.6, 2.0, 3, 3)}, BODY],                       # 2: kártya → rendben
        [{"fill": "14213D", "box": (0, 0, 13.333, 7.5)},                                  # 3: sötét fejezetdia → rendben
         {**TITLE, "color": "FFFFFF", "box": (0.6, 3, 12, 1)},
         {**BODY, "color": "FFFFFF", "box": (0.6, 4.2, 12, 2)}],
        [TITLE, {"text": "Rövid", "size": 20, "box": (0.6, 2, 5, 1)}],                     # 4: kevés szó → nem jelez
    ])
    k = _kinds(p)
    assert [i["page"] for i in k.get("text_only", [])] == [1]
    assert k["text_only"][0]["severity"] == "warning" and '"image"' in k["text_only"][0]["fix"]


def test_leftover_placeholder_text_title_period_and_low_contrast(tmp_path):
    p = _deck(tmp_path / "d.pptx", [
        [{**TITLE, "text": "Lorem ipsum dolor"}, {**BODY, "text": "TODO: számok"}],
        [{**TITLE, "text": "A robotok nem veszik el a munkát."}, {**BODY, "text": "Ld. a Kft. adatait stb."}],
        [TITLE, {**BODY, "color": "D9D9D9"}],                                              # világosszürke fehéren
        [TITLE, {"fill": "F3EEDC", "box": (0.6, 2, 12, 3)}, {**BODY, "color": "E8DFC4"}],  # krém krémen (a kártyán)
        [{**TITLE, "text": "„", "size": 110, "color": "FCA311"}, BODY],                    # díszítő idézőjel: nem szöveg
    ])
    k = _kinds(p)
    hard = [i for i in k["placeholder_text"] if i["severity"] == "error"]
    soft = [i for i in k["placeholder_text"] if i["severity"] == "warning"]
    assert [i["page"] for i in hard] == [1] and [i["page"] for i in soft] == [1]
    assert [i["page"] for i in k["title_period"]] == [2]                     # a „stb.” a törzsszövegben nem cím
    assert [i["page"] for i in k["low_contrast"]] == [3, 4]
    assert all("format.color" in i["fix"] for i in k["low_contrast"])


# ---------------------------------------------------------------- gépi ellenőrzés (renderelt sorvégek)

def test_line_breaks_find_split_figures_and_broken_words_only():
    from app.tools.validate import _line_breaks
    box = [0, 0, 200, 100]

    def words(*lines):     # soronként szavak → (x0, y0, x1, y1, szó)
        return [(10 + 40 * i, 10 + 20 * n, 45 + 40 * i, 24 + 20 * n, w) for n, ln in enumerate(lines) for i, w in enumerate(ln)]
    assert _line_breaks(words(["festés", "90"], ["%", "felett"]), box, "festés 90 % felett") == ("split_number", "90", "%")
    assert _line_breaks(words(["robot", "10"], ["000", "dolgozó"]), box, "robot 10 000 dolgozó") == ("split_number", "10", "000")
    assert _line_breaks(words([f"4,3{NB}milli"], ["ó"]), box, f"4,3{NB}millió") == ("broken_word", "milli", "ó")
    assert _line_breaks(words(["Dél-"], ["Korea"]), box, "Dél-Korea") is None            # kötőjelnél törni szabad
    assert _line_breaks(words(["2019"], ["óta", "nő"]), box, "2019 óta nő") is None
    assert _line_breaks(words(["a", "festés"], ["90", "%"]), box, "a festés 90 %") is None  # egy sorban


@pytest.mark.engines
async def test_rendered_check_reports_a_word_broken_by_a_narrow_box(put_file, tmp_path):
    if shutil.which("soffice") is None:
        pytest.skip("LibreOffice not installed")
    from app.storage import input_path
    from app.tools import validate
    p = _deck(tmp_path / "d.pptx", [[TITLE, {"text": "Robotsűrűség-növekedés Magyarországon", "size": 28,
                                             "box": (0.6, 2.0, 1.6, 3.0)}]])
    src = input_path(put_file(p))
    layouts, _ = await validate._layouts(src, "deck", want_file=True)
    try:
        issues = validate.deck_issues(src, layouts)
    finally:
        for L in layouts:
            L.close()
    broken = [i for i in issues if i["kind"] == "broken_word"]
    assert broken and broken[0]["slide"] == 1 and "setBounds" in broken[0]["fix"], issues


# ---------------------------------------------------------------- szerkesztés

def test_find_replace_matches_a_figure_bound_with_a_no_break_space(tmp_path):
    from pptx import Presentation
    from app.tools.edits import _pptx_find_replace
    src = _deck(tmp_path / "a.pptx", [[{**BODY, "text": f"A festés 90{NB}% felett automatizált"}]])
    dst = tmp_path / "b.pptx"
    _pptx_find_replace(src, dst, "90 % felett", r"95 % felett \1", regex=False)
    texts = [sh.text_frame.text for sh in Presentation(str(dst)).slides[0].shapes if sh.has_text_frame]
    assert texts == [r"A festés 95 % felett \1 automatizált"]                    # szó szerinti csere
