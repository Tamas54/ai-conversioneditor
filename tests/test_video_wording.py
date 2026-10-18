"""Saját szövegből videó: minden mondat benne van, és idővonal csak valódi dátumokból.

UI-próba (10-18): 4 mondatos szöveg, időtartam nélkül → n = 1 jelenet → a modell két mondatot tartott meg, kettő
kimaradt; az idővonal pontjai „Kr. e. 100 körül” és „ettől kezdve” voltak (az utóbbi nem időpont).
"""
from __future__ import annotations

from app.tools.video import _chart_from_plan, _restore_wording, _sentences

TXT = ("Kr. e. 100 körül Marius reformja átalakította a római sereget. A légiósok ettől kezdve hivatásos katonák "
       "lettek, állami fegyverzettel. A nagy, ívelt pajzs és a hosszú dárda a csatasor fegyelmét szolgálta. "
       "Egy légió nagyjából 5000 főből állt.")


def test_sentences_respect_abbreviations_and_short_ones():
    assert len(_sentences(TXT)) == 4 and _sentences(TXT)[0].startswith("Kr. e. 100")
    assert _sentences("Dr. Kiss jött. Ez jó.\n\nJ. R. R. Tolkien írta.") == ["Dr. Kiss jött.", "Ez jó.", "J. R. R. Tolkien írta."]


def test_left_out_sentences_come_back_in_order():
    scenes = [{"headline": "Marius", "caption": " ".join(_sentences(TXT)[:2]), "image_query": "roman legion"}]
    assert _restore_wording(TXT, scenes) == 2
    text = " ".join(sc["caption"] for sc in scenes)
    assert all(s in text for s in _sentences(TXT))
    assert all(len(sc["caption"]) <= 200 for sc in scenes) and scenes[-1]["image_query"] == "roman legion"


def test_timeline_needs_two_real_dates():
    plan = {"type": "timeline", "title": "A reform", "items": [{"label": "Kr. e. 100 körül", "text": "Marius reformja"},
                                                               {"label": "ettől kezdve", "text": "hivatásos katonák"}]}
    chart, note = _chart_from_plan(plan, TXT)
    assert chart is None and "two real dates" in note
    ok = {"type": "timeline", "title": "Évek", "items": [{"label": "1987", "text": "nyitás"}, {"label": "2020", "text": "bővítés"}]}
    chart, _ = _chart_from_plan(ok, "1987-ben nyitottunk, 2020-ban bővítettünk.")
    assert chart and len(chart["items"]) == 2


def test_title_layer_repeating_a_chart_title_is_dropped():
    """UI-próba 10-18: a tervező egy oszlopdiagram mellé ugyanazzal a szöveggel címréteget is tett — dupla cím."""
    from app.tools.video import layers_from
    notes: list[str] = []
    src = "15 és 18 másodperc között: Három fontos gondolat, 3, 5 és 8."
    out = layers_from([{"type": "chart", "at": 15, "dur": 3, "chart": {"type": "column", "title": "Három fontos gondolat",
                                                                       "items": [{"label": "a", "value": 3}, {"label": "b", "value": 5}]}},
                       {"type": "title", "at": 15, "dur": 3, "text": "Három fontos gondolat"},
                       {"type": "title", "at": 40, "dur": 3, "text": "Három fontos gondolat"}], src, notes)
    assert [ly["type"] for ly in out] == ["chart", "title"] and out[1]["at"] == 40
    assert any("repeated the title" in n for n in notes)
