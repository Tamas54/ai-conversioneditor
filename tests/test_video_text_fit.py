"""1.3 (OMLETT, 10-19): a saját grafika szövegei — alapból nem tördel, amíg a képkocka ~0,92-ébe elfér; a check a
renderer MÉRT szövegdobozaiból szól az ütközésről, a dobozból (rect) kilógásról és a váratlan tördelésről.
A 10-19-i első render három rétege (rendkivuli, tudta, hir3) szintetikusan, a main rejtett 0,6-os dobozával."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

W, H = 1080, 1920


def _inter() -> bool:
    return Path("/usr/share/fonts/opentype/inter/Inter-Bold.otf").exists()


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


# a 10-19-i rétegek: „RENDKÍVÜLI KIADÁS” 66 px-es cím az alcím fölött; a „Tudta?” kártya két szövege; a hírszalag
# szövege egy 0,03 magas sávban. w = 0.6: a main rejtett alapdoboza (ezzel tört két sorba mindhárom)
def omlett_layers(w: object = 0.6) -> list[dict]:
    ww = {} if w is None else {"w": w}
    return [
        {"id": "rendkivuli", "at": 0.2, "dur": 3.5, "type": "graphic", "elements": [
            {"shape": "text", "text": "RENDKÍVÜLI KIADÁS", "x": 0.5, "y": 0.1, "size": 66, "weight": "bold", "color": "red", **ww},
            {"shape": "text", "text": "Tamás TV – az omlett-hadművelet", "x": 0.5, "y": 0.135, "size": 40, **ww}]},
        {"id": "tudta", "at": 0.2, "dur": 3.5, "type": "graphic", "elements": [
            {"shape": "rect", "x": 0.5, "y": 0.6, "w": 0.9, "h": 0.12, "fill": "dark", "opacity": 0.85},
            {"shape": "text", "text": "Tudta? A francia omlett belül krémes marad", "x": 0.5, "y": 0.57, "size": 44,
             "weight": "bold", **ww},
            {"shape": "text", "text": "A titok: kis láng, sok vaj, és türelem", "x": 0.5, "y": 0.6, "size": 38, **ww}]},
        {"id": "hir3", "at": 0.2, "dur": 3.5, "type": "graphic", "elements": [
            {"shape": "rect", "x": 0.5, "y": 0.95, "w": 1.0, "h": 0.03, "fill": "accent"},
            {"shape": "text", "text": "FRISS: a szakács szerint a vaj a titok, a tojás pedig szobahőmérsékletű legyen",
             "x": 0.5, "y": 0.947, "size": 30, "weight": "semibold", **ww}]},
    ]


@pytest.mark.skipif(not _inter(), reason="the Inter font (fonts-inter) is needed")
def test_text_without_w_stays_on_one_line_while_it_fits():
    """A „RENDKÍVÜLI KIADÁS” 66 px-en ~663 px — a rejtett 0,6-os (648 px) doboz két sorba törte; most a doboz kiszélesedik
    (legfeljebb a képkocka 0,92-éig), a rövid szöveg doboza marad 0,6 (a régi kép nem változik)."""
    from app.tools.video import _graphic_elements, _graphic_from_plan, _text_px
    ch, _ = _graphic_from_plan({"type": "graphic", "elements": [
        {"shape": "text", "text": "RENDKÍVÜLI KIADÁS", "x": 0.5, "y": 0.1, "size": 66, "weight": "bold"},
        {"shape": "text", "text": "Rövid", "x": 0.5, "y": 0.3, "size": 40},
        {"shape": "text", "text": "Balra", "x": 0.4, "y": 0.4, "size": 40, "align": "left"},
        {"shape": "text", "text": "x " * 200, "x": 0.5, "y": 0.5, "size": 40}]}, "")
    big, short, left, long_ = [e for e in _graphic_elements(ch, W, H, [1, 0, 0]) if e["k"] == "text"]
    need = _text_px("RENDKÍVÜLI KIADÁS", big["size"], "bold")
    assert 640 < need < 700                                           # Inter Bold, a renderer betűje (ő/ű is mérve)
    assert big["box"][0] >= need + 10 and big["box"][0] <= 0.92 * W + 1
    assert short["box"][0] == round(0.6 * W, 1) and short["pos"][0] == round(0.5 * W, 1)
    assert left["box"][0] == round(0.6 * W, 1) and left["pos"][0] == round(0.4 * W, 1)
    assert long_["box"][0] <= 0.92 * W + 1                           # ami így sem fér el, az tördel (a check szól)
    assert _text_px("őű", 40, "regular") > 0 and _text_px("ŐŰ", 40, "bold") > _text_px("OU", 40, "bold") * 0.9


def test_text_report_names_layer_element_and_a_fix():
    """A renderer kimenetéből (TEXT_BOX: a kirajzolt szöveg tintadoboza) — motor nélkül, szintetikus mérésekkel."""
    from app.tools import video
    ls = video.layers_from(omlett_layers(), "", [])
    D = {"W": W, "H": H, "scenes": []}
    D["layers"], _, _ = video.layer_comps(ls, W, H, 6.0, "hu", [1, 0, 0], H - 60)
    probes = {e["probe"]: e for ly in D["layers"] for e in ly["elements"] if e.get("probe")}
    assert set(probes) == {"rendkivuli#0", "rendkivuli#1", "tudta#1", "tudta#2", "hir3#1"}
    # a main renderje: a cím két sorban (142 px magas tinta), az alcím alatta; a Tudta két szövege egymáson;
    # a hírszalag két sorban (lóg ki a 58 px-es sávból)
    out = "\n".join([
        'TEXT_BOX ["rendkivuli#0", -196.1, -117.5, 392.1, 142.5]',
        'TEXT_BOX ["rendkivuli#1", -280.0, -63.9, 560.0, 31.1]',
        'TEXT_BOX ["tudta#1", -302.7, -70.1, 604.3, 86.8]',
        'TEXT_BOX ["tudta#2", -290.0, -60.1, 580.0, 70.0]',
        'TEXT_BOX ["hir3#1", -310.0, -47.8, 620.0, 61.0]'])
    f = video._text_report(out, D)
    assert len(f) >= 3, f
    rk = next(x for x in f if x.startswith("Layer rendkivuli"))
    assert "element 1" in rk and "2 lines" in rk and "element 2" in rk and '"w": 0.' in rk
    assert any(x.startswith("Layer tudta") and "runs into" in x for x in f)
    hr = next(x for x in f if x.startswith("Layer hir3"))
    assert "rect" in hr and ('"size":' in hr or '"w":' in hr)
    # a jó elrendezés csendes: egy sor, a dobozán belül, nem ér máshoz
    ok = 'TEXT_BOX ["rendkivuli#0", -330, -117.5, 661, 63.3]\nTEXT_BOX ["rendkivuli#1", -214, 0, 428, 31.1]'
    assert not [x for x in video._text_report(ok, {**D, "layers": D["layers"][:1]}) if "rendkivuli" in x]


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_omlett_first_render_layers_give_named_findings(tmp_path, put_file):
    """Elfogadás: a 10-19-i első render rétegei (rendkivuli, tudta, hir3) — a check eddig „ok” volt, üres findings-szel."""
    from app.tools import video
    clip = tmp_path / "omlett.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate=10", "-t", "4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True)
    res = asyncio.run(video.overlay({"file_id": put_file(clip), "layers": omlett_layers()}))
    chk = res["check"]
    named = [f for f in chk["findings"] if f.startswith(("Layer rendkivuli", "Layer tudta", "Layer hir3"))]
    assert chk["status"] == "needs_check" and len(named) >= 3, chk
    assert all(("w\":" in f or "size\":" in f or "\"y\":" in f) for f in named)
    # ugyanez w nélkül (az új alapdoboz): a cím egy sorban marad — rá már nincs finding
    res2 = asyncio.run(video.overlay({"file_id": put_file(clip), "layers": omlett_layers(None), "render": False}))
    assert not [f for f in res2["check"]["findings"] if f.startswith("Layer rendkivuli")], res2["check"]


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_omlett_regression_25_layers_15_subtitles_one_collision(tmp_path, put_file):
    """Az omlett-teszt 2. pontja: 25 réteg + 15 felirat, köztük egy szándékosan ütköző szöveg → needs_check, nevesítve."""
    from app.tools import video
    clip = tmp_path / "o.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=10", "-t", "30",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True)
    kinds = [{"type": "lower_third", "name": "Tamás", "role": "szakács"}, {"type": "source", "text": "Forrás: Tamás TV"},
             {"type": "chapter", "text": "Második lépés"},
             {"type": "graphic", "elements": [{"shape": "arrow", "from": [0.2, 0.3], "to": [0.5, 0.5]}]}]
    layers = [{"id": f"L{i + 1}", "at": i * 1.1, "dur": 1.0, **kinds[i % len(kinds)]} for i in range(24)]
    layers.append({"id": "utkozik", "at": 27, "dur": 2, "type": "graphic", "elements": [
        {"shape": "text", "text": "Jöhet a tálalás", "x": 0.5, "y": 0.3, "size": 70},
        {"shape": "text", "text": "Voilà!", "x": 0.5, "y": 0.31, "size": 60}]})
    subs = [{"start": i * 2, "end": i * 2 + 1.8, "text": f"Felirat {i + 1}: sonkával, fakanállal."} for i in range(15)]
    res = asyncio.run(video.overlay({"file_id": put_file(clip), "layers": layers, "subtitles": subs, "render": False}))
    assert len(res["layers"]) == 25 and res["subtitles"]["count"] == 15
    hit = [f for f in res["check"]["findings"] if f.startswith("Layer utkozik, element 1") and "runs into element 2" in f]
    assert res["check"]["status"] == "needs_check" and hit, res["check"]["findings"]
