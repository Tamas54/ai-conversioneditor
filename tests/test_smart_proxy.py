"""A munkamásolat vágása újrakódolás nélkül (10-25, PROFILE.md 3.d: egy vágás +430 s volt — a munkamásolat az eredetiből
készült újra). Egy meglévő munkamásolatból (ugyanazzal a korrekcióval, más idővonallal) a megmaradó GOP-ok másolva,
csak a vágáspontok körül kódolva:
- a terv: mely kockák jönnek a régiből (forrás, illesztés, áttűnés), melyik GOP másolható, mi kódolandó;
- végponttól végpontig: ugyanannyi kocka, mint a teljes újrakódolásé, a kép (PSNR) ugyanaz, a hang a helyén;
- ahol nem megy (stabilizálás, más korrekció): a teljes út.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.tools import footage


def test_reuse_maps_kept_parts_to_the_old_copy():
    # vágatlan régi → vágott új: két futam, a második 20 s-mal (600 kockával) odébb a régiben
    runs = footage.reuse(None, [[0.0, 300.0], {"a": 320.0, "b": 1816.0, "join": "cut"}], 30.0)
    assert runs == [(0, 0, 9000), (9000, 9600, 44880)]
    # vágott régi → még egy vágás: a régi második darabjából is jön
    old = [[0.0, 300.0], {"a": 320.0, "b": 1816.0, "join": "cut"}]
    new = footage.cut(old, 1816.0, [(600.0, 610.0)], 30.0)
    runs = footage.reuse(old, new, 30.0)
    assert runs == [(0, 0, 18000), (18000, 18300, 35580)]
    # beszúrt klip: ami a régiben nincs meg (a klip), nem jön a régiből
    ins = footage.insert(None, 100.0, 50.0, {"a": 0.0, "b": 10.0, "src": "clip.mp4", "join": "smooth"}, 30.0)
    runs = footage.reuse(None, ins, 30.0)
    assert runs == [(0, 0, 1500), (1800, 1500, 1500)]


def test_a_dissolve_comes_from_the_old_copy_only_if_it_is_the_same():
    old = [[0.0, 50.0], {"a": 0.0, "b": 10.0, "src": "c.mp4", "join": "dissolve"}, {"a": 50.0, "b": 100.0, "join": "dissolve"}]
    same = footage.reuse(old, old, 30.0)
    assert same == [(0, 0, 3300)]                                   # ugyanaz az idővonal: minden a régiből
    # a klip eleje 2 s-mal később: az áttűnése új → a klip első 12 kockája (0,4 s) kódolandó
    moved = [[0.0, 50.0], {"a": 2.0, "b": 10.0, "src": "c.mp4", "join": "dissolve"}, {"a": 50.0, "b": 100.0, "join": "dissolve"}]
    runs = footage.reuse(old, moved, 30.0)
    assert runs[0] == (0, 0, 1500) and runs[1] == (1512, 1572, 228)
    assert runs[2][0] == 1740 + 12                                  # utána is: az áttűnés kulcsában a klip eleje is benne van


def test_stitch_copies_whole_gops_and_encodes_the_rest():
    keys = list(range(0, 3000, 30))                                 # 100 s, kulcskocka másodpercenként
    ops = footage.stitch([(0, 0, 1500), (1500, 1515, 1485)], keys, 3000, 2985)
    assert ops == [("copy", 0, 0, 1500), ("enc", 1500, 1515), ("copy", 1515, 1530, 1470)]
    # a futam a régi végéig: a vég is vágópont; a hosszú kódolandó rész darabokban
    ops = footage.stitch([(0, 100, 2900)], keys, 3000, 3200, piece=150)
    assert ops[0] == ("enc", 0, 20) and ops[1] == ("copy", 20, 120, 2880)
    assert [op for op in ops if op[0] == "enc"][1:] == [("enc", 2900, 3050), ("enc", 3050, 3200)]


def test_clean_keys_are_closed_gop_starts():
    # dekódolási sorrend B-kockákkal (pts): I0 P3 B1 B2 | I4 P7 B5 B6 — és egy nyitott GOP-os kulcskocka (I8 után B7 jön)
    pk = [(0, True), (3, False), (1, False), (2, False), (4, True), (7, False), (5, False), (6, False)]
    assert footage.clean_keys(pk) == [0, 4]
    pk2 = pk + [(9, True), (8, False)]                              # a 9-es kulcskocka után a 8-as: nem tiszta
    assert footage.clean_keys(pk2) == [0, 4]


def test_donors_need_the_same_recipe(monkeypatch):
    from app.tools import crafts
    proxies = {footage._key({"adjust": {}}, None): {"media_id": "a.mp4"},
               footage._key({"adjust": {"lut": "cinema"}}, None): {"media_id": "b.mp4"}}
    monkeypatch.setattr("app.storage.read_meta", lambda fid: {"proxies": proxies})
    monkeypatch.setattr(crafts, "existing", lambda fid: Path("/"))
    base = {"file_id": "x.mov", "fps": 30, "keep": [[0.0, 300.0], [320.0, 1816.0]]}
    assert [h["media_id"] for _, h in footage.donors(base)] == ["a.mp4"]
    assert [h["media_id"] for _, h in footage.donors({**base, "adjust": {"lut": "cinema"}})] == ["b.mp4"]
    assert footage.donors({**base, "adjust": {"stabilize": True}}) == []      # stabilizálva: az egész mozgásából
    monkeypatch.setenv("VIDEO_SMART_PROXY", "0")
    assert footage.donors(base) == []


def _engine_ok() -> bool:
    return bool(shutil.which("ffmpeg"))


def _clip(path: Path, secs: int = 24) -> Path:
    """1280×720-as mintás felvétel hanggal (nagyobb a munkamásolatnál: kell munkamásolat)."""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=48000", "-t", str(secs), "-c:v", "libx264", "-crf", "14", "-g", "48",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def _frames(p: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                          "stream=nb_read_packets", "-of", "csv=p=0", str(p)], capture_output=True, text=True, check=True).stdout
    return int(out.strip())


def _max_gap(p: Path) -> float:
    """A kockák időbélyegei közti legnagyobb lépés (s): az illesztéseknél se legyen lyuk / ugrás."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time", "-of", "csv=p=0",
                          str(p)], capture_output=True, text=True, check=True).stdout
    ts = sorted(float(x) for x in out.split() if x.strip())
    return max(b - a for a, b in zip(ts, ts[1:]))


def _psnr_all(a: Path, b: Path) -> tuple[float, float]:
    """Kockáról kockára (a két fájl azonos sorszámú kockái): (átlag, legkisebb) PSNR."""
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", str(a), "-i", str(b), "-lavfi", "[0:v][1:v]psnr=stats_file=-",
                        "-f", "null", "-"], capture_output=True, text=True)
    vals = [float(m) if m != "inf" else 99.0 for m in re.findall(r"psnr_avg:(inf|[0-9.]+)", r.stdout)]
    return (sum(vals) / len(vals), min(vals)) if vals else (0.0, 0.0)


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="ffmpeg needed")
def test_a_cut_copy_is_made_from_the_old_copy_and_matches_a_full_remake(tmp_path, put_file, monkeypatch):
    from app.storage import output_path
    clip = _clip(tmp_path / "talk.mp4")
    fid = put_file(clip)
    base = {"file_id": fid, "asset": "footage.mp4", "width": 1280, "height": 720, "fps": 30.0, "duration": 24.0, "audio": True}
    used = []
    real = footage.smart_proxy

    async def spy(*a, **k):
        r = await real(*a, **k)
        used.append(r is not None)
        return r
    monkeypatch.setattr(footage, "smart_proxy", spy)

    async def main():
        b0, _ = await footage.make_proxy(base, clip)                           # a vágatlan munkamásolat (a donor)
        assert not used
        keep = footage.cut(None, 24.0, [(5.5, 9.2)], 30.0)                    # nem egész másodpercen: részleges GOP-ok
        cut = {**base, "keep": keep, "duration": footage.length(keep, 24.0)}
        b1, _ = await footage.make_proxy(cut, clip)
        assert used == [True], "the cut copy should come from the old copy"
        full, _ = await footage._build_proxy(cut, clip)                       # ugyanaz teljes újrakódolással
        return b0, b1, full
    b0, b1, full = asyncio.run(main())
    p1, pf = output_path(b1["media_id"]), output_path(full["media_id"])
    assert _frames(p1) == _frames(pf) == round(footage.length(b1["keep"], 24.0) * 30)
    assert _max_gap(p1) == pytest.approx(1 / 30, abs=0.002)         # egyenletes kockaidő, az illesztéseken is
    avg, low = _psnr_all(p1, pf)
    assert avg > 40 and low > 35, (avg, low)
    assert b1["proxy"] == b0["proxy"] == {"w": 960, "h": 540}
    # a hang: a vágott idővonalé, ugyanolyan hosszú
    au = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration",
                                    "-of", "json", str(p1)], capture_output=True, text=True, check=True).stdout)["streams"][0]
    assert float(au["duration"]) == pytest.approx(24.0 - 3.7, abs=0.06)
