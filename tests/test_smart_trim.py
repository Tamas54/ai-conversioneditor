"""Smart cut a trim-video-ban (app/tools/smart_trim.py): a megtartott rész zöme bitre pontos másolat, csak a vágáspont és
a következő kulcskocka közti pár kocka újrakódolt; ahol ez nem biztonságos, a régi teljes újrakódolás fut, és a válasz
megmondja, miért.

Minden kimenetet végig dekódolunk (hiba nélkül), a kockák száma és időbélyege a forrásé, a másolt kockák md5-je a
forrásé, a határkockák PSNR-je legalább a mai teljes újrakódolásé, a hang a képpel együtt fut. Minden minta szintetikus
(ffmpeg testsrc2 / sine / villanás + sípszó)."""
from __future__ import annotations

import array
import json
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg needed")

STUDIO = ["-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-g", "30"]   # a Studio kimenete, 1 s-os GOP


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True, capture_output=True)


def _src(path: Path, *venc: str, seconds: int = 6, vf: str = "", extra: tuple[str, ...] = ()) -> Path:
    _ff("-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=30:duration={seconds}{vf}", "-f", "lavfi", "-i",
        f"sine=frequency=440:duration={seconds}", *(venc or STUDIO), "-c:a", "aac", "-b:a", "256k", *extra, str(path))
    return path


def _probe(p: Path, *extra: str) -> dict:
    return json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                                      *extra, str(p)], capture_output=True, text=True, check=True).stdout)


def _video(p: Path, *extra: str) -> dict:
    return next(s for s in _probe(p, *extra)["streams"] if s["codec_type"] == "video")


def _decode_errors(p: Path) -> str:
    """A teljes fájl (minden sáv) végigdekódolva; a dekóder hibái (az ffprobe dekódol, muxer nincs a láncban)."""
    return subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0",
                           str(p)], capture_output=True, text=True).stderr


def _md5s(p: Path, ss: str | None = None, n: int | None = None) -> list[str]:
    out = subprocess.run(["ffmpeg", "-v", "error", "-noautorotate", *(["-ss", ss] if ss else []), "-i", str(p), "-map", "0:v:0",
                          *(["-frames:v", str(n)] if n else []), "-vsync", "passthrough", "-f", "framemd5", "-"],
                         capture_output=True, text=True, check=True).stdout
    return [line.rsplit(",", 1)[1].strip() for line in out.splitlines() if line and not line.startswith("#")]


def _times(p: Path) -> list[Fraction]:
    """A videó kockáinak megjelenítési ideje (s), 0-tól."""
    v = _video(p)
    tb = Fraction(v["time_base"])
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", str(v["index"]), "-show_entries", "packet=pts",
                          "-of", "csv=p=0", str(p)], capture_output=True, text=True, check=True).stdout
    t = sorted(int(x) * tb for x in out.split())
    return [x - t[0] for x in t]


def _audio_hashes(p: Path) -> list[str]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "packet=data_hash", "-show_data_hash",
                          "adler32", "-of", "csv=p=0", str(p)], capture_output=True, text=True, check=True).stdout
    return re.findall(r"adler32:[0-9a-f]+", out)


def _psnr(a: Path, b: Path, ss: str, n: int) -> list[float]:
    out = subprocess.run(["ffmpeg", "-v", "error", "-noautorotate", "-i", str(a), "-noautorotate", "-ss", ss, "-i", str(b),
                          "-lavfi", f"[0:v]trim=end_frame={n},setpts=PTS-STARTPTS[x];[1:v]trim=end_frame={n},"
                          "setpts=PTS-STARTPTS[y];[x][y]psnr=stats_file=-", "-vsync", "passthrough", "-f", "null", "-"],
                         capture_output=True, text=True, check=True).stdout
    return [99.0 if v == "inf" else float(v) for v in
            (part.split(":", 1)[1] for line in out.splitlines() for part in line.split() if part.startswith("psnr_avg:"))]


async def _trim(src: Path, out: Path, **o) -> dict:
    from app.ops.handlers import media
    out.mkdir(exist_ok=True)
    r = await media.trim([src], o, out)
    return {"file": r["files"][0], **r["data"]}


async def _today(src: Path, dst: Path, start: str, end: str) -> Path:
    """A mai út: a teljes megtartott rész újrakódolva (a referencia a határkockák minőségéhez)."""
    from app.ops.handlers import media
    return await media._video(src, dst, pre=["-ss", start, "-to", end])


# ---------------------------------------------------------------- a terv (tiszta függvény)

def _pk(seq: list[tuple[int, bool]]):
    """(pts, kulcs) dekódolási sorrendben → csomagok; dts = sorszám − 2 (B-kockás késleltetés)."""
    from app.tools.smart_trim import _Pk
    return [_Pk(pts, i - 2, key, 100 + i) for i, (pts, key) in enumerate(seq)]


def _gop(start: int, n: int = 6) -> list[tuple[int, bool]]:
    """Egy zárt GOP dekódolási sorrendben: I P B B P B B…"""
    order = [(start, True)]
    rest = list(range(start + 1, start + n))
    while rest:                                   # P előre, aztán a két B
        grp, rest = rest[:3], rest[3:]
        order += [(grp[-1], False)] + [(x, False) for x in grp[:-1]]
    return order


def test_plan_copies_between_keyframes_and_reencodes_the_edges():
    from app.tools import smart_trim
    pk = _pk(_gop(0) + _gop(6) + _gop(12) + _gop(18))
    p = smart_trim.plan(pk, Fraction(1, 6), Fraction(0), Fraction(2, 6), Fraction(15, 6))    # kockák 2…14
    assert p.frames == list(range(2, 15)) and (p.k1, p.k2) == (6, 12)
    assert p.head == [2, 3, 4, 5] and p.tail == [12, 13, 14] and sorted(x.pts for x in p.body) == list(range(6, 12))
    on = smart_trim.plan(pk, Fraction(1, 6), Fraction(0), Fraction(1), Fraction(3))           # kulcskockán kezd és végez
    assert (on.head, on.tail, on.k1, on.k2) == ([], [], 6, 18)
    end = smart_trim.plan(pk, Fraction(1, 6), Fraction(0), Fraction(7, 6), None)             # a fájl végéig
    assert end.k2 is None and end.head == [7, 8, 9, 10, 11] and len(end.body) == 12


def test_plan_refuses_open_gops_short_parts_and_disordered_timestamps():
    from app.tools import smart_trim
    tb, z = Fraction(1, 6), Fraction(0)
    opened = _pk([(0, True), (3, False), (1, False), (2, False),                    # nyitott GOP: a kulcskocka UTÁN dekódolt
                  (6, True), (4, False), (5, False), (9, False), (7, False), (8, False),   # kockák előtte látszanak
                  (12, True), (10, False), (11, False), (15, False), (13, False), (14, False),
                  (18, True), (16, False), (17, False), (19, False)])
    with pytest.raises(smart_trim.NotSafe, match="keyframe"):
        smart_trim.plan(opened, tb, z, Fraction(2, 6), Fraction(16, 6))
    with pytest.raises(smart_trim.NotSafe, match="shorter than"):
        smart_trim.plan(_pk(_gop(0) + _gop(6) + _gop(12)), tb, z, Fraction(1), Fraction(9, 6))
    bad = _pk(_gop(0) + _gop(6))
    bad[3].dts = bad[2].dts
    with pytest.raises(smart_trim.NotSafe, match="order"):
        smart_trim.plan(bad, tb, z, Fraction(1, 6), None)


# ---------------------------------------------------------------- smart cut

@pytest.mark.asyncio
async def test_smart_cut_copies_the_middle_bit_for_bit(tmp_path):
    """Elfogadás: kulcskockák közé eső vágáspontok — a köztes rész bitre a forrás, a határkockák legalább olyan jók, mint
    a mai teljes újrakódolásban, a fájl végig hibátlanul dekódol, a kockák száma és ideje a forrásé."""
    src = _src(tmp_path / "studio.mp4")
    r = await _trim(src, tmp_path / "o", start="1.4", end="4.6")
    out = r["file"]
    assert r["method"] == "smart cut", r["note"]
    assert r["copied_seconds"] == pytest.approx(2.0) and r["reencoded_seconds"] == pytest.approx(1.2)
    assert "start and the end" in r["note"]
    assert _decode_errors(out) == ""
    n = 96                                                         # 1.4 s … 4.6 s, 30 fps
    assert _times(out) == [Fraction(i, 30) for i in range(n)]
    o, s = _md5s(out), _md5s(src, "1.4", n)
    assert len(o) == n and o[18:78] == s[18:78]                    # 2.0 … 4.0 s: a forrás kockái, bitre
    v, w = _video(out, "-show_data"), _video(src, "-show_data")
    assert (v["profile"], v["level"], v["pix_fmt"], v["extradata"]) == (w["profile"], w["level"], w["pix_fmt"], w["extradata"])
    today = await _today(src, tmp_path / "today.mp4", "1.4", "4.6")
    ours, ref = _psnr(out, src, "1.4", n), _psnr(today, src, "1.4", n)
    edge = list(range(18)) + list(range(78, n))
    assert min(ours[i] for i in range(18, 78)) == 99.0              # azonos
    assert sum(ours[i] for i in edge) / len(edge) >= sum(ref[i] for i in edge) / len(edge) - 0.25
    a = next(s for s in _probe(out)["streams"] if s["codec_type"] == "audio")
    assert float(a["start_time"]) == 0 and float(a["duration"]) == pytest.approx(3.2, abs=0.03)
    got, have = _audio_hashes(out), _audio_hashes(src)                # az AAC-hang másolat: minden csomagja a forrásé
    assert len(got) >= 3.2 * 44100 / 1024 and "".join(f"{h}|" for h in got) in "".join(f"{h}|" for h in have)


@pytest.mark.asyncio
async def test_cut_on_keyframes_and_open_ends_reencode_only_what_they_must(tmp_path):
    src = _src(tmp_path / "studio.mov", extra=("-c:a", "pcm_s16le"))           # nem AAC-hang: az újrakódolva megy, mint eddig
    r = await _trim(src, tmp_path / "a", start="2", end="5")
    assert r["method"] == "smart cut" and r["reencoded_seconds"] == 0 and "nothing was re-encoded" in r["note"]
    assert r["file"].suffix == ".mov" and _md5s(r["file"]) == _md5s(src, "2", 90)
    assert [s["codec_name"] for s in _probe(r["file"])["streams"]] == ["h264", "aac"]
    r = await _trim(src, tmp_path / "b", start="3.5")
    assert r["method"] == "smart cut" and r["reencoded_seconds"] == pytest.approx(0.5) and "the start" in r["note"]
    assert len(_md5s(r["file"])) == 75 and _decode_errors(r["file"]) == ""
    r = await _trim(src, tmp_path / "c", end="2.5")
    assert r["method"] == "smart cut" and r["reencoded_seconds"] == pytest.approx(0.5) and "the end" in r["note"]
    assert _md5s(r["file"])[:60] == _md5s(src, None, 60) and _decode_errors(r["file"]) == ""


@pytest.mark.asyncio
async def test_smart_cut_keeps_rotation_and_variable_frame_timing(tmp_path):
    """A telefonos elforgatás-jelölés marad (a határdarab a kódolt tájolásban készül), és VFR-forrásnál (kiesett kockák)
    minden kocka a saját idejét kapja — nincs CFR-re kerekítés, nincs kockaduplázás."""
    raw = _src(tmp_path / "raw.mp4")
    rot = tmp_path / "rot.mp4"
    _ff("-i", str(raw), "-c", "copy", "-metadata:s:v:0", "rotate=90", str(rot))
    r = await _trim(rot, tmp_path / "a", start="1.4", end="4.6")
    assert r["method"] == "smart cut", r["note"]
    v = _video(r["file"])
    assert (v["width"], v["height"]) == (320, 240) and _decode_errors(r["file"]) == ""
    rot_of = lambda s: [(d.get("displaymatrix"), d.get("rotation")) for d in s.get("side_data_list", [])]
    assert rot_of(v) == rot_of(_video(rot)) and abs(rot_of(v)[0][1]) == 90
    vfr = _src(tmp_path / "vfr.mp4", vf=",select=not(between(n\\,50\\,64))*not(between(n\\,100\\,103))",
               extra=("-vsync", "vfr"))
    want = [t for t in _times(vfr) if Fraction(13, 10) <= t < Fraction(47, 10)]
    r = await _trim(vfr, tmp_path / "b", start="1.3", end="4.7")
    assert r["method"] == "smart cut", r["note"]
    assert _times(r["file"]) == [t - want[0] for t in want] and _decode_errors(r["file"]) == ""


@pytest.mark.asyncio
async def test_smart_cut_keeps_sound_in_sync(tmp_path):
    """Másodpercenként egy fehér kocka és vele egy 20 ms-os sípszó: vágás után minden villanás és sípszó együtt marad."""
    src = tmp_path / "flash.mp4"
    _ff("-f", "lavfi", "-i", "color=black:s=320x240:r=30:d=6", "-f", "lavfi", "-i",
        "aevalsrc=if(between(mod(t\\,1)\\,0.5\\,0.52)\\,sin(2*PI*1000*t)\\,0):s=48000:d=6",
        "-vf", "drawbox=c=white:t=fill:enable=eq(mod(n\\,30)\\,15)", *STUDIO, "-c:a", "aac", "-b:a", "256k", str(src))
    r = await _trim(src, tmp_path / "o", start="1.23", end="4.81")
    assert r["method"] == "smart cut", r["note"]
    gray = subprocess.run(["ffmpeg", "-v", "error", "-i", str(r["file"]), "-vf", "scale=8:8,format=gray", "-f", "rawvideo", "-"],
                          capture_output=True, check=True).stdout
    flashes = [i / 30 for i in range(len(gray) // 64) if sum(gray[i * 64:(i + 1) * 64]) / 64 > 128]
    pcm = array.array("h", subprocess.run(["ffmpeg", "-v", "error", "-i", str(r["file"]), "-ac", "1", "-ar", "48000", "-f",
                                           "s16le", "-"], capture_output=True, check=True).stdout)
    loud = [i for i, x in enumerate(pcm) if abs(x) > 3000]
    beeps = [loud[0] / 48000] + [b / 48000 for a, b in zip(loud, loud[1:]) if b - a > 4800]
    assert len(flashes) == len(beeps) == 4
    for f, b in zip(flashes, beeps):                               # 1.2333 s-tól: 0.2667, 1.2667, 2.2667, 3.2667
        assert abs(f - b) < 0.012, (flashes, beeps)


# ---------------------------------------------------------------- ahol nem biztonságos: a régi út, megindokolva

@pytest.mark.asyncio
@pytest.mark.parametrize("kind, why", [
    ("hevc", "H.264 video only, and this one is HEVC"),
    ("crf23", "lower quality setting (CRF 23)"),
    ("not_x264", "wasn't encoded by x264"),
    ("open_gop", "open GOPs"),
])
async def test_falls_back_to_a_full_reencode_and_says_why(tmp_path, kind, why):
    src = tmp_path / f"{kind}.mp4"
    if kind == "hevc":
        _src(src, "-c:v", "libx265", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1",
             "-x265-params", "log-level=error")
    elif kind == "crf23":
        _src(src, "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p", "-g", "30")
    elif kind == "not_x264":                                         # az x264 névjegye (SEI) nélkül
        _ff("-i", str(_src(tmp_path / "x.mp4")), "-c", "copy", "-bsf:v", "filter_units=remove_types=6", str(src))
    else:
        _src(src, *STUDIO, "-x264-params", "open-gop=1")
    r = await _trim(src, tmp_path / "o", start="1.4", end="4.6")
    assert r["method"] == "re-encode" and why in r["note"] and r["note"].startswith("The whole clip was re-encoded"), r
    v = _video(r["file"])
    assert v["codec_name"] == "h264" and _decode_errors(r["file"]) == ""
    assert float(_probe(r["file"])["format"]["duration"]) == pytest.approx(3.2, abs=0.05)


@pytest.mark.asyncio
async def test_switch_and_failed_self_check_fall_back(tmp_path, monkeypatch):
    from app.tools import smart_trim
    src = _src(tmp_path / "studio.mp4", seconds=4)
    monkeypatch.setenv("VIDEO_SMART_TRIM", "0")
    r = await _trim(src, tmp_path / "a", start="1.4", end="2.6")
    assert r["method"] == "re-encode" and "switched off" in r["note"]
    monkeypatch.delenv("VIDEO_SMART_TRIM")

    async def broken(*a, **k):
        raise smart_trim.NotSafe("the copied part differs from the original")
    monkeypatch.setattr(smart_trim, "_verify", broken)
    r = await _trim(src, tmp_path / "b", start="1.4", end="3.6")
    assert r["method"] == "re-encode" and "copied part differs" in r["note"]
    assert _decode_errors(r["file"]) == "" and len(_md5s(r["file"])) == 66
    assert not list((tmp_path / "b").glob(".smart-*"))              # a munkamappa eltakarítva


@pytest.mark.asyncio
async def test_cut_point_encodes_share_the_heavy_job_threads(tmp_path, monkeypatch):
    """A nehéz munka kapuján belül (HEAVY_JOB_THREADS) a két, egyszerre futó határdarab-kódolás a munka szálain osztozik;
    a próbakockát és a visszalépés teljes újrakódolását a teljes korlát kapja."""
    from app import jobs
    from app.convert import engines
    from app.tools import smart_trim
    monkeypatch.setenv("HEAVY_JOB_THREADS", "4")
    seen, orig = [], engines._threads

    def spy(args):
        out = orig(args)
        if "libx264" in args:
            n = args[args.index("-frames:v") + 1] if "-frames:v" in args else None
            seen.append(("full" if n is None else "probe" if n == "1" else "piece", out[-2], engines.FFMPEG_THREADS.get()))
        return out
    monkeypatch.setattr(engines, "_threads", spy)
    src = _src(tmp_path / "studio.mp4", seconds=4)
    async with jobs.heavy():
        r = await _trim(src, tmp_path / "a", start="1.4", end="3.6")
    assert r["method"] == "smart cut" and "start and the end" in r["note"], r["note"]
    assert sorted(seen) == [("piece", "2", 2), ("piece", "2", 2), ("probe", "4", 4)]
    assert _decode_errors(r["file"]) == "" and len(_md5s(r["file"])) == 66
    seen.clear()

    async def broken(*a, **k):
        raise smart_trim.NotSafe("the copied part differs from the original")
    monkeypatch.setattr(smart_trim, "_verify", broken)
    async with jobs.heavy():
        r = await _trim(src, tmp_path / "b", start="1.4", end="3.6")
    assert r["method"] == "re-encode" and seen[-1] == ("full", "4", 4)


@pytest.mark.asyncio
async def test_trim_video_tool_reports_the_method(tmp_path, put_file):
    from app.ops import run_tool
    fid = put_file(_src(tmp_path / "studio.mp4", seconds=4))
    r = await run_tool("trim-video", [fid], {"start": "1.4", "end": "3"}, charge=False)
    assert r["data"]["method"] == "smart cut" and r["data"]["note"].startswith("Smart cut:")
