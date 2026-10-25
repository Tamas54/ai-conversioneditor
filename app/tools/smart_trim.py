"""Vágás újrakódolás nélkül (smart cut): a megtartott rész zömét bitre pontosan átmásoljuk, és csak a vágáspont és a
következő kulcskocka közti pár másodpercet kódoljuk újra — a forrással azonos paraméterkészlettel —, majd összefűzzük.

Miért: a trim-video eddig a TELJES megtartott részt újrakódolta (x264 CRF 17); 1 percet levágni egy 45 perces videóról
44 perc kódolás volt, és egy generációnyi minőségvesztés.

Mikor biztonságos (különben a régi, teljes újrakódolás fut, és a válasz angolul megmondja, miért):
  · H.264, MP4/MOV/M4V-ben, 8 bites 4:2:0, progresszív, és x264 kódolta — a Studio saját kimenetei mind ilyenek. Az
    x264 az első kockába beírja a beállításait; ebből a határdarabok paraméterkészlete (SPS/PPS) BÁJTRA azonos a
    forráséval. Ezt nem feltételezzük, hanem összevetjük: így a kész fájl egyetlen mintaleírása (avcC) minden kockára
    érvényes, nincs sávon belüli paraméter-váltás és nincs második mintaleírás (ezeket nem minden lejátszó viseli).
  · A másolt rész első kockája IDR, és a vágás tiszta: előtte dekódolt kocka nem látszik utána, utána dekódolt nem
    látszik előtte (a csomagok időbélyegéből, kodektől függetlenül — a nyitott GOP így kiesik).
  · A határdarab legalább olyan jó, mint a teljes újrakódolás: a mai preset (VIDEO_X264_PRESET) és CRF ≤ a mai
    (VIDEO_CRF_INTERMEDIATE, 17). Az x264 a CRF egész részét a PPS-be írja (pic_init_qp), ezért egy 17-nél gyengébb
    CRF-fel kódolt forráshoz (pl. a renderelő CRF 19-es végső kimenete) csak gyengébb határdarab illene: ott a teljes
    újrakódolás marad. Bitrátás x264-forrásnál (ABR/CBR, pic_init_qp=26) a határdarab „stitchable” CRF 17.
Nem megy (mérve 10-23, iPhone 17 Pro Max, HEVC Main 10 HLG + Dolby Vision 8.4): a telefon HEVC-kódolójának
paraméterkészletét az x265 a legközelebbi beállítással sem állítja elő (general_non_packed_constraint_flag=1,
sub_layer_ordering_info_present_flag=0, 1088 px-re kódolt szélesség levágási ablakkal), a GOP nyitott (CRA + RASL: a
2 perces felvétel 130 kulcskockájából 129 nem tiszta vágópont), és minden kockán Dolby Vision RPU van. Eltérő
paraméterkészlet egy sávon belül az Apple lejátszóin nem biztonságos — a telefonos felvétel ezért a teljes
újrakódolást kapja (a válasz ezt megmondja).

A hang: AAC-t másolunk (bitre azonos; 30 perc AAC-újrakódolása 80 s volt a 96-ból), más hangot újrakódolunk, mint eddig.

Minden kimenet ellenőrzött, mielőtt kiadjuk: paraméterkészlet, kodek/profil/szint/pixelformátum/színjelölés/elforgatás
egyezik; a kockák száma és időbélyege (VFR-en is) a forrás kockáié; a másolt csomagok mérete a forráséval azonos; a
két illesztés körül dekódolva nincs hiba, és a másolt kockák md5-je a forráséval egyezik; a hang a videóval együtt
indul és ér véget. Ha bármi eltér: teljes újrakódolás. VIDEO_SMART_TRIM=0 kikapcsolja.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)

_FF = ("ffmpeg", "-y", "-hide_banner", "-loglevel", "error")
_CONTAINERS = ("mp4", "mov", "m4v")
_PIX = ("yuv420p", "yuvj420p")
_SEI = re.compile(rb"x264 - core \d+[^\x00]*? options: ([ -~]+)")
_PSY_PRESETS = ("fast", "medium", "slow", "slower", "veryslow", "placebo")      # subme ≥ 6: a psy-rd a króma-QP-t −2-vel tolja
CHECK_S = 1.0               # az illesztések körül ennyit dekódolunk vissza (és vetünk össze a forrással)


def enabled() -> bool:
    return os.getenv("VIDEO_SMART_TRIM", "1").strip().lower() not in ("0", "false", "no", "off")


class NotSafe(Exception):
    """Ennél a videónál a smart cut nem biztonságos — az üzenet a felhasználónak szól (angolul, „because …” után)."""


def seconds(v: Any) -> Fraction:
    """„90”, „1:30”, „00:01:30.5” → pontos tört másodperc (az ffmpeg értelmezése szerint: [[óó:]pp:]ss[.t])."""
    t = Fraction(0)
    for part in str(v).strip().split(":"):
        t = t * 60 + Fraction(part)
    return t


def _clock(s: float) -> str:
    s = max(0.0, s)
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:04.1f}" if s >= 3600 else \
        f"{int(s // 60)}:{s % 60:04.1f}" if s >= 60 else f"{s:.1f} s"


def _ts(x: Fraction) -> str:
    return f"{float(max(Fraction(0), x)):.6f}"


async def _ff(*args: str, timeout: float = 1800) -> bytes:
    from app.convert.engines import run
    out, _ = await run(*args, timeout=timeout)
    return out


async def _probe_json(path: Path, *extra: str) -> dict[str, Any]:
    return json.loads(await _ff("ffprobe", "-v", "error", "-print_format", "json", *extra, str(path), timeout=120) or b"{}")


# ---------- a forrás felmérése ----------

def _video_stream(info: dict[str, Any]) -> dict[str, Any]:
    vids = [s for s in info.get("streams", []) if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")]
    if len(vids) != 1:
        raise NotSafe("it has more than one video track")
    return vids[0]


def _encoder_hint(v: dict[str, Any]) -> str:
    tags = v.get("tags") or {}
    if "core media" in str(tags.get("handler_name", "")).lower():
        return " (it was recorded by an Apple device)"
    enc = str(tags.get("encoder") or "").strip()
    return f" (it was made by {enc})" if enc and "x264" not in enc else ""


def _check_source(src: Path, dst: Path, info: dict[str, Any]) -> dict[str, Any]:
    from app.catalog.formats import from_filename
    if from_filename(dst.name) not in _CONTAINERS or "mp4" not in str(info.get("format", {}).get("format_name", "")):
        raise NotSafe("smart cut works on MP4 and MOV files only")
    v = _video_stream(info)
    if v.get("codec_name") != "h264":
        name = {"hevc": "HEVC (H.265)"}.get(v.get("codec_name"), str(v.get("codec_name") or "unknown").upper())
        raise NotSafe(f"smart cut works on H.264 video only, and this one is {name}{_encoder_hint(v)}")
    if str(v.get("codec_tag_string")) != "avc1":
        raise NotSafe("its H.264 stream keeps settings inside the picture data (avc3)")
    if v.get("pix_fmt") not in _PIX:
        raise NotSafe(f"its pictures are {v.get('pix_fmt')}, not 8-bit 4:2:0")
    if v.get("field_order") not in (None, "progressive", "unknown"):
        raise NotSafe("it is interlaced")
    tb = Fraction(str(v.get("time_base") or "0/1"))
    if tb <= 0 or tb.numerator != 1:
        raise NotSafe("its timing can't be copied exactly")
    return v


async def _x264_options(src: Path, v: dict[str, Any]) -> dict[str, str]:
    """Az x264 beállításai a forrás első kockájából (SEI user data: „x264 - core … options: cabac=1 ref=3 …”)."""
    raw = await _ff(*_FF, "-i", str(src), "-map", f"0:{v['index']}", "-c", "copy", "-frames:v", "1", "-f", "data", "-",
                    timeout=120)
    m = _SEI.search(raw)
    if not m:
        raise NotSafe(f"it wasn't encoded by x264, so its cut points can't be re-encoded to match the rest{_encoder_hint(v)}")
    return dict(kv.split("=", 1) for kv in m.group(1).decode("ascii").split() if "=" in kv)


def _x264_args(x: dict[str, str], v: dict[str, Any]) -> list[str]:
    """A határdarab x264-beállításai: a mai preset és minőség + a forrás paraméterkészletét meghatározó kapcsolók."""
    from app.tools import encoding
    for k, why in (("interlaced", "it is interlaced"), ("fake_interlaced", "it is interlaced"),
                   ("open_gop", "it uses open GOPs"), ("intra_refresh", "it uses periodic intra refresh instead of keyframes")):
        if x.get(k, "0") not in ("0", ""):
            raise NotSafe(why)
    if x.get("cqm", "0") != "0" or x.get("nal_hrd", "none") != "none" or x.get("avcintra", "0") not in ("0", ""):
        raise NotSafe("it uses special x264 settings (custom matrices or HRD) that aren't matched")
    preset, limit = encoding.preset(), encoding.intermediate_crf()
    p: dict[str, str] = {}
    rc = x.get("rc")
    if rc == "crf":
        crf = float(x.get("crf", "99"))
        if crf > limit + 1e-6:
            raise NotSafe(f"it was encoded at a lower quality setting (CRF {crf:g}) than the Studio's (CRF {limit}): "
                          "its cut points could only be re-encoded to match at that lower quality")
        q = ["-crf", f"{crf:g}"]
    elif rc in ("abr", "cbr", "2pass"):
        p["stitchable"] = "1"                         # pic_init_qp = 26, mint a bitrátás forrásé
        q = ["-crf", str(limit)]
    elif rc == "cqp" and int(x.get("qp", "99")) <= limit:
        q = ["-qp", x["qp"]]
    else:
        raise NotSafe("its x264 rate control can't be matched at the Studio's quality")
    for k in ("cabac", "ref", "8x8dct", "bframes", "weightb", "weightp", "constrained_intra", "keyint", "bluray_compat"):
        if k in x:
            p[k.replace("_", "-")] = x[k]
    if "b_pyramid" in x:
        p["b-pyramid"] = x["b_pyramid"]
    if "keyint_min" in x:
        p["min-keyint"] = x["keyint_min"]
    if "deblock" in x:                               # „1:0:0” = be, alfa, béta (a szeletfejlécben van — a kép miatt egyeztetjük)
        on, a, b = (x["deblock"].split(":") + ["0", "0"])[:3]
        p["deblock" if on == "1" else "no-deblock"] = f"{a},{b}" if on == "1" else "1"
    if "chroma_qp_offset" in x:                      # a kiírt érték már a psy-eltolás utáni; a miénket visszaadjuk rá
        p["chroma-qp-offset"] = str(int(x["chroma_qp_offset"]) + (2 if preset in _PSY_PRESETS else 0))
    if int(v.get("level") or 0) >= 10:
        p["level"] = str(v["level"])
    color = []
    for opt, key in (("-color_primaries", "color_primaries"), ("-color_trc", "color_transfer"),
                     ("-colorspace", "color_space"), ("-color_range", "color_range")):
        if v.get(key) and v[key] not in ("unknown", "reserved"):
            color += [opt, str(v[key])]
    return ["-c:v", "libx264", "-preset", preset, *q, "-pix_fmt", str(v["pix_fmt"]),
            "-x264-params", ":".join(f"{k}={val}" for k, val in p.items()), *color]


def _param_sets(stream: dict[str, Any]) -> list[bytes]:
    """Az avcC (ffprobe -show_data hexdumpja) SPS/PPS NAL-egységei."""
    hexs = "".join(line.split(":", 1)[1][:40].replace(" ", "")
                   for line in str(stream.get("extradata") or "").strip().splitlines() if ":" in line)
    b = bytes.fromhex(hexs)
    if len(b) < 7 or b[0] != 1:
        return []
    out, i = [], 6
    try:
        for count in (b[5] & 31, None):
            if count is None:
                count, i = b[i], i + 1
            for _ in range(count):
                n = int.from_bytes(b[i:i + 2], "big")
                out.append(b[i + 2:i + 2 + n])
                i += 2 + n
    except IndexError:
        return []
    return out


async def _stream_data(path: Path, index: int) -> dict[str, Any]:
    d = await _probe_json(path, "-show_streams", "-show_data", "-select_streams", str(index))
    return (d.get("streams") or [{}])[0]


@dataclass
class _Pk:
    pts: int
    dts: int
    key: bool
    size: int


async def _packets(path: Path, index: int) -> list[_Pk]:
    out = await _ff("ffprobe", "-v", "error", "-select_streams", str(index), "-show_entries", "packet=pts,dts,flags,size",
                    "-of", "compact=p=0", str(path), timeout=600)
    pk = []
    for line in out.decode().split():
        f = dict(kv.split("=", 1) for kv in line.split("|") if "=" in kv)
        if not f.get("pts", "N/A").lstrip("-").isdigit() or not f.get("dts", "N/A").lstrip("-").isdigit():
            raise NotSafe("some of its frames have no timestamps")
        pk.append(_Pk(int(f["pts"]), int(f["dts"]), "K" in f.get("flags", ""), int(f.get("size") or 0)))
    return pk


# ---------- a terv: mi marad, mi másolható ----------

@dataclass
class Plan:
    tb: Fraction                    # a videósáv időalapja
    t0: Fraction                    # a fájl kezdőideje (az ffmpeg -ss ehhez képest számol)
    frames: list[int]               # a megtartott kockák pts-e, megjelenítési sorrendben
    k1: int                         # a másolt rész első kockája (IDR)
    k2: Optional[int]               # a másolt rész utáni első kocka (innen újrakódolt vég); None: a másolás a végéig tart
    body: list[_Pk]                 # a másolt csomagok, dekódolási sorrendben
    last_dur: int                   # az utolsó megtartott kocka hossza (tb)

    @property
    def head(self) -> list[int]:
        return [p for p in self.frames if p < self.k1]

    @property
    def tail(self) -> list[int]:
        return [p for p in self.frames if self.k2 is not None and p >= self.k2]

    def secs(self, a: int, b: int) -> float:
        return float((b - a) * self.tb)


def plan(pk: list[_Pk], tb: Fraction, t0: Fraction, start: Optional[Fraction], end: Optional[Fraction]) -> Plan:
    """A megtartott kockák [start, end) (a fájl kezdetétől), és a leghosszabb tisztán másolható szakasz."""
    if any(b.dts <= a.dts for a, b in zip(pk, pk[1:])) or len({p.pts for p in pk}) != len(pk):
        raise NotSafe("its frame timestamps are not in order")
    order = sorted(p.pts for p in pk)
    lo = t0 + (start or 0)
    keep = [p for p in order if p * tb >= lo and (end is None or p * tb < t0 + end)]
    if not keep:
        raise NotSafe("nothing is left between start and end")
    n = len(pk)
    pre_max, run = [], None                         # tiszta vágás: előtte dekódolt mind korábban, utána dekódolt mind később látszik
    for p in pk:
        pre_max.append(run)
        run = p.pts if run is None else max(run, p.pts)
    suf_min, run = [0] * n, None
    for i in range(n - 1, -1, -1):
        run = pk[i].pts if run is None else min(run, pk[i].pts)
        suf_min[i] = run
    clean = {pk[i].pts: i for i in range(n)
             if pk[i].key and (pre_max[i] is None or pre_max[i] < pk[i].pts) and suf_min[i] == pk[i].pts}
    nxt = [p for p in order if p > keep[-1]]
    starts = sorted(p for p in clean if keep[0] <= p <= keep[-1])
    if not starts:
        raise NotSafe("the kept part has no keyframe to copy from")
    k1 = starts[0]
    ends = [p for p in clean if k1 < p <= (nxt[0] if nxt else keep[-1])]     # a vég kulcskockán: nincs újrakódolt vég
    if not nxt:
        k2, i2 = None, n
    elif ends:
        k2 = max(ends)
        i2 = clean[k2]
    else:
        raise NotSafe("the kept part is shorter than the distance between two keyframes")
    body = pk[clean[k1]:i2]
    span = [p for p in keep if p >= k1 and (k2 is None or p < k2)]
    if sorted(p.pts for p in body) != span:
        raise NotSafe("its frames can't be split cleanly at the keyframes")
    last = (nxt[0] - keep[-1]) if nxt else (keep[-1] - keep[-2] if len(keep) > 1 else 1)
    return Plan(tb, t0, keep, k1, k2, body, last)


# ---------- a darabok ----------

def _seek(p: Plan, pts: int, after: bool) -> str:
    """-ss közvetlenül e kocka elé vagy mögé (negyed időegységnyire, de legalább 1 µs-ra)."""
    eps = max(p.tb / 4, Fraction(1, 1000000))
    return _ts(pts * p.tb - p.t0 + (eps if after else -eps))


async def _encode(src: Path, v: dict[str, Any], p: Plan, frames: list[int], args: list[str], dst: Path) -> None:
    """Pontosan ezek a kockák (forrás-pts szerint, a trim szűrővel), a kódolt tájolásban, x264-gyel; az idő 0-tól."""
    await _ff(*_FF, "-noautorotate", "-noaccurate_seek", "-copyts", "-ss", _seek(p, frames[0], True), "-i", str(src),
              "-map", f"0:{v['index']}",
              "-vf", f"trim=start_pts={frames[0]}:end_pts={frames[-1] + 1},setpts=PTS-STARTPTS",
              "-frames:v", str(len(frames)), "-vsync", "passthrough", *args, "-an", "-sn", "-dn", "-map_metadata", "-1",
              "-video_track_timescale", str(p.tb.denominator), "-f", "mp4", str(dst))


async def _share(n: int, job: Awaitable[Any]) -> Any:
    """Párhuzamos kódolás a nehéz munka kapuján belül (app/jobs.py heavy()): a munka szálkorlátja
    (engines.FFMPEG_THREADS) az egyszerre futó n kódolás között elosztva — a vágás együtt se használjon több magot, mint
    egy teljes újrakódolás. A saját taskjában állítjuk (az asyncio.gather a kontextust taskonként másolja)."""
    from app.convert import engines
    cap = engines.FFMPEG_THREADS.get()
    if cap and n > 1:
        engines.FFMPEG_THREADS.set(max(1, cap // n))
    return await job


async def _copy_body(src: Path, v: dict[str, Any], p: Plan, dst: Path) -> None:
    """A k1 kulcskockától len(body) csomag, változatlanul. Az ffmpeg a keresésnél egy korábbi kulcskockára is ugorhat, és
    alapból az onnantól jövő csomagokat is átmásolja (negatív időbélyeggel, az MP4 vágólistája mögé rejtve) — a
    -copypriorss 0 eldobja a k1 előttieket, így az első csomag pontosan k1."""
    await _ff(*_FF, "-ss", _seek(p, p.k1, False), "-i", str(src), "-map", f"0:{v['index']}", "-c", "copy", "-copypriorss", "0",
              "-frames:v", str(len(p.body)),
              "-an", "-sn", "-dn", "-map_metadata", "-1", "-video_track_timescale", str(p.tb.denominator), "-f", "mp4", str(dst))


async def _aux(src: Path, p: Plan, end: Optional[Fraction], acodec: list[str], preroll: Optional[Fraction],
               dst: Path) -> None:
    """Hang (és felirat, fejezetek) az első megtartott kockától, hogy a képpel együtt induljon — ugyanaz a sávválasztás,
    mint a teljes újrakódolásnál. Az AAC-hangot másoljuk, két AAC-kockányival (preroll) korábbról, negatív
    időbélyeggel: az MP4 vágólistája rejti el (mint az AAC saját 1024–2112 mintás kezdőkésleltetését), és a dekóder így
    az első megtartott kockát is helyesen adja vissza; a -copypriorss 0 ennél korábbit nem enged. Más hang (PCM, MP3,
    Opus…) újrakódolva, ahogy eddig."""
    a = p.frames[0] * p.tb - p.t0
    to = ["-to", _ts(end)] if end is not None else []
    if preroll is not None:
        await _ff(*_FF, "-copyts", "-ss", _ts(a - preroll), *to, "-i", str(src), "-vn", "-dn", "-c:a", "copy",
                  "-copypriorss", "0", "-output_ts_offset", f"-{_ts(a)}", str(dst))
    else:
        await _ff(*_FF, "-ss", _ts(a), *to, "-i", str(src), "-vn", "-dn", *acodec, str(dst))


async def _ps_match(src: Path, v: dict[str, Any], p: Plan, args: list[str], want: list[bytes], work: Path) -> list[str]:
    """Egy kockányi próbakódolás: a határdarab paraméterkészlete bájtra a forrásé-e (mielőtt a hosszú másolás indul).
    Az SPS időzítése (VUI) a kódoló időalapjából jön: az ffmpeg CFR-kimenete 1/fps, a VFR-é a sáv időalapja — mindkettőt
    megpróbáljuk; az egyező kapcsolókkal kódol a fej és a vég."""
    for extra in ([], ["-enc_time_base", "-1"]):
        t = work / "probe.mp4"
        await _encode(src, v, p, p.frames[:1], args + extra, t)
        if _param_sets(await _stream_data(t, 0)) == want:
            return args + extra
    raise NotSafe("its cut points can't be re-encoded with exactly the same stream settings")


# ---------- ellenőrzés ----------

async def _md5s(path: Path, index: int, ss: Fraction, n: int) -> list[str]:
    out = await _ff(*_FF, "-noautorotate", "-ss", _ts(ss), "-i", str(path), "-map", f"0:{index}", "-frames:v", str(n),
                    "-vsync", "passthrough", "-f", "framemd5", "-", timeout=600)
    return [line.rsplit(",", 1)[1].strip() for line in out.decode().splitlines() if line and not line.startswith("#")]


def _norm(x: Any) -> str:
    return "" if x in (None, "unknown", "0:1", "1:1") else str(x)


def _rotation(s: dict[str, Any]) -> str:
    for sd in s.get("side_data_list") or []:
        if "displaymatrix" in sd:
            return " ".join(str(sd["displaymatrix"]).split())
    return str((s.get("tags") or {}).get("rotate", "0"))


async def _verify(src: Path, dst: Path, v: dict[str, Any], p: Plan, want: list[bytes], audio_end: float) -> None:
    info = await _probe_json(dst, "-show_streams", "-show_data", "-show_format")
    vids = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if len(vids) != 1:
        raise NotSafe("the joined file lost its video track")
    o = vids[0]
    if _param_sets(o) != want:
        raise NotSafe("the joined file's stream settings differ from the original's")
    for k in ("codec_name", "profile", "level", "pix_fmt", "width", "height", "color_space", "color_transfer",
              "color_primaries", "color_range", "sample_aspect_ratio"):
        if _norm(o.get(k)) != _norm(v.get(k)):
            raise NotSafe(f"the joined file's {k.replace('_', ' ')} differs from the original's")
    if _rotation(o) != _rotation(v):
        raise NotSafe("the joined file lost the video's rotation")
    otb = Fraction(o["time_base"])
    pk = await _packets(dst, o["index"])
    head, tail = p.head, p.tail
    if len(pk) != len(p.frames) or any(b.dts <= a.dts for a, b in zip(pk, pk[1:])) or any(x.pts < x.dts for x in pk):
        raise NotSafe("the joined file's frame count or order is off")
    want_t = [(f - p.frames[0]) * p.tb for f in p.frames]
    got_t = sorted(Fraction(x.pts) * otb for x in pk)
    if [t - got_t[0] for t in got_t] != want_t:
        raise NotSafe("the joined file's frame timing differs from the original's")
    copied = pk[len(head):len(head) + len(p.body)]
    if [x.size for x in copied] != [x.size for x in p.body] or not copied[0].key:
        raise NotSafe("the copied part differs from the original")
    # visszadekódolás az illesztések körül: hiba nélkül, és a másolt kockák md5-je a forrásé
    off = p.frames[0] * p.tb - p.t0
    n1 = len(head) + len([f for f in p.frames if p.k1 <= f < p.k1 + CHECK_S / p.tb and (p.k2 is None or f < p.k2)])
    a, b = await asyncio.gather(_md5s(dst, o["index"], -otb / 2, n1), _md5s(src, v["index"], off - p.tb / 2, n1))
    if len(a) != n1 or a[len(head):] != b[len(head):n1]:
        raise NotSafe("the copied part doesn't decode exactly like the original after the first cut")
    if p.k2 is not None:
        keys = [x.pts for x in p.body if x.key and x.pts <= p.k2 - CHECK_S / p.tb] or [p.k1]
        kb = keys[-1]
        nb = len([f for f in p.frames if kb <= f < p.k2])
        ss = (kb - p.frames[0]) * p.tb
        a, b = await asyncio.gather(_md5s(dst, o["index"], ss - otb / 2, nb + len(tail)),
                                    _md5s(src, v["index"], kb * p.tb - p.t0 - p.tb / 2, nb))
        if len(a) != nb + len(tail) or a[:nb] != b:
            raise NotSafe("the copied part doesn't decode exactly like the original before the second cut")
    for s in info.get("streams", []):
        if s.get("codec_type") == "audio":
            st, du = float(s.get("start_time") or 0), float(s.get("duration") or 0)
            if abs(st - float(o.get("start_time") or 0)) > 0.05 or abs(st + du - audio_end) > 0.1:
                raise NotSafe("the sound wouldn't stay in sync with the picture")


# ---------- a vágás ----------

async def _smart(src: Path, dst: Path, start: Any, end: Any, info: dict[str, Any], acodec: list[str], work: Path) -> dict:
    v = _check_source(src, dst, info)
    x = await _x264_options(src, v)
    args = _x264_args(x, v)
    tb = Fraction(v["time_base"])
    t0 = Fraction(str(info.get("format", {}).get("start_time") or "0"))
    a = seconds(start) if start not in (None, "") else None
    b = seconds(end) if end not in (None, "") else None
    pk = await _packets(src, v["index"])
    p = plan(pk, tb, t0, a, b)
    want = _param_sets(await _stream_data(src, v["index"]))
    if not want:
        raise NotSafe("its stream settings couldn't be read")
    if p.head or p.tail:
        args = await _ps_match(src, v, p, args, want, work)
    files, jobs = [], []                            # (darab, hossz a concat-listába); fej, test, vég és a hang egyszerre
    enc = int(bool(p.head)) + int(bool(p.tail))     # a két határdarab kódolása osztozik a munka szálain
    if p.head:
        files.append((work / "head.mp4", p.secs(p.frames[0], p.k1)))
        jobs.append(_share(enc, _encode(src, v, p, p.head, args, files[-1][0])))
    files.append((work / "body.mp4", p.secs(p.k1, p.k2) if p.k2 is not None else None))
    jobs.append(_copy_body(src, v, p, files[-1][0]))
    if p.tail:
        files.append((work / "tail.mp4", None))
        jobs.append(_share(enc, _encode(src, v, p, p.tail, args, files[-1][0])))
    has_aux = any(s.get("codec_type") in ("audio", "subtitle") for s in info.get("streams", []))
    aux = work / f"aux{dst.suffix.lower()}"
    auds = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    copy = auds and t0 == 0 and all(s.get("codec_name") == "aac" and int(s.get("sample_rate") or 0) > 0 for s in auds)
    if has_aux:
        jobs.append(_aux(src, p, b, acodec, Fraction(2048, int(auds[0]["sample_rate"])) if copy else None, aux))
    for r in await asyncio.gather(*jobs, return_exceptions=True):
        if isinstance(r, BaseException):
            raise r
    body = await _packets(work / "body.mp4", 0)
    if [x.size for x in body] != [x.size for x in p.body] or not body[0].key:
        raise NotSafe("its keyframes couldn't be copied exactly")
    for f, _ in files:
        if f.name != "body.mp4" and _param_sets(await _stream_data(f, 0)) != want:
            raise NotSafe("its cut points can't be re-encoded with exactly the same stream settings")
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{f.name}'\n" + (f"duration {d:.6f}\n" if d is not None else "") for f, d in files))
    # a kép a darabokból, a hang/felirat/fejezetek a segédfájlból, a fájl- és a képsáv-metaadat (elforgatás is) a forrásból
    ins, maps, meta = ["-i", str(lst)], ["-map", "0:v:0"], []
    if has_aux:
        n_aux = len((await _probe_json(aux, "-show_streams")).get("streams", []))
        ins += ["-i", str(aux)]
        maps += ["-map", "1"]
        meta += [o for j in range(n_aux) for o in (f"-map_metadata:s:{j + 1}", f"1:s:{j}")] + ["-map_chapters", "1"]
    else:
        meta += ["-map_chapters", "-1"]
    si = len(ins) // 2
    meta += ["-map_metadata", str(si), "-map_metadata:s:0", f"{si}:s:{v['index']}"]
    rot = str((v.get("tags") or {}).get("rotate") or "0")
    if rot.lstrip("-").isdigit() and int(rot) % 360:    # a kijelzőmátrix az első (újrakódolt) darabból hiányzik; az ffmpeg 4.4
        meta += ["-metadata:s:v:0", f"rotate={-int(rot) % 360}"]   # a rotate=X kapcsolóból −X fokos mátrixot ír (a címke X-et olvas)
    await _ff(*_FF, "-f", "concat", "-safe", "0", "-auto_convert", "0", *ins, "-i", str(src), *maps, "-c", "copy", *meta,
              "-video_track_timescale", str(tb.denominator), "-movflags", "+faststart", str(dst))
    aud = [s for s in info.get("streams", []) if s.get("codec_type") == "audio" and s.get("codec_name")]
    a_end = (float(aud[0].get("start_time") or 0) + float(aud[0].get("duration") or 0)) if aud else 0.0
    if b is not None:
        a_end = min(a_end, float(t0 + b))
    await _verify(src, dst, v, p, want, a_end - float(p.frames[0] * tb))
    re_s = (p.secs(p.frames[0], p.k1) if p.head else 0.0) + (p.secs(p.k2, p.frames[-1]) + float(p.last_dur * tb) if p.tail else 0.0)
    total = p.secs(p.frames[0], p.frames[-1]) + float(p.last_dur * tb)
    copied = max(0.0, total - re_s)
    where = " and ".join(w for w, on in (("the start", p.head), ("the end", p.tail)) if on)
    return {"method": "smart cut", "copied_seconds": round(copied, 3), "reencoded_seconds": round(re_s, 3),
            "note": f"Smart cut: {_clock(copied)} of the video was copied untouched"
                    + (f"; only {re_s:.1f} s at {where} was re-encoded, up to the nearest keyframe." if re_s else
                       "; the cut points fall on keyframes, so nothing was re-encoded.")}


async def trim(src: Path, dst: Path, start: Any, end: Any, info: dict[str, Any], acodec: list[str],
               reencode: Callable[[], Awaitable[Any]]) -> dict[str, Any]:
    """Smart cut, ha biztonságos; különben a teljes újrakódolás (reencode), és a válasz megmondja, miért."""
    why = "smart cut is switched off on this server"
    if enabled():
        work = dst.parent / f".smart-{dst.stem}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            return await _smart(src, dst, start, end, info, acodec, work)
        except NotSafe as e:
            why = str(e)
        except Exception as e:                      # váratlan hiba a gyors úton: a régi út mindig megmarad
            log.warning("smart trim of %s stopped: %s", src.name, e)
            why = "the quick cut stopped with an error"
        finally:
            shutil.rmtree(work, ignore_errors=True)
        dst.unlink(missing_ok=True)
    await reencode()
    return {"method": "re-encode", "note": f"The whole clip was re-encoded because {why}."}
