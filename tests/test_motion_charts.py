"""Új mozgógrafikák a videóban (app/tools/motion_charts.py): vízesés, előtte → utána, pont/buborék, sáv-előrejelzés,
több vonal, rangsor-verseny — a számok a forrásból, a kiszámolt vég ellenőrizve, a terv újra beolvasva ugyanaz
(szerkesztés), és minden elem a képkockán belül marad mindhárom formátumban."""
from __future__ import annotations

import math

import pytest

SRC = ("A költségvetés 2025-ben 120 milliárd forint volt. Az adóbevétel 18 milliárddal nőtt, a támogatás 7 milliárddal "
       "csökkent, az energia 9 milliárddal nőtt. Távhő 2022-ben 4200, 2026-ban 3900; víz 2022-ben 2100, 2026-ban 2600. "
       "Tarján 12,5 és 1,9, 41 000 lakás; Belváros 6,8 és 0,9, 18 000 lakás; Uszoda 1,9 és 0,31, 3 000 lakás. "
       "Gáz 2022: 62, 2023: 58, 2024: 55; 2025: 47 (44–50), 2026: 43 (38–48). "
       "CO2 2026: 100; gyors 2027: 85, 2028: 72; lassú 2027: 96, 2028: 93. "
       "Részarány 2020: gáz 80, geotermia 12, biomassza 6; 2022: gáz 72, geotermia 18, biomassza 7; "
       "2024: gáz 60, geotermia 27, biomassza 8.")
CHARTS = {
    "waterfall": {"type": "waterfall", "title": "Költségvetés", "unit": "Mrd Ft",
                  "items": [{"label": "2025", "value": 120}, {"label": "Adó", "value": 18}, {"label": "Támogatás", "value": -7},
                            {"label": "Energia", "value": 9}]},
    "slope": {"type": "slope", "title": "Díjak", "unit": "Ft", "labels": ["2022", "2026"],
              "items": [{"label": "Távhő", "from": 4200, "to": 3900}, {"label": "Víz", "from": 2100, "to": 2600}]},
    "scatter": {"type": "scatter", "title": "Projektek", "x_title": "Beruházás, Mrd Ft", "y_title": "Megtakarítás, Mrd Ft",
                "items": [{"label": "Tarján", "x": 12.5, "y": 1.9, "size": 41000}, {"label": "Belváros", "x": 6.8, "y": 0.9, "size": 18000},
                          {"label": "Uszoda", "x": 1.9, "y": 0.31, "size": 3000}]},
    "band": {"type": "band", "title": "Gáz", "forecast_from": "2025",
             "items": [{"label": "2022", "value": 62}, {"label": "2023", "value": 58}, {"label": "2024", "value": 55},
                       {"label": "2025", "value": 47, "low": 44, "high": 50}, {"label": "2026", "value": 43, "low": 38, "high": 48}]},
    "lines": {"type": "lines", "title": "CO2", "labels": ["2026", "2027", "2028"],
              "series": [{"name": "Gyors", "values": [100, 85, 72]}, {"name": "Lassú", "values": [100, 96, 93]}]},
    "race": {"type": "race", "title": "Részarány", "unit": "%", "labels": ["2020", "2022", "2024"],
             "items": [{"label": "Gáz", "values": [80, 72, 60]}, {"label": "Geotermia", "values": [12, 18, 27]},
                       {"label": "Biomassza", "values": [6, 7, 8]}]},
}


def _notes(n):
    return n if isinstance(n, list) else [n] if n else []


@pytest.mark.parametrize("typ", list(CHARTS))
def test_numbers_come_from_the_source_and_the_plan_reads_back_the_same(typ):
    from app.tools import video
    ch, note = video._chart_from_plan(CHARTS[typ], SRC)
    assert ch and ch["type"] == typ and not any(n.startswith("⚠") for n in _notes(note)), note
    again, _ = video._chart_from_plan(ch, SRC)                  # szerkesztéskor a tárolt terv újra átmegy az ellenőrzésen
    assert again == ch
    bad = {**CHARTS[typ]}
    key = "items" if typ != "lines" else "series"
    first = dict(bad[key][0])
    for k in ("value", "from", "x"):
        if k in first:
            first[k] = 777
    if "values" in first:
        first["values"] = [777] + first["values"][1:]
    bad[key] = [first] + bad[key][1:]
    gone, why = video._chart_from_plan(bad, SRC)
    assert gone is None and any("left out" in n for n in _notes(why))           # kitalált szám → kimarad, és meg is mondja


def test_waterfall_end_is_computed_and_a_wrong_total_is_said():
    from app.tools import video
    ch, note = video._chart_from_plan(CHARTS["waterfall"], SRC)
    assert ch["total"] == 140 and "computed" in note
    ch2, note2 = video._chart_from_plan({**CHARTS["waterfall"], "total": 150}, SRC)
    assert ch2["total"] == 140 and note2[0].startswith("⚠") and "150" in note2[0]


def test_aliases_dates_and_axis_titles():
    from app.tools import video
    assert video._chart_from_plan({**CHARTS["waterfall"], "type": "bridge"}, SRC)[0]["type"] == "waterfall"
    gone, why = video._chart_from_plan({**CHARTS["slope"], "labels": ["2019", "2026"]}, SRC)
    assert gone is None and "2019" in why                                     # a dátum is a forrásból
    ch, notes = video._chart_from_plan({**CHARTS["scatter"], "x_title": "", "y_title": ""}, SRC)
    assert ch and any(n.startswith("⚠") and "x_title" in n for n in notes)    # tengelycím nélkül: figyelmeztetés


@pytest.mark.parametrize("fmt", ["landscape", "vertical", "square"])
@pytest.mark.parametrize("typ", list(CHARTS))
def test_elements_stay_in_the_frame_and_text_is_not_squeezed(typ, fmt):
    from app.tools import video
    W, H = video.FORMATS[fmt]
    ch, _ = video._chart_from_plan(CHARTS[typ], SRC)
    els = video.chart_elements(ch, W, H, "hu", [0.95, 0.65, 0.25], dur=8)
    assert els and video.chart_elements(ch, W, H, "hu", [0.95, 0.65, 0.25], dur=8) == els     # determinisztikus
    m = min(W, H)
    for e in els:
        pts = [e["pos"]] if "pos" in e else e.get("points", [])
        for x, y in pts:
            assert math.isfinite(x) and math.isfinite(y) and -2 <= x <= W + 2 and -2 <= y <= H + 2, (e["name"], x, y)
        if e["k"] == "text" and e["name"].split(" – ")[0] in ("Value", "From value", "To value", "Name"):
            assert e["size"] >= round(m * 0.03) * 0.85, (e["name"], e["size"])           # nem zsugorodott olvashatatlanra


def test_race_bars_glide_through_keyframes_in_time_order():
    from app.tools import video
    ch, _ = video._chart_from_plan(CHARTS["race"], SRC)
    els = video.chart_elements(ch, 1920, 1080, "hu", [0.95, 0.65, 0.25], dur=8)
    bars = [e for e in els if e["name"].startswith("Bar – ")]
    assert len(bars) == 3 and all(e.get("keys") for e in bars)
    for e in bars:
        ts = [k[0] for k in e["keys"]]
        assert ts == sorted(ts) and e["keys"][0][4] == 0                             # beúszik, aztán időrendben mozog
    gaz = next(e for e in bars if e["name"] == "Bar – Gáz")
    assert gaz["keys"][-1][2] < next(e for e in bars if e["name"] == "Bar – Biomassza")["keys"][-1][2]   # a nagyobb van felül
    periods = [e for e in els if e["name"].startswith("Period ")]
    assert [e["str"] for e in periods] == ["2020", "2022", "2024"] and all(e.get("cut") for e in periods)


def test_on_screen_numbers_count_for_coverage():
    from app.tools import video
    ch, _ = video._chart_from_plan(CHARTS["race"], SRC)
    txt = video._video_text({"scenes": [{"headline": "", "caption": "", "chart": ch}], "layers": []})
    assert "27" in txt and "2024" in txt
