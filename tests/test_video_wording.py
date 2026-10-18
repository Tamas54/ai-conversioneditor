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


def test_table_chart_checked_and_drawn():
    """UI-próba 10-18: a videóban kért „táblázat” nem volt ábratípus."""
    from app.tools.video import chart_elements
    src = "Hétfőn 3 óra, kedden 5 óra edzés."
    ch, note = _chart_from_plan({"type": "table", "title": "Edzésnapló", "columns": ["Nap", "Óra"],
                                 "rows": [["Hétfő", "3"], ["Kedd", "5"]]}, src)
    assert ch and ch["rows"] == [["Hétfő", "3"], ["Kedd", "5"]] and "Table" in note
    assert _chart_from_plan({"type": "table", "title": "X", "columns": ["Nap", "Óra"], "rows": [["Szerda", "9"]]}, src)[0] is None
    els = chart_elements(ch, 1080, 1920, "hu", [1, 0.35, 0.2])
    cells = [e["str"] for e in els if e["k"] == "text"]
    assert {"Nap", "Óra", "Hétfő", "3", "Kedd", "5"} <= set(cells)


def test_speech_reaches_the_edit_planner():
    """A saját felvétel szerkesztése: a tervező látja, mi hangzik el (különben „amit a videóban kérek” vakon fut)."""
    from app.tools.video import _describe
    spec = {"kind": "overlay", "base": {"width": 1080, "height": 1920, "duration": 65}, "opts": {},
            "story": {"layers": [], "subtitles": [{"start": 47.5, "end": 53.3, "text": "különböző táblázatok, szép kis ábrák"}]}}
    d = _describe(spec)
    assert "what is said" in d and "táblázatok" in d and "00:47" in d


def test_table_layer_shorthand_and_timeline_label_numbers():
    """MCP-próba 10-18: {"type": "table", "columns", "rows"} rétegként „not enough usable items” lett (a rövidítés nem
    adta tovább a mezőket); a „15. mp” idővonal-címke kiesett, pedig a 15 elhangzott."""
    from app.tools.video import layers_from
    src = "15 és 18 másodperc között fontos gondolatok. Oregánó, bazsalikom."
    notes: list[str] = []
    out = layers_from([{"type": "table", "at": 5, "dur": 4, "title": "Fűszerek", "columns": ["Fűszer", "Íz"],
                        "rows": [["Oregánó", "fűszeres"], ["Bazsalikom", "édeskés"]]},
                       {"type": "timeline", "at": 15, "dur": 3, "title": "Fontos",
                        "items": [{"label": "15. mp", "text": "kezdet"}, {"label": "18. mp", "text": "vég"}]}], src, notes)
    assert [ly["chart"]["type"] for ly in out] == ["table", "timeline"], notes
    assert out[0]["chart"]["rows"][1] == ["Bazsalikom", "édeskés"]


def test_frames_grid_several_moments(tmp_path, put_file):
    """MCP files preview at=[…]: több pillanat egy képen, időcímkével és tizedes ráccsal (mutogatós felvételhez)."""
    import shutil
    import subprocess
    import pytest
    from app.tools.video import frames_grid
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg needed")
    src = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=360x640:rate=30:duration=3",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    fid = put_file(src, "mp4")
    jpg, shots = frames_grid(fid, [0.5, 1.5, 2.5, 99])
    assert jpg[:2] == b"\xff\xd8" and [s["at"] for s in shots] == [0.5, 1.5, 2.5]      # a videón túli pillanat kimarad


def test_upright_skips_undecodable_audio(monkeypatch, tmp_path):
    """10-18, IMG_9271.MOV: az elforgatott iPhone-felvétel újrakódolása „-map 0:a”-val a térhang-sávot (APAC) is vitte →
    „Decoder (codec none) not found”. Csak az első dekódolható hangsáv mehet."""
    import asyncio
    from app.convert import engines
    from app.tools import video as V
    calls = []

    async def fake_probe(_p):
        return {"audio_stream": 0}

    async def fake_run(*a, **k):
        calls.append(a)
        return b"", b""
    monkeypatch.setattr(V, "probe_movie", fake_probe)
    monkeypatch.setattr(engines, "run", fake_run)
    monkeypatch.setattr(V, "write_meta", lambda *a, **k: None)
    asyncio.run(V._upright(tmp_path / "x.mov"))
    args = list(calls[0])
    assert "0:a?" not in args and args[args.index("0:v:0") + 2] == "0:a:0"


def test_graphic_opacity_outline_draw_and_bar_grow():
    """10-18, mutogatós videó: 0–1 opacity → 5 % (láthatatlan oszlopok); a téglalap-keret „draw”-ja rögtön ott volt
    (a builder csak útvonalon ismeri); a „grow” középről nőtt, nem az oszlop talpáról."""
    from app.tools.video import _graphic_elements, _graphic_from_plan
    ch, _ = _graphic_from_plan({"type": "graphic", "elements": [
        {"shape": "rect", "x": 0.1, "y": 0.6, "w": 0.06, "h": 0.4, "fill": "accent", "opacity": 0.9, "anim": "grow", "at": 2},
        {"shape": "rect", "x": 0.5, "y": 0.9, "w": 0.4, "h": 0.04, "fill": "#000000", "opacity": 55},
        {"shape": "rect", "x": 0.3, "y": 0.4, "w": 0.1, "h": 0.7, "fill": "none", "stroke": "white", "anim": "draw", "at": 15.7}]}, "")
    bar, strip, outline = _graphic_elements(ch, 1080, 1920, [1, 0, 0])
    assert bar["opacity"] == 90 and bar["grow"] == "up" and bar["t0"] == 2
    assert strip["opacity"] == 55
    assert outline["k"] == "path" and outline["anim"] == "draw" and outline["t0"] == 15.7 and outline["fill"] is None


def test_video_cut_in_from_another_clip(tmp_path):
    """10-18: „a másik vidiből valamit bele lehet vágni?” — a kép-elem / teljes képes réteg egy VIDEÓ file_id-ját is
    viszi: a kért rész némán, egyenesen; a builder a megjelenésekor indítja."""
    import asyncio
    import shutil
    import subprocess
    import pytest
    from app.tools.video import _clip_sounds, _clip_span, _cut_clip, _cut_sound, _graphic_elements, _graphic_from_plan, _movie_src
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg needed")
    assert _movie_src("abc.MOV") == "abc.MOV" and _movie_src({"file_id": "x.mp4"}) == "x.mp4"
    assert _movie_src("pic.jpg") is None and _movie_src("https://e.com/a.mp4") is None
    src = tmp_path / "other.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=4", "-f", "lavfi",
                    "-i", "sine=duration=4", "-shortest", "-pix_fmt", "yuv420p", str(src)], check=True)
    assert _clip_span([1.0, 2.5]) == (1.0, 1.5) and _clip_span([1.0, None], 3.3) == (1.0, 3.3) and _clip_span(None) == (0.0, 60.0)
    out = asyncio.run(_cut_clip(src, _clip_span([1.0, 2.5]), tmp_path / "cut.mp4"))
    pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(out)],
                        capture_output=True, text=True, check=True).stdout
    import json
    d = json.loads(pr)
    assert [s["codec_type"] for s in d["streams"]] == ["video"] and abs(float(d["format"]["duration"]) - 1.5) < 0.2
    ch, _ = _graphic_from_plan({"type": "graphic", "elements": [{"shape": "image", "image": "other.mov", "clip": [1, 2.5]}]}, "")
    assert ch["elements"][0]["clip"] == [1.0, 2.5]
    ch["elements"][0]["file"] = "gfx-01-L6.mp4"
    el = _graphic_elements(ch, 1080, 1920, [1, 0, 0])[0]
    assert el["movie"] is True and el["t0"] == 0                    # a bevágás a réteggel együtt indul
    # 10-18: „a bevágott vidinek nem volt hangja” — a hangja külön sáv, a réteg idejére; teljes képen a felvételé helyett
    assert asyncio.run(_cut_sound(src, (1.0, 1.5), tmp_path / "cut.wav")) and (tmp_path / "cut.wav").stat().st_size > 1000
    ly = {"at": 29.4, "dur": 3.3, "chart": {"elements": [{**ch["elements"][0], "sound": "replace", "sound_file": "gfx-01-L6.wav",
                                                           "clip_len": 3.4}]}}
    assert _clip_sounds([ly]) == [{"file": "gfx-01-L6.wav", "t0": 29.4, "dur": 3.3, "mode": "replace"}]
    from app.tools import video as V
    full = V._layer_from({"at": 29.4, "dur": 3.3, "type": "fullscreen", "image": "other.mov", "clip": [0.2, 3.6]}, "", [])
    assert full["chart"]["elements"][0]["sound"] == "replace"


def test_layer_picture_from_related_keys():
    """10-18, webes „Ask for a change”: „teljes képernyőn egy AI-val generált kép…” → a tervező
    {"type": "fullscreen", "image_query": …}-t adott → „give chart, image or text”. A rokon kulcsok a réteg képe."""
    from app.tools import video as V
    notes = []
    ly = V._layer_from({"at": 2.2, "dur": 3.1, "type": "fullscreen", "image_prompt": "a busy 15th-century harbour"}, "", notes)
    assert ly["kind"] == "image" and ly["image"] == {"generate": "a busy 15th-century harbour"}
    ly = V._layer_from({"at": 2.2, "dur": 3.1, "type": "fullscreen", "image_query": "caravel harbour"}, "", notes)
    assert ly["image"] == {"stock": "caravel harbour"}
    assert '{"generate": "an English description"}' in V._EDIT_PLAN
