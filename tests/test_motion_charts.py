"""Új mozgógrafikák a videóban (app/tools/motion_charts.py): vízesés, előtte → utána, pont/buborék, sáv-előrejelzés,
több vonal, rangsor-verseny, sankey — a számok a forrásból, a kiszámolt vég ellenőrizve, a terv újra beolvasva ugyanaz
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
       "2024: gáz 60, geotermia 27, biomassza 8. "
       "Klímaalap: uniós pénz 70, ebből energetika 45, közlekedés 25; állami pénz 35: energetika 20, parkok 15; "
       "az energetikából távhő 40, napelem 25.")
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
    "sankey": {"type": "sankey", "title": "Klímaalap", "unit": "Mrd Ft",
               "flows": [{"from": "Uniós", "to": "Energetika", "value": 45}, {"from": "Uniós", "to": "Közlekedés", "value": 25},
                         {"from": "Állami", "to": "Energetika", "value": 20}, {"from": "Állami", "to": "Parkok", "value": 15},
                         {"from": "Energetika", "to": "Távhő", "value": 40}, {"from": "Energetika", "to": "Napelem", "value": 25}]},
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
    key = next(k for k in ("items", "series", "flows") if k in bad)
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


def test_sankey_refuses_loops_and_says_why():
    from app.tools import video
    loop = {**CHARTS["sankey"], "flows": CHARTS["sankey"]["flows"] + [{"from": "Távhő", "to": "Uniós", "value": 15}]}
    gone, why = video._chart_from_plan(loop, SRC)
    assert gone is None and why.startswith("⚠") and "circle" in why and "Uniós → Energetika → Távhő → Uniós" in why
    gone, why = video._chart_from_plan({**CHARTS["sankey"], "flows": [{"from": "Uniós", "to": "uniós", "value": 70},
                                                                      {"from": "Uniós", "to": "Parkok", "value": 15}]}, SRC)
    assert gone is None and "itself" in why                                    # önmagába (kis-nagybetű mindegy)
    gone, why = video._chart_from_plan({**CHARTS["sankey"], "flows": CHARTS["sankey"]["flows"][:1]}, SRC)
    assert gone is None and "two flows" in why


def test_sankey_nodes_keep_order_and_colour_and_totals_are_computed():
    from app.tools import motion_charts, video
    ch, note = video._chart_from_plan({**CHARTS["sankey"], "nodes": [{"name": "állami", "color": "#3366cc"}, "Nincs ilyen"]}, SRC)
    assert ch["nodes"] == [{"name": "Állami", "color": "#3366CC"}] and video._chart_from_plan(ch, SRC)[0] == ch
    assert "computed" in note and "3 stages" in note
    g = motion_charts._sankey_graph(ch["flows"])
    assert g["total"]["Energetika"] == 65 and g["total"]["Uniós"] == 70 and g["depth"]["Távhő"] == 2
    els = video.chart_elements(ch, 1920, 1080, "hu", [0.95, 0.65, 0.25], dur=8)
    assert next(e for e in els if e["name"] == "Node – Állami")["color"] == [0.2, 0.4, 0.8]
    assert next(e for e in els if e["name"] == "Node – Uniós")["color"] == [0.95, 0.65, 0.25]    # az első: az akcentus
    assert next(e for e in els if e["name"] == "Value – Energetika")["str"] == "65 Mrd Ft"
    txt = video._video_text({"scenes": [{"headline": "", "caption": "", "chart": ch}], "layers": []})
    assert "Energetika" in txt and "65" in txt


@pytest.mark.parametrize("fmt", ["landscape", "vertical", "square"])
def test_sankey_ribbons_start_and_end_at_their_nodes_and_never_cross_a_node(fmt):
    from app.tools import video
    W, H = video.FORMATS[fmt]
    skip = {**CHARTS["sankey"], "flows": CHARTS["sankey"]["flows"] + [{"from": "Uniós", "to": "Távhő", "value": 15}]}
    ch, _ = video._chart_from_plan(skip, SRC)                   # az Uniós → Távhő két oszlopot ugrik át
    els = video.chart_elements(ch, W, H, "hu", [0.95, 0.65, 0.25], dur=8)
    box = {e["name"][7:]: (e["pos"][0] - e["size"][0] / 2, e["pos"][1] - e["size"][1] / 2,
                           e["pos"][0] + e["size"][0] / 2, e["pos"][1] + e["size"][1] / 2) for e in els if e["name"].startswith("Node – ")}

    def on_edge(p, b):
        x, y = p
        inside = b[0] - 1.5 <= x <= b[2] + 1.5 and b[1] - 1.5 <= y <= b[3] + 1.5
        return inside and min(abs(x - b[0]), abs(x - b[2]), abs(y - b[1]), abs(y - b[3])) <= 1.5
    ribbons = [e for e in els if e["name"].startswith("Flow – ")]
    assert len(ribbons) == 7 and all(e["k"] == "path" and e["closed"] and e["opacity"] < 60 for e in ribbons)
    for e in ribbons:
        a, b = e["name"][7:].split(" → ")
        pts, n = e["points"], len(e["points"]) // 2
        assert on_edge(pts[0], box[a]) and on_edge(pts[-1], box[a]), (e["name"], pts[0], box[a])        # a forrás széléről
        assert on_edge(pts[n - 1], box[b]) and on_edge(pts[n], box[b]), (e["name"], pts[n - 1], box[b])  # a cél szélére
        for x, y in pts:                                         # dobozon nem fut át (az átugró folyam sem)
            assert not any(b_[0] + 2 < x < b_[2] - 2 and b_[1] + 2 < y < b_[3] - 2 for b_ in box.values()), (e["name"], x, y)
    src, dst = box["Uniós"], box["Távhő"]
    if fmt == "vertical":                                      # állóban lefelé folyik (ott nagyobb a betű / hosszabb a szalag)
        assert src[3] < dst[1]
    else:
        assert src[2] < dst[0]
    t = {e["name"]: e["t0"] for e in els if "t0" in e}
    assert t["Node – Uniós"] < t["Flow – Uniós → Energetika"] < t["Node – Energetika"] < t["Flow – Energetika → Távhő"]


def test_a_race_layer_plays_within_its_own_length_and_a_short_one_is_said():
    """Rétegként (saját felvételen) a verseny a RÉTEG hosszába fér — 10-17: 12 s-os alapidővel számolt, a 6 s-os rétegben
    az utolsó év nem jelent meg; a túl rövid réteg a check-ben szól."""
    from app.tools import video
    ch, _ = video._chart_from_plan(CHARTS["race"], SRC)
    layers = video.layers_from([{"at": 1, "dur": 4, "type": "chart", "chart": ch}], SRC, [])
    comps, rows, problems = video.layer_comps(layers, 1920, 1080, 30.0, "hu", [0.95, 0.65, 0.25], 1000)
    periods = [e for e in comps[0]["elements"] if e["name"].startswith("Period ")]
    assert periods and max(e["t0"] for e in periods) < 4.0                     # az utolsó év is a rétegen belül jön
    assert any("needs about" in p and "dur" in p for p in problems)
    ok = video.layers_from([{"at": 1, "dur": 9, "type": "chart", "chart": ch}], SRC, [])
    assert not [p for p in video.layer_comps(ok, 1920, 1080, 30.0, "hu", [0.95, 0.65, 0.25], 1000)[2] if "needs about" in p]
