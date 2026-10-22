"""Gyors végső render a saját felvételre tett grafikához (10-22, Kommandant: „Mennyi idő lesz egy 1 órás vidi vágása,
effektezése?” — a régi út a teljes kompozíciót kockánként rajzolta: 2 perc 325 s, 1,6 GB köztes fájl).

- a grafikás szakaszok: összevonás, kockahatár, felső korlát;
- a végig látható rétegek ismétlődése (bevezetés + kör), és a lyukak (a szakaszok) kihagyása;
- a felirat ASS-ben a builder méreteivel — és a libass TÉNYLEG Intert rajzol (rossz családnévre csendben tartalék
  betűt vett: 10-22, DejaVu Sans lett a Tamás TV próbán);
- ami nem adható vissza pontosan (keverési mód, korrekciós réteg, kézzel átírt felirat), az a régi úton marad;
- végponttól végpontig: a gyors és a teljes út képe egyezik (a feliratsáv fölött).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.tools import fast_render as fr


def test_spans_merge_snap_and_cap():
    out = fr.spans([(1.0, 2.0), (2.3, 3.0), (5.0, 6.0), (-1, 0.5), (9.9, 12)], dur=10.0, fps=25)
    assert out == [(0.0, 0.52), (1.0, 3.0), (5.0, 6.0), (9.88, 10.0)]         # 0,3 s szünet: egy szakasz; kockahatár, a vég a videóé
    assert fr.spans([(0.01, 0.99)], dur=5, fps=25) == [(0.0, 1.0)]             # kifelé kockahatárra
    many = [(k * 2.0, k * 2.0 + 1) for k in range(10)]
    assert len(fr.spans(many, dur=30, fps=25, cap=4)) == 4


def test_cycle_finds_intro_and_period():
    assert fr.cycle(list("abc" + "xy" * 5)) == (3, 2)
    assert fr.cycle(list("ab") + ["s"] * 6) == (2, 1)                          # bevezetés után álló kép
    assert fr.cycle([str(i) for i in range(20)]) is None                        # sosem ismétlődik
    assert fr.cycle(list("abxyx")) is None                                      # egy kör nem bizonyíték


def test_enable_expression_skips_the_graphics_parts():
    e = fr.outside(3.5, 100, [(10, 20), (200, 210), (50, 60)])
    assert e.startswith("gte(t,3.5000)*lt(t,100.0000)") and "between(t,10.0000" in e and "between(t,50.0000" in e
    assert "200.0000" not in e                                                  # a tartományon kívüli lyuk nem kell


def _info(**over):
    layers = [
        {"i": 1, "name": "Caption 1", "a": 0.5, "b": 2.0, "on": True, "video": True, "nul": False, "adj": False, "keys": [], "fx": 0,
         "normal": True, "text": "Hello there"},
        {"i": 2, "name": "Caption box 1", "a": 0.5, "b": 2.0, "on": True, "video": True, "nul": False, "adj": False, "keys": [], "fx": 0,
         "normal": True},
        {"i": 3, "name": "Layer L1 – chart", "a": 3, "b": 5, "on": True, "video": True, "nul": False, "adj": False, "keys": [], "fx": 0,
         "normal": True},
        {"i": 4, "name": "Layer L2 – graphic", "a": 0, "b": 60, "on": True, "video": True, "nul": False, "adj": False, "keys": [],
         "fx": 0, "normal": True},
        {"i": 5, "name": "Footage", "a": 0, "b": 60, "on": True, "video": True, "nul": False, "adj": False, "keys": [0, 10, 10.5, 11],
         "fx": 0, "normal": True, "file": "footage.mp4", "t0": [[100, 100]], "t1": [[100, 100]],
         "kv": [["ADBE Scale", 0, [100, 100]], ["ADBE Scale", 10, [100, 100]], ["ADBE Scale", 10.5, [130, 130]],
                ["ADBE Scale", 11, [100, 100]]]},
    ]
    for i, patch in over.items():
        layers[int(i) - 1].update(patch)
    return {"W": 640, "H": 360, "fps": 25, "dur": 60, "layers": layers, "comp": "Video"}


def test_classify_sorts_layers_and_refuses_what_it_cant_match():
    spec = {"kind": "overlay", "base": {"asset": "footage.mp4"}}
    k = fr.classify(_info(), spec)
    assert k["ok"] and k["footage"]["i"] == 5 and [x["i"] for x in k["captions"]] == [1, 2]
    assert [x["i"] for x in k["persistent"]] == [4] and [x["i"] for x in k["short"]] == [3]
    assert fr.spans(k["opaque"], 60, 25) == [(9.96, 11.04)]                    # csak ahol el van mozdítva (a 0 s-os kulcs nem)
    assert not fr.classify(_info(**{"3": {"normal": False, "blend": "Multiply"}}), spec)["ok"]
    assert not fr.classify(_info(**{"3": {"adj": True}}), spec)["ok"]
    assert not fr.classify(_info(**{"5": {"fx": 1}}), spec)["ok"]
    assert fr.classify(_info(), {"kind": "make"})["why"] == fr.NOT_OVERLAY
    rows = [{"t0": 0.5, "t1": 2.0, "text": "Hello there"}]
    assert fr.captions_match(k["captions"], rows)
    assert not fr.captions_match(k["captions"], [{**rows[0], "text": "Hello, there"}])    # kézzel átírt felirat
    assert not fr.captions_match(k["captions"], [{**rows[0], "t1": 2.4}])


def test_ass_has_the_bar_the_font_and_word_highlights():
    subs = [{"start": 0.5, "end": 3.0, "text": "Ez a próba remélhetően működőképes lesz", "words": [0.5, 0.8, 1.0, 1.5, 2.2, 2.6]}]
    ass = fr.ass_subtitles(subs, "highlight", 1920, 1080, 10, [1, 0.42, 0])
    assert "PlayResX: 1920" in ass and "Inter Semi Bold" in ass
    bars = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 0,")]
    words = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 1,")]
    assert len(bars) == 1 and "\\p1}m " in bars[0]                              # a lekerekített sáv rajzként
    assert len(words) == 6                                                      # szavanként (az első szó a darab elején indul)
    assert words[3].count("\\1c&H006BFF&") == 1 and words[3].split("\\1c&H006BFF&}")[1].startswith("remélhetően")
    box = fr.ass_subtitles(subs, "box", 1920, 1080, 10, [1, 0.42, 0])
    assert len([ln for ln in box.splitlines() if ln.startswith("Dialogue: 1,")]) == 1
    bold = fr.ass_subtitles(subs, "bold", 1080, 1920, 10, [1, 0.42, 0])
    assert "Dialogue: 0," not in bold and "REMÉLHETŐEN" in bold


@pytest.mark.skipif(not shutil.which("ffmpeg") or not Path(fr.INTER_DIR, "Inter-SemiBold.otf").exists(),
                    reason="ffmpeg with libass and the Inter font needed")
def test_libass_really_draws_inter_at_the_builder_size(tmp_path):
    from PIL import Image, ImageFont
    line = "Ez a próba remélhetően működőképes lesz"
    ass = fr.ass_subtitles([{"start": 0, "end": 1, "text": line}], "box", 1920, 1080, 1, [1, 0.4, 0])
    ass = "\n".join(ln for ln in ass.splitlines() if not ln.startswith("Dialogue: 0,"))   # csak a betű, sáv nélkül
    (tmp_path / "t.ass").write_text(ass, "utf-8")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=black:s=1920x1080:d=1", "-vf",
                    f"ass=t.ass:fontsdir={fr.INTER_DIR}", "-frames:v", "1", "out.png"], cwd=tmp_path, check=True)
    bb = Image.open(tmp_path / "out.png").convert("L").point(lambda v: 255 if v > 80 else 0).getbbox()
    em = ImageFont.truetype(f"{fr.INTER_DIR}/Inter-SemiBold.otf", round(1080 * 0.038)).getbbox(line)
    assert abs((bb[2] - bb[0]) / (em[2] - em[0]) - 1) < 0.03, (bb, em)         # tartalék betűnél ~10%-kal szélesebb


# ---------- végponttól végpontig, EffectCraft-tal ----------

def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


def _frame(path: Path, t: float, out: Path) -> Path:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1", str(out)], check=True)
    return out


def _psnr(a: Path, b: Path, h: int) -> float:
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", str(a), "-i", str(b), "-filter_complex",
                        f"[0:v]crop=iw:{h}:0:0[x];[1:v]crop=iw:{h}:0:0[y];[x][y]psnr", "-f", "null", "-"],
                       capture_output=True, text=True)
    import re
    m = re.search(r"average:(inf|[0-9.]+)", r.stderr)
    return 99.0 if m and m[1] == "inf" else float(m[1]) if m else 0.0


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI (EFFECTCRAFT_CLI) and ffmpeg needed")
def test_fast_and_full_render_look_the_same(tmp_path, put_file, monkeypatch):
    """Egyszínű felvételen: a grafika a két úton ugyanaz. (Mintás felvételen a régi út maga tér el az eredetitől: 10-22,
    testsrc2-n 23–24 dB, mert az EffectCraft újrarajzolja a felvételt — a gyors út 48 dB, lásd a következő tesztet.)"""
    from app.storage import output_path
    from app.tools import video
    monkeypatch.setattr(fr, "PERSISTENT_S", 4.0)                                 # a 8 s-os próbán a vízjel „végig látható”
    monkeypatch.setattr(fr, "PROBE_S", 5.0)                                      # és a minta rövidebb nála
    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x3a5a7a:size=640x360:rate=25", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=48000", "-t", "8", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(clip)], check=True)
    fid = put_file(clip)
    layers = [{"at": 0.5, "until": 8, "type": "graphic", "backdrop": 0, "elements": [
                  {"shape": "circle", "x": 0.05, "y": 0.08, "r": 0.02, "fill": "red", "anim": "pulse"},
                  {"shape": "text", "text": "TV", "x": 0.12, "y": 0.08, "size": 40, "weight": "bold", "color": "white"}]},
              {"at": 1, "dur": 2, "type": "lower_third", "name": "Anna Kovács", "role": "Mayor"},
              {"at": 4, "dur": 2, "type": "chart", "chart": {"type": "column", "title": "Budget", "unit": "bn Ft",
                                                             "items": [{"label": "2024", "value": 44.8}, {"label": "2025", "value": 47.5}]}}]
    subs = [{"start": 0.3, "end": 3.5, "text": "The city budget grew again this year"},
            {"start": 3.6, "end": 7.5, "text": "It was 47.5 billion forints"}]
    body = {"file_id": fid, "layers": layers, "text": "Budget: 44.8 bn Ft in 2024 and 47.5 bn Ft in 2025.", "subtitles": subs,
            "caption_style": "highlight", "camera": [{"at": 6, "zoom": 1.3, "dur": 0.4}, {"at": 7, "zoom": 1, "dur": 0.4}]}
    fast = asyncio.run(video.overlay(dict(body)))
    assert fast["video"].get("render_path"), fast["check"]
    assert any(v.startswith("Fast render:") for v in fast["check"]["verified"]), fast["check"]["verified"]
    assert any("drawn once" in v for v in fast["check"]["verified"])           # a vízjel: egyszer, ismételve
    monkeypatch.setenv("VIDEO_FAST_RENDER", "0")
    full = asyncio.run(video.overlay(dict(body)))
    assert not full["video"].get("render_path")
    a, b = output_path(fast["video"]["file_id"]), output_path(full["video"]["file_id"])
    probe = lambda p: json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",   # noqa: E731
                                                 "-show_format", str(p)], capture_output=True, text=True).stdout)
    pa, pb = probe(a), probe(b)
    assert abs(float(pa["format"]["duration"]) - float(pb["format"]["duration"])) < 0.1
    assert [s["codec_type"] for s in pa["streams"]] == ["video", "audio"]
    for t in (0.2, 1.5, 4.6, 6.5, 7.6):                                         # bevezetés, alsó sáv, ábra, ráközelítés, vég
        p = _psnr(_frame(a, t, tmp_path / f"a{t}.png"), _frame(b, t, tmp_path / f"b{t}.png"), 285)   # a feliratsáv fölött
        assert p > 30, (t, p)


def test_moved_footage_spans_come_from_the_key_values():
    """10-22, 30 perces próba: a ráközelítés 0 s-os kezdőkulcsa miatt 0–12 percet rajzolt újra — a fájl túllépte a 2 GiB-et."""
    f = {"kv": [["ADBE Scale", 0, [100, 100]], ["ADBE Scale", 720, [100, 100]], ["ADBE Scale", 720.6, [120, 120]],
                ["ADBE Scale", 726, [120, 120]], ["ADBE Scale", 726.6, [100, 100]],
                ["ADBE Position", 0, [960, 540]], ["ADBE Position", 720, [960, 540]], ["ADBE Position", 720.6, [900, 500]],
                ["ADBE Position", 726, [900, 500]], ["ADBE Position", 726.6, [960, 540]]]}
    assert fr.spans(fr.moved(f, 1800.0), 1800.0, 30) == [(720.0, 726.6)]
    held = {"kv": [["ADBE Scale", 0, [100, 100]], ["ADBE Scale", 100, [100, 100]], ["ADBE Scale", 101, [150, 150]]]}
    assert fr.moved(held, 300.0)[-1] == (101.0, 300.0)                          # a végéig nagyítva marad


def test_long_parts_are_split_to_stay_under_the_file_limit():
    assert fr.part_seconds(1920, 1080, 30) == 60.0 and fr.part_seconds(3840, 2160, 30) < 20
    parts = fr.split([(0.0, 150.0), (200.0, 210.0)], 60.0, 25)
    assert parts[0] == (0.0, 50.0) and len(parts) == 4 and parts[-1] == (200.0, 210.0)
    assert all(b - a <= 60 for a, b in parts) and parts[2][1] == 150.0


def test_the_full_render_of_a_long_project_goes_in_pieces(tmp_path):
    """A régi út is: a köztes ProRes LT 1080p-n ~2,5 percenként éri el a renderelő 2 GiB-os fájlkorlátját."""
    from app.tools import video
    def proj(secs: float) -> Path:
        p = tmp_path / f"p{secs:g}.ecproj"
        p.write_text(json.dumps({"items": {"1": {"name": "Video", "kind": {
            "type": "Comp", "width": 1920, "height": 1080, "frame_rate": {"num": 30000, "den": 1001},
            "duration": int(secs * video.TICKS_PER_S), "layers": []}}}}), "utf-8")
        return p
    assert video._master_pieces(proj(50)) == []
    pieces = video._master_pieces(proj(1800))
    assert len(pieces) == 31 and pieces[0][0] == 0 and abs(pieces[-1][1] - 1800) < 1e-6
    assert all(b - a <= 60 for a, b in pieces)


def test_chunks_and_window_graph():
    assert fr.chunks(60.0, 25) == [(0.0, 60.0)]                                 # rövid: egy menet
    cs = fr.chunks(1800.0, 30, n=4)
    assert len(cs) == 4 and cs[0][0] == 0 and cs[-1][1] == 1800.0 and all(b > a for a, b in cs)
    items = [{"name": "cycle.mov", "a": 3.0, "b": 1790.0, "period": 2.0, "enable": "1", "loop": True},
             {"name": "span-000.mov", "a": 440.0, "b": 460.0, "enable": "between(t,440,460)", "loop": False},
             {"name": "span-001.mov", "a": 1000.0, "b": 1010.0, "enable": "between(t,1000,1010)", "loop": False}]
    ins, g = fr.window_graph("f.mp4", 450.0, 900.0, items, None, None, chunked=True)
    assert ins[:6] == ["-ss", "450.0000", "-t", "450.0000", "-i", "f.mp4"]
    assert ["-stream_loop", "-1", "-ss", "1.0000", "-itsoffset", "450.0000", "-i", "cycle.mov"] == ins[6:14]   # (450−3) mod 2
    assert ["-ss", "10.0000", "-itsoffset", "450.0000", "-i", "span-000.mov"] == ins[14:20]                 # a közepéből
    assert "span-001.mov" not in ins                                            # az ablakon kívüli réteg nem kell
    assert g.startswith("[0:v]setpts=PTS+450.0000/TB") and "setpts=PTS-450.0000/TB" in g


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI (EFFECTCRAFT_CLI) and ffmpeg needed")
def test_parts_encoded_at_once_match_one_pass(tmp_path, put_file, monkeypatch):
    from app.storage import output_path
    from app.tools import video
    monkeypatch.setattr(fr, "PERSISTENT_S", 4.0)
    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=48000", "-t", "12", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(clip)], check=True)
    fid = put_file(clip)
    layers = [{"at": 0.5, "until": 12, "type": "graphic", "backdrop": 0, "elements": [  # (a felvétel hűsége: lent)
                  {"shape": "circle", "x": 0.05, "y": 0.08, "r": 0.02, "fill": "red", "anim": "pulse"}]},
              {"at": 2.5, "dur": 4, "type": "lower_third", "name": "Anna Kovács", "role": "Mayor"}]
    subs = [{"start": 0.3, "end": 5.9, "text": "One sentence across the first cut"}, {"start": 6.1, "end": 11.5, "text": "And another one"}]
    res = asyncio.run(video.overlay({"file_id": fid, "layers": layers, "subtitles": subs, "render": False}))
    monkeypatch.setattr(fr, "CHUNK_MIN_S", 1e9)
    one = asyncio.run(video.render(res["file_id"]))
    monkeypatch.setattr(fr, "CHUNK_MIN_S", 1.0)
    monkeypatch.setattr(fr.os, "cpu_count", lambda: 12)                         # → 3 szelet
    many = asyncio.run(video.render(res["file_id"]))
    assert any("Encoded in 3 parts" in v for v in many["check"]["verified"]), many["check"]["verified"]
    a, b = output_path(one["file_id"]), output_path(many["file_id"])
    count = lambda p: subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",   # noqa: E731
                                      "stream=nb_read_frames", "-of", "csv=p=0", str(p)], capture_output=True, text=True).stdout.strip()
    assert count(a) == count(b) == "300"
    for t in (0.2, 3.9, 4.1, 7.9, 8.1, 11.8):                                   # a szelethatárok (4 s, 8 s) körül is
        p = _psnr(_frame(a, t, tmp_path / f"o{t}.png"), _frame(b, t, tmp_path / f"m{t}.png"), 360)
        assert p > 35, (t, p)
    # a felvétel hűsége: ahol nincs rajta semmi, a gyors út az eredetit adja (a régi út itt 23–24 dB volt)
    # (jelöletlen SD-felvétel: BT.601-ként olvasva, BT.709-re átszámolva — a képe RGB-ben ugyanaz)
    assert _psnr(_frame(a, 0.2, tmp_path / "f.png"), _frame(clip, 0.2, tmp_path / "c.png"), 360) > 40
