"""Videóeffektek CPU-n, ffmpeg-gel (OMLETT 3.2): stabilizálás, zajszűrés, élesítés, színek és fények, LUT-presetek,
„auto” (signalstats-mérésből), előtte/utána osztott előnézet. Egy beállítás-szótár (adjust) mindenhol ugyanaz:
az `adjust-video` eszköz (web, REST, MCP file_tools) lapos opciói, és a `video` eszköz szerkesztésében
`changes.adjust` (a saját felvétel — az overlay-videó alapklipje — kapja).

    adj = normalize({"auto": True, "lut": "Meleg konyha", "sharpen": 0.3})
    info = await apply(src, dst, adj, work)       # → {"applied": [...], "auto": {...}, "measured": {...}}
    png = await preview(src, adj, work)           # 4 kocka, bal fele előtte, jobb fele utána

Sorrend: HDR → SDR (videonorm) → stabilizálás (vidstab két menetben; nélküle deshake) → zajszűrés (hqdn3d gyors /
nlmeans minőségi) → LUT → szintek, árnyékok, csúcsfények, csúcsfény-lekerekítés (curves) → eq (fényerő, kontraszt,
gamma, telítettség) → színhőmérséklet → vibrance → élesítés (cas / unsharp) a végén.
A kiégett (clipped) csúcsfény nem állítható vissza — ami 100%-on van, ott nincs rajzolat —, csak tompítható
(highlight_rolloff: a fehér szürkésfehérré hajlik, a környezete részletesebb marad).
"""
from __future__ import annotations

import asyncio
import functools
import math
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional

from app.convert.options import OptionError

# a `video` eszköz action="schema" referenciájába (a video sáv köti be) — és a lead összefésüléséhez
REFERENCE = """adjust — colour, light and clean-up for your own footage (CPU, ffmpeg). In the video tool: action="edit",
changes={"adjust": {…}} on an overlay video (your footage is the clip it changes; {} or "none" = back to the original).
As a file tool: file_tools(tool="adjust-video", options={…}) — the same keys; preview=true returns a before/after
picture of 4 frames (left half before, right half after) instead of the whole video.
  auto: true — measures the clip (signalstats: average, darkest and brightest luma, saturation) and sets levels,
        gamma, saturation and highlight roll-off; your own values win over it.
  brightness -1…1 (0) · contrast 0…3 (1) · gamma 0.1…3 (1; >1 brighter mid-tones) · saturation 0…3 (1; 0 = grey)
  shadows -1…1 (0; + lifts the dark parts) · highlights -1…1 (0; − tones the bright parts down)
  highlight_rolloff 0…1 (0) — bends the brightest tones down softly (white → off-white). Clipped (burnt-out)
        highlights can't be recovered, only toned down: there is no detail left in them.
  temperature 1000…40000 K (6500 = as shot; lower = warmer/orange, higher = cooler/blue) · vibrance -2…2 (0; lifts
        the dull colours more than the strong ones, skin stays natural)
  lut: warm_kitchen ("Meleg konyha") | news_studio ("Hírstúdió") | cinema ("Mozi") | black_white ("Fekete-fehér")
  denoise: off | fast (hqdn3d) | quality (nlmeans, slow) · sharpen 0…1 (0) with sharpen_method cas (default) | unsharp
  stabilize: true — two-pass vidstab (deshake where vidstab is missing); the frame is zoomed in slightly to hide edges.
Example: {"auto": true, "lut": "news_studio", "denoise": "fast", "sharpen": 0.3, "highlight_rolloff": 0.4}"""

LUTS = {"warm_kitchen": "Meleg konyha", "news_studio": "Hírstúdió", "cinema": "Mozi", "black_white": "Fekete-fehér"}
DENOISE = ("off", "fast", "quality")
SHARPEN = ("cas", "unsharp")
# kulcs → (típus, min, max, semleges érték)
_NUM: dict[str, tuple[float, float, float]] = {
    "brightness": (-1, 1, 0), "contrast": (0, 3, 1), "gamma": (0.1, 3, 1), "saturation": (0, 3, 1),
    "shadows": (-1, 1, 0), "highlights": (-1, 1, 0), "highlight_rolloff": (0, 1, 0),
    "temperature": (1000, 40000, 6500), "vibrance": (-2, 2, 0), "sharpen": (0, 1, 0),
}
KEYS = tuple(_NUM) + ("auto", "lut", "denoise", "sharpen_method", "stabilize")


def _plain(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


_LUT_ALIASES = {**{k: k for k in LUTS}, **{_plain(v): k for k, v in LUTS.items()},
                "warm": "warm_kitchen", "kitchen": "warm_kitchen", "news": "news_studio", "studio": "news_studio",
                "film": "cinema", "movie": "cinema", "bw": "black_white", "mono": "black_white", "monochrome": "black_white",
                "fekete_feher": "black_white", "hirstudio": "news_studio", "mozi": "cinema", "meleg_konyha": "warm_kitchen"}


def lut_key(name: Any) -> str:
    k = _LUT_ALIASES.get(_plain(name or ""))
    if not k:
        raise OptionError("lut: " + " | ".join(f"{k} (\"{v}\")" for k, v in LUTS.items()))
    return k


def normalize(raw: Any) -> dict[str, Any]:
    """A beállítás-szótár ellenőrizve, a semleges értékek nélkül. Ismeretlen kulcs → OptionError (példával)."""
    if raw in (None, "", "none", "off", False):
        return {}
    if not isinstance(raw, dict):
        raise OptionError('adjust: an object, e.g. {"auto": true, "lut": "news_studio", "sharpen": 0.3}')
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k in ("preview",):
            continue
        if k not in KEYS:
            raise OptionError(f"adjust doesn't take {k!r}. Keys: {', '.join(KEYS)}")
        if v is None:
            continue
        if k in _NUM:
            lo, hi, neutral = _NUM[k]
            try:
                x = float(v)
            except (TypeError, ValueError):
                raise OptionError(f"adjust.{k}: a number {lo:g}…{hi:g}")
            if isinstance(v, bool) or not lo <= x <= hi:
                raise OptionError(f"adjust.{k}: a number {lo:g}…{hi:g} ({neutral:g} = unchanged)")
            if x != neutral:
                out[k] = x
        elif k in ("auto", "stabilize"):
            if isinstance(v, str):
                v = v.strip().lower() in ("1", "true", "yes", "on", "igen")
            if v:
                out[k] = True
        elif k == "lut":
            if str(v).strip().lower() not in ("", "none", "off"):
                out[k] = lut_key(v)
        elif k == "denoise":
            d = {True: "fast", False: "off"}.get(v, str(v).strip().lower()) if isinstance(v, bool) else str(v).strip().lower()
            if d not in DENOISE:
                raise OptionError(f"adjust.denoise: {' | '.join(DENOISE)}")
            if d != "off":
                out[k] = d
        elif k == "sharpen_method":
            if str(v) not in SHARPEN:
                raise OptionError(f"adjust.sharpen_method: {' | '.join(SHARPEN)}")
            out[k] = str(v)
    return out


# ---------- LUT-ok (.cube, generálva — egyszerű táblázatok) ----------

def _luma(r: float, g: float, b: float) -> float:
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _sat(rgb: tuple[float, float, float], k: float) -> tuple[float, ...]:
    y = _luma(*rgb)
    return tuple(y + (c - y) * k for c in rgb)


def _s_curve(x: float, k: float) -> float:
    """Lágy S-görbe: a közép marad, a negyedeknél ±k (k ≤ 0,15: monoton)."""
    return x - k * math.sin(2 * math.pi * x)


def _warm_kitchen(r, g, b):          # Meleg konyha: meleg fény, telt, étvágygerjesztő színek, lágy kontraszt
    r, g, b = r * 1.05 + 0.012, g * 1.0 + 0.006, b * 0.88
    r, g, b = _sat((r, g, b), 1.10)
    return tuple(_s_curve(c, 0.03) for c in (r, g, b))


def _news_studio(r, g, b):           # Hírstúdió: tiszta, enyhén hűvös, feszes kontraszt, természetes bőrtónus
    r, g, b = r * 0.985, g, b * 1.03
    r, g, b = _sat((r, g, b), 1.05)
    return tuple(_s_curve(c, 0.05) for c in (r, g, b))


def _cinema(r, g, b):                # Mozi: teal-narancs, emelt fekete, tompított csúcsfények, visszafogott telítettség
    y = _luma(r, g, b)
    r, g, b = r - 0.045 * (1 - y) + 0.05 * y, g + 0.01 * (1 - y), b + 0.04 * (1 - y) - 0.05 * y
    r, g, b = _sat((r, g, b), 0.88)
    r, g, b = (_s_curve(c, 0.04) for c in (r, g, b))
    return tuple(0.035 + c * (0.95 - 0.035) for c in (r, g, b))


def _black_white(r, g, b):           # Fekete-fehér: BT.709 fénysűrűség, enyhe S-görbe
    y = _s_curve(_luma(r, g, b), 0.05)
    return y, y, y


_LUT_FN: dict[str, Callable[..., tuple[float, ...]]] = {"warm_kitchen": _warm_kitchen, "news_studio": _news_studio,
                                                       "cinema": _cinema, "black_white": _black_white}


def cube_text(key: str, size: int = 17) -> str:
    fn = _LUT_FN[key]
    lines = [f'TITLE "{key}"', f"LUT_3D_SIZE {size}", "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]
    n = size - 1
    for bi in range(size):              # a .cube sorrend: R változik a leggyorsabban
        for gi in range(size):
            for ri in range(size):
                out = fn(ri / n, gi / n, bi / n)
                lines.append(" ".join(f"{min(1.0, max(0.0, c)):.5f}" for c in out))
    return "\n".join(lines) + "\n"


def write_lut(key: str, work: Path) -> str:
    """A LUT a munkamappába (relatív név: a szűrőgráfban nincs útvonal-escape)."""
    name = f"lut-{key}.cube"
    p = work / name
    if not p.exists():
        p.write_text(cube_text(key), "ascii")
    return name


# ---------- mérés és „auto” ----------

@functools.lru_cache(maxsize=None)
def has_filter(name: str) -> bool:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return False
    return re.search(rf"\s{re.escape(name)}\s", out) is not None


async def measure(src: Path, head: Optional[list[str]] = None) -> dict[str, float]:
    """signalstats a kulcskockákon (gyors: csak azokat dekódolja): YAVG, YMIN, YMAX, YLOW, YHIGH, SATAVG (8 bit)."""
    from app.convert.engines import run
    vf = ",".join((head or ["format=yuv420p"]) + ["signalstats", "metadata=print:file=-"])
    out, _ = await run("ffmpeg", "-hide_banner", "-skip_frame", "nokey", "-i", str(src), "-map", "0:v:0", "-vf", vf,
                       "-vsync", "0", "-frames:v", "240", "-f", "null", "-", timeout=600)
    vals: dict[str, list[float]] = {}
    for k, v in re.findall(r"lavfi\.signalstats\.(YAVG|YMIN|YMAX|YLOW|YHIGH|SATAVG)=([\d.]+)", out.decode("utf-8", "replace")):
        vals.setdefault(k, []).append(float(v))
    if not vals:
        raise ValueError("couldn't measure the picture (no frame decoded)")
    return {k: round(sum(v) / len(v), 2) for k, v in vals.items()}


def auto_params(st: dict[str, float]) -> dict[str, Any]:
    """Mérés → szintek (fekete/fehér pont), gamma, telítettség, csúcsfény-lekerekítés. Óvatos: csak a nyilvános hibát
    javítja (fakó, sötét, beégett), és korlátosan."""
    n = lambda v: min(1.0, max(0.0, (v - 16) / 219))           # noqa: E731 — TV-tartomány → 0…1
    lo, hi = n(st.get("YLOW", st.get("YMIN", 16))), n(st.get("YHIGH", st.get("YMAX", 235)))
    blo, bhi = n(st.get("YMIN", 16)), n(st.get("YMAX", 235))
    p: dict[str, Any] = {}
    black = round(min(blo, 0.12), 3) if blo > 0.04 else 0.0      # fakó, „ködös” kép: a legsötétebb rész nem fekete
    white = round(max(bhi, 0.82), 3) if bhi < 0.94 else 1.0      # a legvilágosabb nem fehér
    if black or white < 1:
        p["levels"] = [black, white]
    y = n(st.get("YAVG", 126))
    y = min(0.98, max(0.02, (y - black) / max(0.2, white - black)))
    target = 0.45
    if abs(y - target) > 0.06:
        p["gamma"] = round(min(1.6, max(0.7, math.log(y) / math.log(target))), 3)
    sat = st.get("SATAVG", 40)
    if sat < 22:
        p["saturation"] = round(min(1.35, max(1.0, 28 / max(sat, 1))), 3)
    if st.get("YMAX", 0) >= 234 and hi > 0.6:
        p["highlight_rolloff"] = 0.35                            # forró csúcsfények: tompítva (vissza nem hozhatók)
    return p


# ---------- szűrőlánc ----------

def _curve_points(levels: Optional[list[float]], shadows: float, highlights: float, rolloff: float) -> Optional[str]:
    if not levels and not shadows and not highlights and not rolloff:
        return None
    b, w = levels or (0.0, 1.0)
    bump = lambda x, c: max(0.0, 1 - ((x - c) / 0.25) ** 2)    # noqa: E731

    def f(x: float) -> float:
        y = min(1.0, max(0.0, (x - b) / max(1e-3, w - b)))
        y += 0.12 * shadows * bump(y, 0.25) + 0.12 * highlights * bump(y, 0.75)
        y = min(1.0, max(0.0, y))
        k = 0.75
        if rolloff and y > k:
            t = (y - k) / (1 - k)
            y = k + (y - k) * (1 - 0.4 * rolloff * t)
        return y
    pts, last = [], -1.0
    for i in range(9):
        x = i / 8
        y = max(last, f(x))
        last = y
        pts.append(f"{x:.3f}/{y:.3f}")
    return " ".join(pts)


def chain(adj: dict[str, Any], work: Path, auto: Optional[dict[str, Any]] = None, temporal: bool = True) -> list[str]:
    """A színes/tisztító szűrők (a stabilizálás nélkül) a beállításból; auto: az auto_params eredménye (a felhasználó
    saját értéke felülírja). temporal=False: az előnézet egyetlen kockáján (a hqdn3d időbeli része ott mindegy)."""
    a = {**(auto or {}), **adj}
    f: list[str] = []
    if a.get("denoise") == "fast":
        f.append("hqdn3d=3:2:4:3" if temporal else "hqdn3d=3:2:0:0")
    elif a.get("denoise") == "quality":
        f.append("nlmeans=s=3:p=7:r=9" if has_filter("nlmeans") else "hqdn3d=4:3:6:4.5")
    if a.get("lut"):
        f.append(f"lut3d=file={write_lut(a['lut'], work)}:interp=tetrahedral")
    pts = _curve_points(a.get("levels"), float(a.get("shadows") or 0), float(a.get("highlights") or 0),
                        float(a.get("highlight_rolloff") or 0))
    if pts:
        f.append(f"curves=all='{pts}'")
    eq = [f"{k}={float(a[k]):.3f}" for k in ("brightness", "contrast", "gamma", "saturation") if k in a]
    if eq:
        f.append("eq=" + ":".join(eq))
    if "temperature" in a and has_filter("colortemperature"):
        f.append(f"colortemperature=temperature={int(a['temperature'])}:pl=1")
    if a.get("vibrance") and has_filter("vibrance"):
        f.append(f"vibrance=intensity={float(a['vibrance']):.3f}")
    if a.get("sharpen"):
        s = float(a["sharpen"])
        if a.get("sharpen_method") == "unsharp" or not has_filter("cas"):
            f.append(f"unsharp=5:5:{0.3 + 1.2 * s:.2f}:5:5:0")
        else:
            f.append(f"cas=strength={min(1.0, 0.2 + 0.8 * s):.2f}")
    return f


def describe(adj: dict[str, Any], auto: Optional[dict[str, Any]] = None) -> list[str]:
    """Mit állított be (a felhasználónak, a check verified-listájába)."""
    out = []
    if auto:
        bits = []
        if auto.get("levels"):
            bits.append(f"levels {auto['levels'][0]:.2f}–{auto['levels'][1]:.2f} → 0–1")
        bits += [f"{k} {auto[k]:g}" for k in ("gamma", "saturation", "highlight_rolloff") if k in auto]
        out.append("Auto: " + (", ".join(bits) if bits else "the picture needed nothing") + ".")
    own = {k: v for k, v in adj.items() if k != "auto"}
    if own:
        out.append("Adjusted: " + ", ".join(f"{k} {LUTS.get(v, v) if k == 'lut' else v}" for k, v in own.items()) + ".")
    if adj.get("highlight_rolloff") or (auto or {}).get("highlight_rolloff"):
        out.append("Clipped highlights can't be recovered, only toned down.")
    return out


# ---------- futtatás ----------

async def _stabilize_pass(src: Path, head: list[str], work: Path) -> list[str]:
    """Stabilizálás: vidstab két menetben (első: mozgás → transforms.trf), különben deshake (egy menet)."""
    from app.convert.engines import run
    if not (has_filter("vidstabdetect") and has_filter("vidstabtransform")):
        return ["deshake"]
    vf = ",".join(head + ["vidstabdetect=shakiness=6:accuracy=15:result=transforms.trf"])
    await run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-map", "0:v:0", "-vf", vf, "-f", "null", "-",
              timeout=3600, cwd=work)
    return ["vidstabtransform=input=transforms.trf:smoothing=15:optzoom=1:zoomspeed=0.25:interpol=bicubic"]


async def apply(src: Path, dst: Path, adj: dict[str, Any], work: Path) -> dict[str, Any]:
    """A felvétel újrakódolva a beállítással (egy menet; stabilizálásnál előtte egy mérő menet). → mit csinált."""
    from app.convert.engines import run
    from app.ops.handlers.media import _acodec_for, _faststart, _fmt, _vcodec_for
    from app.tools import mediaprobe, videonorm
    work.mkdir(parents=True, exist_ok=True)
    m = await mediaprobe.info(src)
    if not m["has_video"]:
        raise OptionError(f"{src.name} has no picture to adjust")
    head = videonorm.head_filters(m)
    measured = auto = None
    if adj.get("auto"):
        measured = await measure(src, head)
        auto = auto_params(measured)
    stab = await _stabilize_pass(src, head, work) if adj.get("stabilize") else []
    vf = head + stab + chain(adj, work, auto) + ["scale=trunc(iw/2)*2:trunc(ih/2)*2"]
    fmt = _fmt(dst)
    a = ["-map", f"0:a:{m['audio_stream'] or 0}"] + (["-c:a", "copy"] if m["acodec"] == "aac" and fmt in ("mp4", "mov", "m4v")
                                                     else _acodec_for(fmt)) if m["has_audio"] else ["-an"]
    await run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-map", "0:v:0", *a, "-vf", ",".join(vf),
              *_vcodec_for(fmt), *_faststart(fmt), str(dst), timeout=3600, cwd=work)
    return {"settings": adj, "auto": auto, "measured": measured, "filters": vf,
            "applied": describe(adj, auto) + (["Stabilized (" + ("vidstab, two passes" if stab[0] != "deshake" else "deshake")
                                               + ")."] if stab else [])}


async def preview(src: Path, adj: dict[str, Any], work: Path, frames: int = 4, width: int = 1600) -> tuple[Path, dict[str, Any]]:
    """Előtte/utána: `frames` kocka a klip hosszában, mindegyik bal fele az eredeti, jobb fele a beállított (a
    stabilizálás az előnézetben nem látszik — az egész klip mozgásából számol). → (PNG, mit állított)"""
    from PIL import Image, ImageDraw
    from app.convert.engines import run
    from app.tools import mediaprobe, videonorm
    work.mkdir(parents=True, exist_ok=True)
    m = await mediaprobe.info(src)
    head = videonorm.head_filters(m)
    measured = auto = None
    if adj.get("auto"):
        measured = await measure(src, head)
        auto = auto_params(measured)
    fx = ",".join(chain(adj, work, auto, temporal=False) or ["null"])
    dur = max(0.1, float(m["duration"] or 0))
    tiles = []
    for i in range(frames):
        t = dur * (0.1 + 0.8 * i / max(1, frames - 1))
        b, a = work / f"pv-{i}-b.png", work / f"pv-{i}-a.png"
        await run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", str(src), "-filter_complex",
                  f"[0:v]{','.join(head)},split[x][y];[y]{fx}[z]", "-map", "[x]", "-frames:v", "1", b.name, "-map", "[z]",
                  "-frames:v", "1", a.name, timeout=300, cwd=work)
        if not b.exists() or not a.exists():
            continue

        def split(b=b, a=a):
            with Image.open(b) as ib, Image.open(a) as ia:
                ib, ia = ib.convert("RGB"), ia.convert("RGB")
                w, h = ib.size
                out = ib.copy()
                out.paste(ia.crop((w // 2, 0, w, h)), (w // 2, 0))
                d = ImageDraw.Draw(out)
                d.rectangle((w // 2 - 1, 0, w // 2 + 1, h), fill=(255, 255, 255))
                for x0, label in ((8, "before"), (w // 2 + 8, "after")):
                    d.rectangle((x0 - 4, 6, x0 + 7 * len(label) + 6, 24), fill=(0, 0, 0))
                    d.text((x0, 9), label, fill=(255, 255, 255))
                return out
        tiles.append(await asyncio.to_thread(split))
    if not tiles:
        raise ValueError("couldn't read a frame for the preview")

    def sheet() -> Path:
        cols = 2 if len(tiles) > 1 else 1
        tw = min(tiles[0].width, width // cols)
        th = round(tiles[0].height * tw / tiles[0].width)
        rows = math.ceil(len(tiles) / cols)
        canvas = Image.new("RGB", (cols * tw + (cols - 1) * 6, rows * th + (rows - 1) * 6), (20, 20, 24))
        for i, im in enumerate(tiles):
            canvas.paste(im.resize((tw, th)), ((i % cols) * (tw + 6), (i // cols) * (th + 6)))
        p = work / "preview.png"
        canvas.save(p)
        return p
    p = await asyncio.to_thread(sheet)
    return p, {"settings": adj, "auto": auto, "measured": measured, "applied": describe(adj, auto)}


async def render_file(src: Path, adj: dict[str, Any], label: str = "footage adjusted") -> tuple[str, dict[str, Any]]:
    """A beállított felvétel rejtett munkafájlként (a videó szerkesztésének alapklipje) → (file_id, mit csinált)."""
    import tempfile
    from app.storage import output_path, temp_path, write_meta
    from app.storage.files import new_file_id
    fid = new_file_id("mp4")
    work = Path(tempfile.mkdtemp(prefix="fx-", dir=str(temp_path("").parent)))
    try:
        tmp = work / "adjusted.mp4"
        info = await apply(src, tmp, adj, work)
        shutil.move(str(tmp), output_path(fid))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    write_meta(fid, operation=label, source_file_id=None, extra={"ephemeral": True, "adjust": adj})
    return fid, info
