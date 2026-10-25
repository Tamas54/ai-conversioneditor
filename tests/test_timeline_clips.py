"""Klipek beszúrása a saját felvételbe (10-23, Kommandant: „a második kis videó 2 percesét vágjuk be a 16–19. perc közé
értelemszerűen”, „a harmadikat tedd a végére, csak ne tűnjön fel nagyon, hogy vágás”) — app/tools/footage.py.

- az idővonal több forrásból: beszúrás, vágás, a klip kivágása után újra egy darab a fő felvétel;
- a hely az anyagból: a fő felvétel szünetei az ablakban, a mondatvégi előnyben;
- az illesztés: a klip hangereje és színei a fő felvételéhez, hang-áttűnés, kérésre képi áttűnés;
- végponttól végpontig: a beszúrt rész a klip eredetijéből (nem a kis munkamásolatból), a rétegek csúsznak, a hang hossza
  stimmel, és a vágás kiveszi a klipet.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.tools import footage


def test_insert_then_cut_it_out_gives_the_whole_footage_back():
    k = footage.insert(None, 100.0, 40.0, {"src": "clip.mp4", "a": 2.0, "b": 12.0, "join": "smooth", "gain": 3.0}, 25)
    assert k == [[0.0, 40.0], {"a": 2.0, "b": 12.0, "src": "clip.mp4", "join": "smooth", "gain": 3.0},
                 {"a": 40.0, "b": 100.0, "join": "smooth"}]
    assert footage.length(k, 100) == 110.0
    assert footage.at(k, 45) == ("clip.mp4", 7.0) and footage.at(k, 55) == (None, 45.0)
    assert footage.to_out(k, 50) == 60.0 and footage.to_out(k, 5.0, "clip.mp4") == 43.0
    assert footage.remap(60, None, k) == 70.0
    assert footage.remap_span(35, 45, None, k) == (35.0, 40.0)            # a beszúrás kettévágja: a hosszabbik (első) fele
    assert footage.pieces(k, 38, 52) == [(0, None, 38.0, 40.0), (1, "clip.mp4", 2.0, 12.0), (2, None, 40.0, 42.0)]
    assert footage.cut(k, 100.0, [(40, 50)], 25) == [[0.0, 100.0]]        # a klip ki: újra egy darab
    end = footage.insert(k, 100.0, 110.0, {"src": "c3.mp4", "a": 0, "b": 5, "join": "dissolve"}, 25)
    assert end[-1] == {"a": 0, "b": 5, "src": "c3.mp4", "join": "dissolve"} and footage.length(end, 100) == 115.0
    assert footage.out_near(footage.cut(None, 100.0, [(10, 20)], 25), 15) == 10.0           # kivágott pont: a következő
    from app.tools.video import _cut_note                          # vágás egy klipes idővonalon: a fő felvétel hézagai
    both = footage.cut(k, 100.0, [(90, 100)], 25)
    assert "plays in 4 part(s), 1 removed" in _cut_note(k, both, 100.0)
    assert footage.whole([[0.0, 100.0]], 100.0) and not footage.whole(k, 100.0) and not footage.whole(both, 100.0)


def test_the_quick_preview_shows_where_a_clip_starts_and_ends():
    """A gyors előnézet (app/tools/video_preview.py) a beszúrt klip elejét és végét is mutatja; szerkesztéskor csak az új
    illesztést, és ahol egy klipet kivettek."""
    from app.tools import video_preview as vp
    k = footage.insert(footage.cut(None, 100.0, [(80, 90)], 25), 100.0, 40.0,
                       {"src": "clip.mp4", "a": 2.0, "b": 12.0, "join": "smooth"}, 25)
    spec = {"story": {"layers": []}, "base": {"keep": k}}
    assert vp.spans_new(spec, 100.0) == [(37.0, 43.0, "clip clip.mp4 starts"), (47.0, 53.0, "clip clip.mp4 ends"),
                                         (87.0, 93.0, "cut")]
    k2 = footage.insert(k, 100.0, 95.0, {"src": "c3.mp4", "a": 0.0, "b": 5.0, "join": "dissolve"}, 25)
    got = vp.spans_edit(spec, {"story": {"layers": []}, "base": {"keep": k2}}, {"insert": [{}]}, 105.0)
    assert got == [(92.0, 98.0, "clip c3.mp4 starts"), (97.0, 103.0, "clip c3.mp4 ends")]      # a régiek nem
    back = footage.cut(k, 100.0, [(40, 50)], 25)
    got = vp.spans_edit(spec, {"story": {"layers": []}, "base": {"keep": back}}, {"cut": [{}]}, 90.0)
    assert got == [(37.0, 43.0, "clip clip.mp4 taken out")]                                    # ott, ahol volt
    short = footage.insert(None, 100.0, 40.0, {"src": "clip.mp4", "a": 2.0, "b": 5.0, "join": "smooth"}, 25)
    picked, left = vp.pick(vp.spans_new({"story": {"layers": []}, "base": {"keep": short}}, 103.0), 103.0, fps=25)
    assert picked == [(37.0, 46.0, "clip clip.mp4 starts, clip clip.mp4 ends")] and not left   # egy rövid klip egészben


def test_insert_specs_forms_and_errors(put_file, tmp_path):
    from app.tools.video import insert_specs
    p = tmp_path / "c.mp4"
    p.write_bytes(b"x")
    fid = put_file(p)
    sp = insert_specs([{"file_id": fid, "at": {"between": ["16:00", "19:00"]}}, {"file_id": fid, "at": "end", "join": "cut",
                                                                                 "from": "00:05", "to": 9}])
    assert sp[0]["at"] == ("between", 960.0, 1140.0) and sp[0]["join"] == "smooth"
    assert sp[1]["at"] == "end" and sp[1]["from"] == 5.0 and sp[1]["to"] == 9.0
    with pytest.raises(ValueError, match="join"):
        insert_specs([{"file_id": fid, "join": "wipe"}])
    with pytest.raises(ValueError, match="isn't a video"):
        insert_specs([{"file_id": "x.png"}])
    assert insert_specs(None) == []


def test_look_match_is_bounded_and_skips_small_differences():
    assert footage.match_look({"YAVG": 120, "SATAVG": 40}, {"YAVG": 121, "SATAVG": 41}) == {}
    m = footage.match_look({"YAVG": 130, "SATAVG": 40}, {"YAVG": 60, "SATAVG": 20})
    assert m["gamma"] > 1 and m["saturation"] == 1.35                     # sötétebb, fakóbb klip: világosabb, telítettebb
    assert footage.match_look(None, {"YAVG": 1}) == {}


def _ff(*a: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *a], check=True)


def _speech(path: Path, parts: list[tuple[str, float]]) -> Path:
    """Szintetikus „beszéd”: hang (sine) és csend (anullsrc) váltakozva."""
    ins, cat = [], []
    for k, (kind, d) in enumerate(parts):
        src = f"sine=frequency=330:sample_rate=48000:duration={d}" if kind == "on" else \
            f"anullsrc=r=48000:cl=mono:d={d}"
        ins += ["-f", "lavfi", "-i", src]
        cat.append(f"[{k}:a]")
    _ff(*ins, "-filter_complex", "".join(cat) + f"concat=n={len(parts)}:v=0:a=1[a]", "-map", "[a]", str(path))
    return path


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg needed")
def test_the_place_is_the_longest_pause_at_a_sentence_end(tmp_path):
    wav = _speech(tmp_path / "s.wav", [("on", 2.0), ("off", 1.2), ("on", 2.0), ("off", 0.6), ("on", 2.0)])
    ps = asyncio.run(footage.pauses(wav, 0.0, 7.8, 0))
    assert [(round(a, 1), round(b, 1)) for a, b in ps] == [(2.0, 3.2), (5.2, 5.8)]
    info = {"audio_stream": 0}
    cues = [{"start": 0.2, "end": 1.9, "text": "Ez itt az első mondat"}, {"start": 3.3, "end": 5.1, "text": "és itt a vége."}]
    cands = asyncio.run(footage.candidates(None, wav, info, 1.0, 7.0, cues, 25))
    assert [c["sentence_end"] for c in cands] == [False, True]
    assert cands[0]["at"] == pytest.approx(2.6, abs=0.05) and cands[1]["at"] == pytest.approx(5.5, abs=0.05)
    t, why = footage.pick(cands, 1.0, 7.0, 25)
    assert t == cands[1]["at"] and why.startswith("end of a sentence, 0.6 s pause")   # a rövidebb, de mondatvégi
    t, why = footage.pick([c for c in cands if not c["sentence_end"]], 1.0, 7.0, 25)
    assert t == cands[0]["at"] and "1.2 s pause" in why
    assert footage.pick([], 1.0, 7.0, 25) == (4.0, "no pause in the window — its middle")
    # a klip néma eleje / vége levágva (0,2 s marad)
    clip = _speech(tmp_path / "c.wav", [("off", 1.5), ("on", 2.0), ("off", 1.0)])
    assert asyncio.run(footage.trim_silence(clip, info, 0.0, 4.5)) == (1.3, 3.7)


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


def _probe(p: Path) -> dict:
    return json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(p)],
                                     capture_output=True, text=True, check=True).stdout)


def _yuv(p: Path, t: float, out: Path, vf: str) -> Path:
    _ff("-ss", f"{t}", "-i", str(p), "-frames:v", "1", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "yuv420p", str(out))
    return out


def _psnr_raw(a: Path, b: Path, w: int, h: int) -> float:
    r = subprocess.run(["ffmpeg", "-v", "info", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}", "-i", str(a),
                        "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}", "-i", str(b), "-lavfi", "psnr", "-f", "null",
                        "-"], capture_output=True, text=True)
    m = re.search(r"average:(inf|[0-9.]+)", r.stderr)
    return 99.0 if m and m[1] == "inf" else float(m[1]) if m else 0.0


def _main(path: Path) -> Path:
    """A fő felvétel: 1280×720-ban tárolva, 90°-os jelöléssel (álló 720×1280), 10 kocka/s, 6 s; a hangban 2,5–3,5 s között
    szünet (egy mondat vége)."""
    wav = _speech(path.with_suffix(".wav"), [("on", 2.5), ("off", 1.0), ("on", 2.5)])
    raw = path.with_name("raw-" + path.name)
    _ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=10", "-i", str(wav), "-t", "6", "-c:v", "libx264", "-crf", "12",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(raw))
    _ff("-i", str(raw), "-c", "copy", "-metadata:s:v:0", "rotate=90", str(path))
    return path


def _clip(path: Path) -> Path:
    """A beszúrandó klip: fekvő 640×360, 25 kocka/s, 3 s, más mintával és hangosabb, folyamatos hanggal."""
    _ff("-f", "lavfi", "-i", "testsrc=size=640x360:rate=25", "-f", "lavfi", "-i", "sine=frequency=550:sample_rate=48000",
        "-t", "3", "-af", "volume=6dB", "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path))
    return path


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg needed")
def test_sources_of_different_frame_rates_are_joined_at_speed(tmp_path, put_file, monkeypatch):
    """10-25, valós anyagon: az iPhone-felvétel (30 tbr, 29,993 átlag) és egy klip concatja „VFR” lett, az ffmpeg 1 µs-os
    kockákra töltötte ki — a munkamásolat szelete 0,001× sebességgel ment, 30 perc után állt meg. Most minden forrás
    ugyanarra a kockasebességre jön: az ablak a rendes idő alatt kész, a kockaszám a hossz × fps."""
    from app.config import settings
    from app.tools import mediaprobe
    raw, main = tmp_path / "raw.mp4", tmp_path / "main.mp4"
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30", "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(raw))
    _ff("-i", str(raw), "-vf", "select='not(between(n,40,45))'", "-vsync", "vfr", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(main))                                                      # 6 kocka ki: az átlag 28,5, a tbr 30 marad
    m = asyncio.run(mediaprobe.info(main))
    assert m["fps"] < 29.5
    cid, fps = put_file(_clip(tmp_path / "clip.mp4")), float(m["fps"])
    base = {"keep": footage.insert(None, 4.0, 2.0, {"src": cid, "a": 0.0, "b": 2.0, "join": "cut"}, fps),
            "width": 320, "height": 240, "fps": fps}
    monkeypatch.setattr(settings, "VIDEO_TIMEOUT_S", 60)

    async def go() -> None:
        tl = await footage.timeline(base, main, 320, 240, fps, tmp_path)
        await footage._tl_window(tl, 1.0, 5.0, ["format=yuv420p"], [], ["-c:v", "libx264", "-preset", "ultrafast",
                                                                         "-pix_fmt", "yuv420p"], tmp_path / "w.mp4", tmp_path)
    t0 = time.time()
    asyncio.run(go())
    v = next(s for s in _probe(tmp_path / "w.mp4")["streams"] if s["codec_type"] == "video")
    assert abs(int(v["nb_frames"]) - 4.0 * fps) <= 2 and time.time() - t0 < 30, (v["nb_frames"], time.time() - t0)


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_a_clip_goes_in_at_the_pause_from_its_original_and_comes_out_again(tmp_path, put_file):
    from app.storage import output_path, read_meta
    from app.tools import video
    main, clip = _main(tmp_path / "main.mp4"), _clip(tmp_path / "clip.mp4")
    fid, cid = put_file(main), put_file(clip)
    res = asyncio.run(video.overlay({
        "file_id": fid, "brand": "none",
        "subtitles": [{"start": 0.2, "end": 2.4, "text": "Ez az első mondat."}, {"start": 3.6, "end": 5.8, "text": "És a második"}],
        "layers": [{"at": 4.5, "dur": 1.0, "type": "source", "text": "Utána"}],
        "insert": [{"file_id": cid, "at": {"between": [1, 5]}, "join": "cut"}]}))
    ins = res["inserted"][0]
    assert 2.5 <= ins["at"] <= 3.5 and ins["why"].startswith("end of a sentence"), ins
    assert any(v.startswith("Clip 1 (") and "inserted at" in v for v in res["check"]["verified"]), res["check"]
    assert any("Clip 1" in f and "no subtitles" in f for f in res["check"]["findings"])
    base = read_meta(res["file_id"])["spec"]["base"]
    T = ins["at"]
    assert len(base["keep"]) == 3 and base["duration"] == pytest.approx(9.0, abs=0.05)
    assert [(x["id"], x["at"]) for x in res["layers"]] == [("L1", 7.5)]           # a beszúrás után: 3 s-mal később
    out = output_path(res["video"]["file_id"])
    pr = _probe(out)
    assert float(pr["format"]["duration"]) == pytest.approx(9.0, abs=0.2)
    au = next(s for s in pr["streams"] if s["codec_type"] == "audio")
    assert float(au.get("duration") or 0) == pytest.approx(9.0, abs=0.25)
    # a beszúrt rész a klip eredetijéből: a középső sáv (720×405, fölötte-alatta a kitöltés) a klip képe
    # (a klip a 10 kocka/s-os videóban: ugyanaz a kocka, BT.601 → BT.709 és a méretre illesztés ugyanúgy; a helye a
    # kitöltött képen kerekítéstől függ — a legjobb illeszkedés ±4 sorban; a sáv széle nélkül)
    a = _yuv(out, T + 1.5, tmp_path / "o.yuv", "crop=640:300:40:490")
    p = _yuv(output_path(base["media_id"]), T + 1.5, tmp_path / "p.yuv", "scale=720:1280,crop=640:300:40:490")

    def best(x: Path) -> float:
        out_ = 0.0
        for y in range(49, 58):
            ref = tmp_path / f"r{y}.yuv"
            _ff("-i", str(clip), "-vf", "fps=10,select=eq(n\\,15),scale=in_color_matrix=bt601:out_color_matrix=bt709:in_range=tv:"
                f"out_range=tv,scale=720:1280:force_original_aspect_ratio=decrease,crop=640:300:40:{y}", "-frames:v", "1",
                "-f", "rawvideo", "-pix_fmt", "yuv420p", str(ref))
            out_ = max(out_, _psnr_raw(x, ref, 640, 300))
        return out_
    good, soft = best(a), best(p)
    assert good > 40 and soft < good - 6, (good, soft)                   # mérve 46,8 / 33,6: a kis munkamásolat elmosódott
    # a vágás kiveszi a klipet: a fő felvétel újra egy darab, a réteg vissza a helyére
    back = asyncio.run(video.edit(res["file_id"], {"cut": [{"from": T, "to": T + 3.0}]}, render=False))
    b2 = read_meta(back["file_id"])["spec"]["base"]
    assert "keep" not in b2 and b2["duration"] == pytest.approx(6.0, abs=0.05)
    assert [(x["id"], x["at"]) for x in back["layers"]] == [("L1", 4.5)]


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_a_smooth_join_matches_the_sound_and_a_dissolve_renders_the_same_both_ways(tmp_path, put_file, monkeypatch):
    from app.storage import output_path, read_meta
    from app.tools import video
    main, clip = _main(tmp_path / "main.mp4"), _clip(tmp_path / "clip.mp4")
    fid, cid = put_file(main), put_file(clip)
    pid = asyncio.run(video.overlay({"file_id": fid, "brand": "none", "render": False,
                                     "layers": [{"at": 0.5, "dur": 1.0, "type": "source", "text": "Eleje"}]}))["file_id"]
    body = {"insert": [{"file_id": cid, "at": "end", "join": "dissolve"}]}       # a szerkesztésben: a végére
    fast = asyncio.run(video.edit(pid, dict(body)))
    seg = read_meta(fast["file_id"])["spec"]["base"]["keep"][-1]
    assert seg["src"] == cid and seg["join"] == "dissolve" and -12 <= seg.get("gain", 0) <= -2, seg   # a hangosabb klip halkabban
    assert fast["video"].get("render_path"), fast["check"]
    monkeypatch.setenv("VIDEO_FAST_RENDER", "0")
    full = asyncio.run(video.edit(pid, dict(body)))
    fa, fb = output_path(fast["video"]["file_id"]), output_path(full["video"]["file_id"])
    for t in (6.2, 7.5):                                                          # az áttűnés közepe, és a klip
        x, y = _yuv(fa, t, tmp_path / f"a{t}.yuv", "null"), _yuv(fb, t, tmp_path / f"b{t}.yuv", "null")
        assert _psnr_raw(x, y, 720, 1280) > 30, t


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_the_quick_preview_has_the_inserted_clip_and_the_final_comes_in_the_background(tmp_path, put_file):
    """A gyors előnézet (#40) a beszúrt klip elejét és végét is megmutatja; a végleges render a háttérben, az eredetikből."""
    from app import jobs
    from app.storage import output_path
    from app.tools import video
    main, clip = _main(tmp_path / "main.mp4"), _clip(tmp_path / "clip.mp4")
    fid, cid = put_file(main), put_file(clip)

    async def go() -> None:
        res = await video.overlay({"file_id": fid, "brand": "none", "final": "background",
                                   "layers": [{"at": 0.5, "dur": 1.0, "type": "source", "text": "Eleje"}],
                                   "insert": [{"file_id": cid, "at": {"between": [1, 5]}}]})
        parts, T = res["preview"]["parts"], res["inserted"][0]["at"]
        whats = " ".join(p["what"] for p in parts)
        assert "clip clip.mp4 starts" in whats and "clip clip.mp4 ends" in whats, parts
        assert any(p["from"] <= T and T + 3.0 <= p["to"] for p in parts), (T, parts)
        pv = _probe(output_path(res["preview"]["file_id"]))
        assert float(pv["format"]["duration"]) == pytest.approx(sum(p["to"] - p["from"] for p in parts), abs=0.4)
        for _ in range(40):
            st = await jobs.wait_status(res["final"]["job_id"], 15)
            if st["status"] != "running":
                break
        assert st["status"] == "done", st
        out = _probe(output_path(st["result"]["file_id"]))
        assert float(out["format"]["duration"]) == pytest.approx(9.0, abs=0.2)
        assert {(s["width"], s["height"]) for s in out["streams"] if s["codec_type"] == "video"} == {(720, 1280)}
    asyncio.run(go())
