"""Összetett pakli kliensként (10-17, a szegedi energiaprogram próbája) — ami ott elcsúszott, általánosan javítva:

- a címlap felső sora (kicker) és a többsoros cím nem kerül egymásra (a cím betűje a dobozához igazodik);
- a táblázat oszlopai a tartalomhoz szélesek, a sorok magassága a szöveghez, és az egész a cím és a lábléc közé fér;
- a kör- és fánkdiagram szeletein százalék van (nem a nyers érték), a pontdiagramnak tengelycíme és pontnevei lehetnek;
- a stockkép-keresés rövidítéskor sem dobja el a tulajdonnevet („Szeged city aerial” ≠ „city aerial”)."""
from __future__ import annotations

from pathlib import Path

C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"


def _deck(tmp_path: Path, slides: list[dict]) -> Path:
    from pptx import Presentation
    from pptx.util import Inches
    from app.tools import pptx_deck
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(pptx_deck.W_IN), Inches(pptx_deck.H_IN)
    t = dict(pptx_deck.DEFAULT_THEME)
    t.setdefault("panel", "#F3F4F6")
    for s in slides:
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        {"cover": lambda: pptx_deck._cover(slide, s, t, None), "table": lambda: pptx_deck._table(slide, s, t),
         "chart": lambda: pptx_deck._chart(slide, s, t)}[s["layout"]]()
    p = tmp_path / "d.pptx"
    prs.save(str(p))
    return p


def test_cover_kicker_stays_above_a_long_title(tmp_path):
    from pptx import Presentation
    p = _deck(tmp_path, [{"layout": "cover", "kicker": "Közgyűlési előterjesztés · 2026. november",
                          "title": "Szeged energiaátállása és távhőfejlesztési programja 2027–2030"}])
    boxes = {sh.text_frame.text: sh for sh in Presentation(str(p)).slides[0].shapes if sh.has_text_frame and sh.text_frame.text}
    kicker = next(v for k, v in boxes.items() if k.startswith("KÖZGYŰLÉSI"))
    title = next(v for k, v in boxes.items() if k.startswith("Szeged"))
    assert kicker.top + kicker.height <= title.top                                   # a két doboz nem fedi egymást
    size = title.text_frame.paragraphs[0].runs[0].font.size.pt
    assert size < 54                                                                 # a hosszú cím kisebb betűvel fér be


def test_table_columns_follow_the_content_and_fit_the_slide(tmp_path):
    from pptx import Presentation
    from app.tools import pptx_deck
    rows = [["Geotermikus kút és hőközpont – Tarján", "2027–2028", "12,5", "Szegedi Hőszolgáltató"],
            ["Közintézmények napelemes rendszerei", "2027", "4,2", "Városüzemeltetés"]] * 4
    p = _deck(tmp_path, [{"layout": "table", "title": "Projektek", "headers": ["Projekt", "Ütem", "Mrd Ft", "Felelős"], "rows": rows}])
    tbl = next(sh for sh in Presentation(str(p)).slides[0].shapes if sh.has_table)
    w = [c.width for c in tbl.table.columns]
    assert w[0] > w[3] > w[2] and w[1] > w[2]                                        # a szöveges oszlop széles, a szám keskeny
    assert (tbl.top + sum(r.height for r in tbl.table.rows)) / 914400 <= pptx_deck.H_IN - 0.7   # a lábléc fölött véget ér


def test_pie_slices_show_percent_and_scatter_has_axis_titles_and_names(tmp_path):
    from pptx import Presentation
    p = _deck(tmp_path, [
        {"layout": "chart", "title": "Források", "chart_type": "doughnut", "categories": ["EU", "Állam", "Város"],
         "series": [{"name": "Mrd Ft", "values": [17.3, 6.9, 9.2]}]},
        {"layout": "chart", "title": "Projektek", "chart_type": "scatter", "x_title": "Beruházás (Mrd Ft)",
         "y_title": "Megtakarítás (Mrd Ft/év)", "series": [{"name": "Projektek", "points": [[12.5, 1.9, "Tarján"], [4.2, 0.7]]}]}])
    prs = Presentation(str(p))
    pie = next(sh for sh in prs.slides[0].shapes if sh.has_chart).chart
    lbls = list(pie._chartSpace.iter(f"{C}dLbl"))
    assert len(lbls) == 3
    for d in lbls:                                                                    # szeletenként százalék, nem érték
        assert d.find(f"{C}showPercent").get("val") == "1" and d.find(f"{C}showVal").get("val") == "0"
        assert d.find(f"{C}numFmt").get("formatCode") == "0%"
    sc = next(sh for sh in prs.slides[1].shapes if sh.has_chart).chart
    assert sc.category_axis.axis_title.text_frame.text == "Beruházás (Mrd Ft)"
    assert sc.value_axis.axis_title.text_frame.text == "Megtakarítás (Mrd Ft/év)"
    names = [t.text for t in sc._chartSpace.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t")]
    assert "Tarján" in names


def test_stock_search_keeps_the_place_name_when_it_shortens():
    from app.tools.images import _tries
    tries = _tries("Szeged city aerial view")
    assert tries[0] == "Szeged city aerial view" and all("Szeged" in q for q in tries)
    assert "Szeged" in tries                                                          # végül csak a név
    assert _tries("gas pipeline valve") == ["gas pipeline valve", "pipeline valve", "gas pipeline"]   # név nélkül a régi


# ---------- a videó ábrái a pakliban (10-17): ugyanaz az objektum, natív diagram vagy natív alakzat ----------

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _shapes(slide) -> dict:
    return {sh.name: sh for sh in slide.shapes}


def test_waterfall_is_a_stacked_column_with_an_invisible_base_and_signed_labels(tmp_path):
    from pptx import Presentation
    from pptx.enum.chart import XL_CHART_TYPE
    p = _deck(tmp_path, [{"layout": "chart", "title": "Budget", "chart_type": "waterfall", "unit": "bn",
                          "items": [{"label": "2025", "value": 120}, {"label": "Tax", "value": 18},
                                    {"label": "Subsidies", "value": -7}, {"label": "Energy", "value": 9}]}])
    ch = next(sh for sh in Presentation(str(p)).slides[0].shapes if sh.has_chart).chart
    assert ch.chart_type == XL_CHART_TYPE.COLUMN_STACKED and not ch.has_legend
    assert list(ch.plots[0].categories) == ["2025", "Tax", "Subsidies", "Energy", "End"]   # a vég kiszámolva, „End”
    base, up, down, total = ch.plots[0].series
    assert list(base.values) == [None, 120, 131, 131, None] and list(total.values) == [120, None, None, None, 140]
    assert list(up.values) == [None, 18, None, 9, None] and list(down.values) == [None, None, 7, None, None]
    alpha = base._element.find(f"{C}spPr/{A}solidFill/{A}srgbClr/{A}alpha")
    assert alpha is not None and alpha.get("val") == "0"                     # az alap láthatatlan (minden motorban)
    assert base._element.find(f"{C}dLbls") is None                           # és felirata sincs
    fmt = lambda s: s._element.find(f"{C}dLbls/{C}numFmt").get("formatCode")   # noqa: E731
    assert fmt(up).startswith('"+"') and fmt(down).startswith('"−"') and '" bn"' in fmt(total)   # az értékhez kötött felirat
    assert ch.value_axis.minimum_scale == 0                                  # a tengely nullától (nem 90-től)


def test_waterfall_below_zero_splits_the_crossing_bar_and_checks_the_given_total(tmp_path):
    from pptx import Presentation
    from app.tools import pptx_charts
    s = {"layout": "chart", "title": "Kassza", "chart_type": "bridge", "total": -10,
         "items": [{"label": "Nyitó", "value": 10}, {"label": "Sokk", "value": -25}, {"label": "Segély", "value": 8},
                   {"label": "Veszteség", "value": -6}]}
    p = _deck(tmp_path, [s])
    ch = next(sh for sh in Presentation(str(p)).slides[0].shapes if sh.has_chart).chart
    names = [x.name for x in ch.plots[0].series]
    assert "Decrease below zero" in names                                    # a nullát átlépő sáv két részből
    assert list(ch.plots[0].categories)[-1] == "Végül"                       # magyar dián magyar a vég
    texts = [t.text for t in ch._chartSpace.iter(f"{A}t")]
    assert "−25" in texts                                                    # a teljes változás egy feliratban
    warn, _ = pptx_charts.check_slides({"slides": [s]})
    assert warn and "Slide 1" in warn[0] and "-13" in warn[0]                # a megadott végösszeg nem jön ki


def test_slope_is_drawn_as_shapes_up_in_accent_down_in_red_with_names_and_values(tmp_path):
    from pptx import Presentation
    from app.tools import pptx_charts, pptx_deck
    p = _deck(tmp_path, [{"layout": "chart", "title": "Tariffs", "chart_type": "slope", "unit": "€", "labels": ["2022", "2026"],
                          "items": [{"label": "Heat", "from": 42, "to": 39}, {"label": "Water", "from": 21, "to": 26}]}])
    sl = Presentation(str(p)).slides[0]
    sh = _shapes(sl)
    assert not any(x.has_chart for x in sl.shapes)
    col = pptx_charts.colors(pptx_deck.DEFAULT_THEME)
    assert str(sh["Change – Water"].line.color.rgb) == col["up"].lstrip("#").upper()
    assert str(sh["Change – Heat"].line.color.rgb) == col["down"].lstrip("#").upper()
    assert sh["Heading – 2022"].text_frame.text == "2022" and sh["Heading – 2026"].text_frame.text == "2026"
    assert sh["Name – Heat"].text_frame.text == "Heat" and sh["From value – Heat"].text_frame.text == "€42"
    assert sh["To value – Water"].text_frame.text == "€26"
    assert sh["Name – Heat"].left + sh["Name – Heat"].width <= sh["From value – Heat"].left   # név és érték egymás mellett


def test_band_has_a_shaded_range_and_a_dashed_forecast_from_the_last_measured_point(tmp_path):
    from pptx import Presentation
    from pptx.enum.dml import MSO_LINE_DASH_STYLE
    p = _deck(tmp_path, [{"layout": "chart", "title": "Gas", "chart": {
        "type": "forecast", "unit": "TWh", "forecast_from": "2025",
        "items": [{"label": "2023", "value": 58}, {"label": "2024", "value": 55}, {"label": "2025", "value": 47, "low": 44, "high": 50},
                  {"label": "2026", "value": 43, "low": 38, "high": 48}]}}])
    sh = _shapes(Presentation(str(p)).slides[0])
    band = sh["Range band"]
    assert band._element.find(f".//{A}solidFill/{A}srgbClr/{A}alpha") is not None   # áttetsző sáv
    assert sh["Forecast path"].line.dash_style == MSO_LINE_DASH_STYLE.DASH
    assert sh["Line"].line.dash_style is None                                      # a mért rész folytonos
    dot = sh["Point – 2024"]                                                       # a szaggatott a 2024-es (utolsó mért) pontból indul
    assert abs(sh["Forecast path"].left - (dot.left + dot.width // 2)) <= 2
    assert sh["Range"].text_frame.text == "38–48 TWh" and sh["Forecast"].text_frame.text == "forecast"


def test_lines_and_race_become_native_charts(tmp_path):
    from pptx import Presentation
    from pptx.enum.chart import XL_CHART_TYPE
    from app.tools import pptx_charts
    race = {"layout": "chart", "title": "Share", "chart_type": "race", "unit": "%", "labels": ["2020", "2022", "2024"],
            "items": [{"label": "Gas", "values": [80, 72, 60]}, {"label": "Biomass", "values": [6, 7, 8]},
                      {"label": "Geothermal", "values": [12, 18, 27]}]}
    p = _deck(tmp_path, [{"layout": "chart", "title": "CO2", "chart_type": "lines", "labels": ["2026", "2027", "2028"],
                          "series": [{"name": "Fast", "values": [100, 85, 72]}, {"name": "Slow", "values": [100, 96, 93]}]}, race])
    prs = Presentation(str(p))
    ln = next(sh for sh in prs.slides[0].shapes if sh.has_chart).chart
    assert ln.chart_type == XL_CHART_TYPE.LINE_MARKERS and list(ln.plots[0].categories) == ["2026", "2027", "2028"]
    assert [s.name for s in ln.plots[0].series] == ["Fast", "Slow"] and ln.has_legend
    assert all(s._element.find(f"{C}spPr/{A}solidFill") is not None for s in ln.plots[0].series)   # a szerkesztő ebből színez
    bar = next(sh for sh in prs.slides[1].shapes if sh.has_chart).chart
    assert bar.chart_type == XL_CHART_TYPE.BAR_CLUSTERED
    assert list(bar.plots[0].categories) == ["Gas", "Geothermal", "Biomass"]          # az utolsó időszak rangsora
    assert list(bar.plots[0].series[0].values) == [60, 27, 8] and bar.category_axis.reverse_order
    _, info = pptx_charts.check_slides({"slides": [race]})
    assert "2024" in info[0] and "only in the video" in info[0]


def test_scatter_items_are_shapes_with_names_and_bubble_sizes(tmp_path):
    from pptx import Presentation
    p = _deck(tmp_path, [{"layout": "chart", "title": "Projects", "chart_type": "bubble", "x_title": "Investment, bn",
                          "y_title": "Savings, bn/year",
                          "items": [{"label": "Tarján", "x": 12.5, "y": 1.9, "size": 41000}, {"label": "Pool", "x": 1.9, "y": 0.31, "size": 3000},
                                    {"label": "Centre", "x": 6.8, "y": 0.9, "size": 18000}]}])
    sl = Presentation(str(p)).slides[0]
    sh = _shapes(sl)
    assert not any(x.has_chart for x in sl.shapes)
    assert sh["Point – Tarján"].width > sh["Point – Centre"].width > sh["Point – Pool"].width   # buborék: a méret látszik
    assert sh["Point – Tarján"].left > sh["Point – Centre"].left > sh["Point – Pool"].left      # x szerint, arányosan
    assert sh["Name – Pool"].text_frame.text == "Pool" and sh["X title"].text_frame.text == "Investment, bn"
    assert sh["Y title"].text_frame.text.endswith("Savings, bn/year")


def test_a_broken_chart_object_fails_before_the_build_with_the_slide_and_the_shape():
    import pytest
    from app.tools import pptx_charts
    bad = {"slides": [{"layout": "cover", "title": "x"},
                      {"layout": "chart", "title": "Bridge", "chart_type": "waterfall", "items": [{"label": "a", "value": 1}]}]}
    with pytest.raises(ValueError) as e:
        pptx_charts.check_slides(bad)
    assert "Slide 2 (waterfall)" in str(e.value) and '"items"' in str(e.value)
    with pytest.raises(ValueError) as e:
        pptx_charts.check_slides({"slides": [{"layout": "chart", "chart_type": "slope", "labels": ["a", "b"],
                                              "items": [{"label": "x", "from": "many", "to": 2}]}]})
    assert "not a number" in str(e.value)
    assert pptx_charts.kind_of({"chart_type": "scatter", "series": [{"points": [[1, 2]]}]}) is None   # a régi pontdiagram marad
