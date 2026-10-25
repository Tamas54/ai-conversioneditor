"""A saját felvétel útja (10-22, a gyors render 2. lépése): a szerkesztő kis munkamásolatot kap, a végső kép az eredetiből.

Eddig a telefonos felvétel a projekt előtt teljes méretben újra lett kódolva (felállítva, HDR → SDR), a zárókártyához még
egyszer (a vége kimerevítve), a színkorrekcióhoz még egyszer — mérve, egy 30 perces HDR iPhone-felvételen 25,5 + 10,3
perc, mielőtt a szerkesztés egyáltalán elkezdődött, és a végső kép így egy köztes kódolásból készült.

Most:
- **munkamásolat** (proxy): az eredetiből egy menetben, kicsiben (a hosszabbik oldal ≤ PROXY_SIDE), felállítva, HDR → SDR,
  a korrekcióval — ezt látja a szerkesztő és a motor. A projektben a felvétel-réteg a kompozíció méretére nagyítva áll
  (D.base.scale), így a rétegek, a ráközelítés, az osztott kép a teljes méret koordinátáiban maradnak;
- **a zárókártya kimerevített vége** a projektben időtérkép (a réteg az utolsó kockán áll, a hang ott csend) — nincs
  meghosszabbított fájl;
- **a végső kép az eredetiből**: a gyors render a felvételt maga olvassa ugyanezzel a lánccal (chain), teljes méretben;
  ahol a motor rajzolja a felvételt (ráközelítés, osztott kép), ott csak arra a szakaszra készül teljes méretű darab
  (clip), és a szakasz projektváltozatában a munkamásolat helyére kerül (swap). A régi (teljes) render a teljes hosszú
  változatot kapja (full).

A stabilizálás az egész felvétel mozgásából számol: ott a teljes hosszú változat készül el, és minden ebből dolgozik.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Optional

PROXY_SIDE = 960          # a munkamásolat hosszabbik oldala (px): a szerkesztő előnézete, a motor rajza ebből nem lesz végső kép
PROXY_CRF = 22
PROXY_BUDGET_MB = 400     # a böngésző-szerkesztő az egész fájlt a memóriába tölti (index.html: fetch → arrayBuffer) — a
                          # bitráta a hossz szerint ehhez igazodik (mérve: 540p, CRF 20 ~1,35 Mbit/s; a régi teljes méretű
                          # munkafájl egy 30 perces videónál ~2 GB lett volna)
PROXY_BYTES = 200 * 1024 * 1024    # ennél nagyobb felvételhez akkor is munkamásolat, ha a mérete kicsi


def size(W: int, H: int) -> tuple[int, int]:
    """A munkamásolat mérete (páros, az arány marad); a PROXY_SIDE-nál kisebb felvétel a saját méretén."""
    k = min(1.0, PROXY_SIDE / max(W, H, 1))
    return max(2, round(W * k / 2) * 2), max(2, round(H * k / 2) * 2)


def needed(m: dict[str, Any], adjust: Optional[dict[str, Any]] = None, nbytes: int = 0,
           keep: Optional[list[list[float]]] = None) -> bool:
    """Kell-e munkamásolat: a motor a tárolt (elforgatás nélküli) kockát olvasná, a HDR-t SDR-nek venné, korrekciót nem tud;
    és a böngésző-szerkesztő az egész fájlt a memóriába tölti — a nagy felvételt (méretben vagy bájtban) se kapja meg."""
    W, H = m.get("display_width") or m.get("width") or 0, m.get("display_height") or m.get("height") or 0
    return bool((m.get("rotation") or 0) % 360 or m.get("hdr") or adjust or max(W, H) > PROXY_SIDE or nbytes > PROXY_BYTES
                or keep)


def _rate(dur: float) -> list[str]:
    """A munkamásolat bitrátája: CRF, a hossz szerint felső korláttal (PROXY_BUDGET_MB), kulcskocka másodpercenként a
    görgetéshez."""
    cap = int(min(4000, PROXY_BUDGET_MB * 8000 / max(dur, 1.0)))
    return ["-crf", str(PROXY_CRF), "-maxrate", f"{cap}k", "-bufsize", f"{2 * cap}k"]


def original(base: dict[str, Any]) -> Optional[Path]:
    """Az eredeti felvétel, ha még megvan (a feltöltések a megőrzési idő után lejárnak — app/storage/cleanup.py)."""
    from app.tools import crafts
    try:
        return crafts.existing(base["file_id"])
    except (FileNotFoundError, KeyError):
        return None


def gone_note(base: dict[str, Any]) -> str:
    p = base.get("proxy") or {}
    return (f"Your original video is no longer stored (uploads are kept for a limited time), so this render was made from "
            f"the editor's small copy ({p.get('w')}×{p.get('h')}) — upload the original again and edit the video to get it "
            "in full size.")


def stabilized(base: dict[str, Any]) -> bool:
    return bool((base.get("adjust") or {}).get("stabilize"))


def chain(base: dict[str, Any], m: dict[str, Any], work: Path) -> list[str]:
    """Az eredetitől a projekt képéig (az elforgatást az ffmpeg dekódoláskor magától elvégzi): HDR → SDR, a korrekció a
    tárolt auto-méréssel (a munkamásolat és a végső kép ugyanazt kapja). A LUT a munkamappába kerül (relatív név: az
    ffmpeg cwd-je a work). A stabilizálás nincs benne (az egész felvétel mozgásából számol: full)."""
    from app.tools import video_fx, videonorm
    f = videonorm.tonemap_filters(m)
    adj = {k: v for k, v in (base.get("adjust") or {}).items() if k != "stabilize"}
    if adj:
        f += video_fx.chain(adj, work, base.get("auto"))
    return f


_KEEP = ("media_id", "asset", "proxy", "rotated", "hdr", "auto")
_BUSY: dict[str, asyncio.Lock] = {}


async def make_proxy(base: dict[str, Any], src: Path) -> tuple[dict[str, Any], list[str]]:
    """A munkamásolat → (az új base: media_id, asset, proxy-méret, rotated, hdr, auto; a korrekció leírása). Ugyanabból a
    feltöltésből ugyanazzal a korrekcióval egyszer készül: a következő videó (újabb változat ugyanarról a felvételről) a
    meglévőt kapja (az eredeti metájában: proxies), amíg a fájlja megvan."""
    from app.storage import read_meta, write_meta
    from app.tools import crafts
    fid0 = str(base.get("file_id") or "")
    key = json.dumps({"v": 2, "side": PROXY_SIDE, "budget": PROXY_BUDGET_MB, "adjust": base.get("adjust") or {},
                      "keep": base.get("keep") or None}, sort_keys=True)
    async with _BUSY.setdefault(fid0 + key, asyncio.Lock()):
        hit = ((read_meta(fid0) or {}).get("proxies") or {}).get(key) if fid0 else None
        try:
            if hit:
                crafts.existing(hit["media_id"])
                b = {k: v for k, v in base.items() if k not in _KEEP + ("clean_media_id",)}
                return {**b, **{k: hit[k] for k in _KEEP if k in hit}}, list(hit.get("applied") or [])
        except FileNotFoundError:
            pass
        from app import jobs
        # a munkamásolat egy nehéz munka (app/jobs.py): sorban vár, ha minden hely foglalt; a szeletei egy helyen, a saját
        # -threads-ükkel. A gyorsítótárból jött másolat (fent) nem kér helyet: a szerkesztés előnézete nem vár.
        async with jobs.heavy(threads=False):
            b, applied = await _build_proxy(base, src)
        if fid0:
            have = (read_meta(fid0) or {}).get("proxies") or {}
            write_meta(fid0, extra={"proxies": {**have, key: {**{k: b[k] for k in _KEEP if k in b}, "applied": applied}}})
        return b, applied


async def _build_proxy(base: dict[str, Any], src: Path) -> tuple[dict[str, Any], list[str]]:
    import shutil
    import tempfile
    from app.convert.engines import run
    from app.config import settings
    from app.storage import output_path, temp_path, write_meta
    from app.storage.files import new_file_id
    from app.tools import encoding, fast_render, mediaprobe, video_fx, videonorm
    m = await mediaprobe.info(src)
    b = {k: v for k, v in base.items() if k not in ("media_id", "proxy", "auto", "clean_media_id", "rotated", "hdr")}
    W, H = int(m["display_width"] or b["width"]), int(m["display_height"] or b["height"])
    pw, ph = size(W, H)
    adj = b.get("adjust") or {}
    applied: list[str] = []
    work = Path(tempfile.mkdtemp(prefix="proxy-", dir=str(temp_path("").parent)))
    try:
        if adj.get("auto"):                                  # egyszer mérve, az eredetin; a végső kép ugyanezt kapja
            measured = await video_fx.measure(src, videonorm.head_filters(m))
            b["auto"] = video_fx.auto_params(measured)
        small = [f"scale={pw}:{ph}"] if (pw, ph) != (W, H) else []
        head = small + videonorm.tonemap_filters(m)
        keep = b.get("keep")
        fps = float(m.get("fps") or 30)
        tl = await timeline(b, src, W, H, float(b.get("fps") or fps), work, size=(pw, ph)) if keep else None
        dur = tl.length if tl else float(m.get("duration") or b.get("duration") or 0)
        gop = str(max(1, round(fps)))
        venc = ["-c:v", "libx264", "-preset", "veryfast", *_rate(dur), "-g", gop, "-pix_fmt", "yuv420p"]
        if tl:                      # vágott / beszúrt: forrásonként a saját láncával, az áttűnések rétegként
            stab = await _stab_detect_tl(tl, work) if adj.get("stabilize") else []
            post, vchain = stab + ["format=yuv420p"], ""
            items = await transitions(tl, work, "p", venc)
        else:
            stab = await _stab_detect(src, [(0.0, dur)], head, work) if adj.get("stabilize") else []
            own = {k: v for k, v in adj.items() if k != "stabilize"}
            vf = head + stab + (video_fx.chain(own, work, b.get("auto")) if adj else [])
            vchain = ",".join(vf + ["format=yuv420p"])
        slices = [(0.0, dur)] if stab else fast_render.chunks(dur, fps)
        threads = ["-threads", str(max(2, (os.cpu_count() or 4) // len(slices)))] if len(slices) > 1 else []

        async def piece(i: int, cs: float, ce: float) -> str:
            name = f"proxy-{i:03d}.mp4" if len(slices) > 1 else "proxy-v.mp4"
            if tl:
                await _tl_window(tl, cs, ce, post, items, venc + threads, work / name, work)
            else:
                await _video_window(src, [(cs, ce)], vchain, venc + threads, work / name, work)
            return name
        if len(slices) <= 1:
            await piece(0, 0.0, dur)
        else:                       # hosszú felvétel: időszeletek egyszerre (a szűrőlánc egy szálon fut), összefűzve
            names = await asyncio.gather(*(piece(i, cs, ce) for i, (cs, ce) in enumerate(slices)))
            (work / "proxy.txt").write_text("".join(f"file '{n}'\n" for n in names), "utf-8")
            await run("ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "proxy.txt", "-c", "copy", "proxy-v.mp4",
                      timeout=settings.VIDEO_TIMEOUT_S, cwd=work)
        k = m.get("audio_stream")
        if tl and any(i.get("audio_stream") is not None for i in tl.infos.values()):
            await tl_audio(tl, work / "proxy-a.m4a", work)      # a szakaszok hangja, úsztatva / áttűnve
            aud: list[str] = ["-i", "proxy-a.m4a", "-map", "1:a:0", "-c:a", "copy"]
        elif tl or k is None:
            aud = []
        else:                       # csak az első dekódolható hangsáv (az iPhone térhangja nélkül), AAC-nál másolva
            aud = ["-i", str(src), "-map", f"1:a:{k}", *(["-c:a", "copy"] if m.get("acodec") == "aac" else encoding.aac())]
        tmp = work / "proxy.mp4"
        await run("ffmpeg", "-v", "error", "-y", "-i", "proxy-v.mp4", *aud, "-map", "0:v:0", "-c:v", "copy", "-t", f"{dur:.4f}",
                  "-movflags", "+faststart", str(tmp), timeout=settings.VIDEO_TIMEOUT_S, cwd=work)
        fid = new_file_id("mp4")
        shutil.move(str(tmp), output_path(fid))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    write_meta(fid, operation="footage: the editor's copy", source_file_id=None, extra={"ephemeral": True})
    if adj:
        applied = video_fx.describe(adj, b.get("auto")) + (["Stabilized."] if stab else [])
    b.update(media_id=fid, asset=f"footage-{fid.split('.')[0][:8]}.mp4", proxy={"w": pw, "h": ph})
    if (m.get("rotation") or 0) % 360:
        b["rotated"] = int(m["rotation"])
    if m.get("hdr"):
        b["hdr"] = m["hdr"]
    return b, applied


async def clip(base: dict[str, Any], src: Path, dst: Path, a: float, b: float, plain: float, work: Path) -> None:
    """A videó [a, b) szakasza teljes méretben (vágott felvételnél a megtartott részekből), a lánccal (és b > plain
    esetén a kimerevített véggel) — a motor ezt rajzolja ott, ahol a felvétel maga mozog. Köztes fájl: a végső kódolás
    előtti minőségen (VIDEO_CRF_INTERMEDIATE)."""
    from app.tools import encoding, mediaprobe
    m = await mediaprobe.info(src)
    fps = float(m.get("fps") or 30)
    cs, ce = (a, min(b, plain)) if a < plain - 0.5 / fps else (plain - 1.5 / fps, plain)
    tail = [f"tpad=stop_mode=clone:stop_duration={b - plain + 1:.3f}"] if b > plain else []
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(encoding.intermediate_crf()), "-pix_fmt", "yuv420p"]
    if base.get("keep"):                     # vágott / beszúrt: az idővonal, forrásonként a saját láncával
        W, H = int(base.get("width") or m["display_width"]), int(base.get("height") or m["display_height"])
        tl = await timeline(base, src, W, H, float(base.get("fps") or fps), work)
        items = await transitions(tl, work, "f", enc)
        await _tl_window(tl, cs, ce, ["format=yuv420p"] + tail, items, ["-t", f"{b - a:.4f}"] + enc, dst, work)
        return
    vf = chain(base, m, work) + ["format=yuv420p"] + tail
    await _video_window(src, [(cs, ce)], ",".join(vf), ["-t", f"{b - a:.4f}"] + enc, dst, work)


async def _tl_window(tl: Timeline, cs: float, ce: float, post: list[str], items: list[dict[str, Any]], enc: list[str],
                     dst: Path, cwd: Path) -> None:
    """Az idővonal [cs, ce) ablaka (a fölé tett áttűnésekkel) → a post-lánc → kódolva, hang nélkül, 0-tól."""
    from app.config import settings
    from app.convert.engines import run
    from app.tools import fast_render
    ins, graph = fast_render.window_graph("", cs, ce, items, None, None, chunked=True, pre=[x for x in post if x], tl=tl)
    await run("ffmpeg", "-v", "error", "-y", *ins, "-filter_complex", graph, "-map", "[vout]", "-an",
              *(enc if "-t" in enc else ["-t", f"{ce - cs:.4f}"] + enc), str(dst), timeout=settings.VIDEO_TIMEOUT_S, cwd=cwd)


async def _stab_detect_tl(tl: Timeline, work: Path) -> list[str]:
    """Stabilizálás az idővonalon (az összefűzött, már láncolt képen): vidstab első menete → transforms.trf."""
    from app.config import settings
    from app.convert.engines import run
    from app.tools import video_fx
    if not (video_fx.has_filter("vidstabdetect") and video_fx.has_filter("vidstabtransform")):
        return ["deshake"]
    args, pre, lab = tl.inputs(0.0, tl.length)
    await run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args, "-filter_complex",
              f"{pre}{lab}vidstabdetect=shakiness=6:accuracy=15:result=transforms.trf[v]", "-map", "[v]", "-f", "null", "-",
              timeout=settings.VIDEO_TIMEOUT_S, cwd=work)
    return ["vidstabtransform=input=transforms.trf:smoothing=15:optzoom=1:zoomspeed=0.25:interpol=bicubic"]


async def _video_window(src: Path, rngs: list[tuple[float, float]], vchain: str, enc: list[str], dst: Path, cwd: Path) -> None:
    """A forrás-szakaszok (összefűzve) → a lánc → kódolva, hang nélkül."""
    from app.config import settings
    from app.convert.engines import run
    args, pre, lab = inputs(src, rngs)
    await run("ffmpeg", "-v", "error", "-y", *args, "-filter_complex", f"{pre}{lab}{vchain}[v]", "-map", "[v]", "-an", *enc,
              str(dst), timeout=settings.VIDEO_TIMEOUT_S, cwd=cwd)


async def _stab_detect(src: Path, rngs: list[tuple[float, float]], head: list[str], work: Path) -> list[str]:
    """Stabilizálás: vidstab első menete a (vágott) felvételen → transforms.trf; vidstab nélkül deshake."""
    from app.config import settings
    from app.convert.engines import run
    from app.tools import video_fx
    if not (video_fx.has_filter("vidstabdetect") and video_fx.has_filter("vidstabtransform")):
        return ["deshake"]
    args, pre, lab = inputs(src, rngs)
    graph = f"{pre}{lab}{','.join(head or ['null'])},vidstabdetect=shakiness=6:accuracy=15:result=transforms.trf[v]"
    await run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args, "-filter_complex", graph, "-map", "[v]", "-f", "null",
              "-", timeout=settings.VIDEO_TIMEOUT_S, cwd=work)
    return ["vidstabtransform=input=transforms.trf:smoothing=15:optzoom=1:zoomspeed=0.25:interpol=bicubic"]


async def full(base: dict[str, Any], src: Path, work: Path) -> Path:
    """Az egész (vágott) felvétel teljes méretben, a lánccal (a stabilizálással is) — a régi renderhez és a stabilizált
    felvételhez. Egyszer készül munkamappánként. A hossza a munkamásolaté (a kimerevítés a projektben időtérkép)."""
    from app.tools import encoding, mediaprobe, video_fx, videonorm
    dst = work / "fr-footage-full.mp4"
    if dst.exists():
        return dst
    tmp = work / "fr-footage-full.tmp.mp4"
    m = await mediaprobe.info(src)
    d = length(base.get("keep"), float(m["duration"] or 0))
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(encoding.intermediate_crf()), "-pix_fmt", "yuv420p"]
    if stabilized(base) and base.get("keep"):   # vágott / beszúrt: a lánc forrásonként, a stabilizálás az egészen
        W, H = int(base.get("width") or m["display_width"]), int(base.get("height") or m["display_height"])
        tl = await timeline(base, src, W, H, float(base.get("fps") or m.get("fps") or 30), work)
        stab = await _stab_detect_tl(tl, work)
        await _tl_window(tl, 0.0, tl.length, stab + ["format=yuv420p"], await transitions(tl, work, "f", enc), enc, tmp, work)
    elif stabilized(base):                   # vidstab két menetben a felvételen, utána a lánc
        head = videonorm.tonemap_filters(m)
        stab = await _stab_detect(src, [(0.0, d)], head, work)
        adj = {k: v for k, v in (base.get("adjust") or {}).items() if k != "stabilize"}
        vf = head + stab + (video_fx.chain(adj, work, base.get("auto")) if adj else []) + ["format=yuv420p"]
        await _video_window(src, [(0.0, d)], ",".join(vf), enc, tmp, work)
    else:
        await clip(base, src, tmp, 0.0, d, d + 1, work)
    tmp.rename(dst)
    return dst


# ---------- a munkamásolat helyére a teljes méretű felvétel (a motor projektjében) ----------

_SWAP = r"""
var asset = __ASSET__, file = __FILE__, start = __START__, item = null, comp = null;
for (var i = 1; i <= app.project.numItems; i++) {
  var it = app.project.item(i);
  if (it instanceof CompItem && it.name == __COMP__) comp = it;
  else if (it instanceof FootageItem && it.file && it.file.name == asset) item = it;
}
if (!item) throw new Error("the footage " + asset + " isn't in the project");
function mul(p, f) {        // a forrás pixeleiben mért érték (horgonypont) / a nagyítás átszámolva, a kulcsokkal együtt
  function m(v) { return [v[0] * f, v[1] * f].concat(v.length > 2 ? [v[2]] : []); }
  if (p.numKeys) { for (var j = 1; j <= p.numKeys; j++) p.setValueAtKey(j, m(p.keyValue(j))); }
  else p.setValue(m(p.value));
}
function convert(L, k) {
  var T = L.property("ADBE Transform Group");
  mul(T.property("ADBE Anchor Point"), k); mul(T.property("ADBE Scale"), 1 / k);
}
function same(L) { try { return !!L.source && L.source.id === item.id; } catch (e) { return false; } }   // (=== a burkolón hamis)
var w0 = item.width, n = 0, users = [];
for (var i = 1; i <= app.project.numItems; i++) {
  var c = app.project.item(i);
  if (!(c instanceof CompItem)) continue;
  for (var j = 1; j <= c.numLayers; j++) if (same(c.layer(j))) users.push([c, c.layer(j)]);
}
if (start !== null && (users.length !== 1 || users[0][0].id !== comp.id))   // (replaceSource-ot a motor még nem tud)
  throw new Error("the footage is used by " + users.length + " layer(s), not only the base");
item.replace(new File(file));
var k = item.width / w0;
for (var u = 0; u < users.length; u++) {
  var L = users[u][1];
  if (start !== null) {   // egy szakasz darabja a [start, …) időre: időtérképpel (a startTime a kulcsokat is eltolná)
    var d = item.duration - 1 / comp.frameRate;
    L.timeRemapEnabled = true;
    var TR = L.property("ADBE Time Remapping");
    while (TR.numKeys > 0) TR.removeKey(1);
    TR.setValueAtTime(start, 0); TR.setValueAtTime(start + d, d);
    L.inPoint = start; L.outPoint = Math.min(comp.duration, start + item.duration);
  }
  convert(L, k); n++;
}
writeLn("swapped " + n);
"""


async def swap(project: Path, comp: str, asset: str, file: str, name: str, start: Optional[float] = None) -> str:
    """A projekt egy változata (--save-as), amelyben a munkamásolat (asset) helyén a teljes méretű felvétel (file, a
    munkamappában) áll. start=None: az egész felvétel (ugyanaz az idővonal); start=a: egy [a, …) szakasz darabja, csak a
    fő kompozíció felvétel-rétegén. → a változat fájlneve."""
    from app.tools import video
    code = (_SWAP.replace("__ASSET__", json.dumps(asset)).replace("__FILE__", json.dumps(file))
            .replace("__START__", "null" if start is None else f"{start:.6f}").replace("__COMP__", json.dumps(comp)))
    out_name = f"{name}.ecproj"
    (project.parent / f"{name}.jsx").write_text(code, "utf-8")
    out = await video._run("script", f"{name}.jsx", project.name, "--save-as", out_name, cwd=project.parent)
    if "swapped" not in out or "swapped 0" in out:
        raise RuntimeError(f"the full-size footage couldn't be put in the project ({out.strip()[-200:]})")
    return out_name


# ---------- az idővonal: vágás és beszúrás (nem roncsoló — az eredetik maradnak, a recept változik) ----------
# keep = a videó szakaszai sorban, mindegyik egy forrás [a, b) darabja a forrás SAJÁT idejében; None = az egész felvétel.
#   [a, b]                          a fő felvétel darabja, sima vágással (a régi, csak-vágás alak)
#   {"a", "b", "src"?, "join"?, "gain"?, "match"?}   src: egy beszúrt klip file_id-ja (nincs = a fő felvétel); join: az
#   ELŐZŐ szakasszal való illesztés — cut (15 ms úsztatás) | smooth (hang-áttűnés) | dissolve (+ képi áttűnés); gain: dB
#   (a klip hangereje a fő felvételéhez); match: korrekció (gamma, saturation) a klip színeihez a fő felvétel felé
# A videó ideje (kimeneti idő) a szakaszok egymás után. A munkamásolat, a végső kép és a teljes méretű darabok mind ebből
# készülnek; a rétegek, feliratok, a kamera a kimeneti időben állnak, vágáskor / beszúráskor velük együtt tolódnak.

FADE_S = 0.015            # a vágáspontokon a hang ennyi alatt úszik ki / be (ne pattanjon)
XFADE_S = 0.25            # smooth / dissolve: a két szakasz hangja ennyi alatt úszik át egymásba
DISSOLVE_S = 0.4          # dissolve: képi áttűnés
JOINS = ("cut", "smooth", "dissolve")
_BIG = 1e12


def segs(keep: Any) -> list[dict[str, Any]]:
    """A tárolt alak → szakasz-szótárak ({"a", "b", "src": None | file_id, …})."""
    out = []
    for x in keep or []:
        if isinstance(x, dict):
            out.append({**x, "a": float(x["a"]), "b": float(x["b"]), "src": x.get("src") or None})
        else:
            out.append({"a": float(x[0]), "b": float(x[1]), "src": None})
    return out


def store(ss: list[dict[str, Any]]) -> list[Any]:
    """A szakaszok tárolt alakja: a fő felvétel sima darabja [a, b] (a csak-vágás projektek alakja), a többi szótár."""
    out: list[Any] = []
    for x in ss:
        extra = {k: v for k, v in x.items() if k not in ("a", "b", "src") and v not in (None, "cut")}
        if not x.get("src") and not extra:
            out.append([round(x["a"], 4), round(x["b"], 4)])
        else:
            out.append({"a": round(x["a"], 4), "b": round(x["b"], 4), **({"src": x["src"]} if x.get("src") else {}), **extra})
    return out


def length(keep: Any, dur: float) -> float:
    return round(sum(x["b"] - x["a"] for x in segs(keep)), 4) if keep else dur


def _offsets(keep: Any) -> list[tuple[float, dict[str, Any]]]:
    out, acc = [], 0.0
    for x in segs(keep) or [{"a": 0.0, "b": _BIG, "src": None}]:
        out.append((acc, x))
        acc += x["b"] - x["a"]
    return out


def at(keep: Any, t: float) -> tuple[Optional[str], float]:
    """Kimeneti idő → (forrás, a forrás ideje) — a vágás / beszúrás után álló pont."""
    if not keep:
        return None, t
    ss = segs(keep)
    acc = 0.0
    for x in ss:
        if t < acc + (x["b"] - x["a"]) - 1e-9:
            return x["src"], x["a"] + (t - acc)
        acc += x["b"] - x["a"]
    return ss[-1]["src"], ss[-1]["b"]


def to_src(keep: Any, t: float) -> float:
    return at(keep, t)[1]


def to_out(keep: Any, s: float, src: Optional[str] = None) -> Optional[float]:
    """A forrás ideje → kimeneti idő; None, ha az a pont nincs a videóban (kivágva, vagy nem ez a forrás)."""
    if not keep:
        return s if src is None else None
    for off, x in _offsets(keep):
        if x["src"] == src and x["a"] - 1e-6 <= s <= x["b"] + 1e-6:
            return round(off + min(max(s, x["a"]), x["b"]) - x["a"], 4)
    return None


def pieces(keep: Any, cs: float, ce: float) -> list[tuple[int, Optional[str], float, float]]:
    """A kimeneti [cs, ce) ablakba eső szakasz-darabok: (a szakasz sorszáma, forrás, a forrás ideje: a, b)."""
    if not keep:
        return [(0, None, cs, ce)]
    out = []
    for i, (off, x) in enumerate(_offsets(keep)):
        lo, hi = max(cs, off), min(ce, off + (x["b"] - x["a"]))
        if hi - lo > 1e-6:
            out.append((i, x["src"], round(x["a"] + lo - off, 4), round(x["a"] + hi - off, 4)))
    return out


def windows(keep: Any, cs: float, ce: float) -> list[tuple[float, float]]:
    """A kimeneti [cs, ce) ablakhoz tartozó forrás-szakaszok (csak-vágás projektnél: a felvétel idejében), sorban."""
    return [(a, b) for _, _, a, b in pieces(keep, cs, ce)]


def _tidy(ss: list[dict[str, Any]], fps: float) -> list[dict[str, Any]]:
    """Az egészen rövid darabok ki, az egymást folytató darabok (ugyanaz a forrás, ugyanott folytatódik) egybe —
    így egy beszúrt klip kivágása után a fő felvétel újra egy darab."""
    out: list[dict[str, Any]] = []
    for x in ss:
        if x["b"] - x["a"] < 0.5 / fps:
            continue
        if out and out[-1]["src"] == x["src"] and abs(out[-1]["b"] - x["a"]) < 1e-3:
            out[-1] = {**out[-1], "b": x["b"]}
            continue
        out.append(x)
    return out


def cut(keep: Any, dur: float, ranges: list[tuple[float, float]], fps: float) -> list[Any]:
    """A szakaszokból kivág (ranges: a MOSTANI videó idejében, [from, to)), kockahatárra igazítva (forrásidőben)."""
    snap = lambda x: round(round(x * fps) / fps, 4)                                     # noqa: E731
    ss = segs(keep) or [{"a": 0.0, "b": dur, "src": None}]
    for f, t in sorted(((min(a, b), max(a, b)) for a, b in ranges), reverse=True):     # hátulról: az idők érvényesek maradnak
        total = length(ss, dur)
        f, t = max(0.0, f), min(total, t)
        if t - f < 0.5 / fps:
            continue
        nxt, acc = [], 0.0
        for x in ss:
            o0, o1 = acc, acc + (x["b"] - x["a"])
            acc = o1
            if o1 <= f + 1e-9 or o0 >= t - 1e-9:
                nxt.append(x)
                continue
            if o0 < f:
                nxt.append({**x, "b": snap(x["a"] + (f - o0))})
            if o1 > t:                                          # a vágás utáni rész: új kezdet, sima vágással illesztve
                nxt.append({**x, "a": snap(x["a"] + (t - o0)), "join": "cut"})
        ss = _tidy(nxt, fps)
    if not ss:
        raise ValueError("That cut would remove the whole video — keep at least a part of it.")
    return store(ss)


def insert(keep: Any, dur: float, t: float, seg: dict[str, Any], fps: float) -> list[Any]:
    """A seg szakasz (egy klip [a, b) darabja, join-nal) beszúrása a MOSTANI videó t pontjára; a fő felvétel a beszúrás
    után ugyanazzal az illesztéssel folytatódik."""
    snap = lambda x: round(round(x * fps) / fps, 4)                                     # noqa: E731
    ss = segs(keep) or [{"a": 0.0, "b": dur, "src": None}]
    seg = {**seg, "src": seg.get("src") or None, "join": seg.get("join") or "smooth"}
    out, acc, done = [], 0.0, False
    for x in ss:
        o0, o1 = acc, acc + (x["b"] - x["a"])
        acc = o1
        if not done and o0 - 1e-9 <= t < o1 - 1e-9:
            if t - o0 > 0.5 / fps:
                cut_at = snap(x["a"] + (t - o0))
                out.append({**x, "b": cut_at})
                out += [seg, {**x, "a": cut_at, "join": seg["join"]}]
            else:
                out += [seg, {**x, "join": seg["join"]}]
            done = True
        else:
            out.append(x)
    if not done:
        out.append(seg)
    return store(_tidy(out, fps))


def remap(t: float, old: Any, new: Any) -> Optional[float]:
    """A régi idővonal kimeneti ideje → az újé (None: kiesett)."""
    src, s = at(old, t)
    return to_out(new, s, src)


def remap_near(t: float, old: Any, new: Any) -> float:
    """Mint a remap, de a kivágott pont helyett a következő megmaradt pont (a fő felvételen)."""
    r = remap(t, old, new)
    if r is not None:
        return r
    src, s = at(old, t)
    return out_near(new, s) if src is None else min(t, length(new, t))


def remap_span(a: float, b: float, old: Any, new: Any) -> Optional[tuple[float, float]]:
    """Egy [a, b) időszak (réteg, felirat) az új idővonalon: a megmaradt része. Vágás után a darabjai egymás mellé
    kerülnek (egy időszak); ha egy beszúrás kettévágja, a hosszabbik fele (a réteg nem nyúlik át a beszúrt klipen)."""
    parts = []
    for _, src, sa, sb in pieces(old, a, b):                          # a régi időszak a forrásokban…
        for off, x in _offsets(new):                                    # …és ami belőle az új idővonalon megmaradt
            if x["src"] != src:
                continue
            lo, hi = max(sa, x["a"]), min(sb, x["b"])
            if hi - lo > 1e-6:
                parts.append((round(off + lo - x["a"], 4), round(off + hi - x["a"], 4)))
    if not parts:
        return None
    runs: list[list[float]] = []
    for p0, p1 in sorted(parts):
        if runs and p0 - runs[-1][1] <= 0.02:
            runs[-1][1] = max(runs[-1][1], p1)
        else:
            runs.append([p0, p1])
    best = max(runs, key=lambda r: r[1] - r[0] - 1e-9 * runs.index(r))
    return best[0], best[1]


def inputs(src: Path, ranges: list[tuple[float, float]], first: int = 0) -> tuple[list[str], str, str]:
    """Egy forrás szakaszai ffmpeg-bemenetként: (bemeneti argumentumok, gráf-előtag, a kép címkéje). Egy szakasznál egy
    bemenet (-ss/-t), többnél concat — a gráf-előtag ';'-vel zárul, ha van."""
    args: list[str] = []
    for a, b in ranges:
        args += ["-ss", f"{a:.4f}", "-t", f"{b - a:.4f}", "-i", str(src)]
    if len(ranges) == 1:
        return args, "", f"[{first}:v]"
    pads = "".join(f"[{first + k}:v]" for k in range(len(ranges)))
    return args, f"{pads}concat=n={len(ranges)}:v=1:a=0[srcv];", "[srcv]"


class Timeline:
    """Az idővonal a rendereléshez, egy célméretre: forrásonként a fájl és a lánca (a fő felvételé: HDR → SDR, a
    korrekció; egy klipé: HDR → SDR, a színillesztés, a méretre illesztés, a kockasebesség), a szakaszok."""

    def __init__(self, keep: Any, paths: dict[Optional[str], Path], infos: dict[Optional[str], dict[str, Any]],
                 chains: dict[Optional[str], Any], size: tuple[int, int], fps: float):
        self.ss, self.paths, self.infos, self.chains, self.size, self.fps = segs(keep), paths, infos, chains, size, fps

    @property
    def length(self) -> float:
        return round(sum(x["b"] - x["a"] for x in self.ss), 4)

    def chain(self, src: Optional[str], tag: str) -> str:
        return self.chains[src](tag)

    def inputs(self, cs: float, ce: float, first: int = 0) -> tuple[list[str], str, str]:
        """A kimeneti [cs, ce) ablak: bemenetek, gráf-előtag (';'-vel zárva), a kész kép címkéje."""
        ps = pieces(self.ss, cs, ce)
        args, parts = [], []
        for k, (_, src, a, b) in enumerate(ps):
            args += ["-ss", f"{a:.4f}", "-t", f"{b - a:.4f}", "-i", str(self.paths[src])]
            parts.append(f"[{first + k}:v]{self.chain(src, f'tl{k}')}[tv{k}]")
        if len(ps) == 1:
            return args, parts[0] + ";", "[tv0]"
        return args, ";".join(parts) + ";" + "".join(f"[tv{k}]" for k in range(len(ps))) + \
            f"concat=n={len(ps)}:v=1:a=0[tlv];", "[tlv]"

    def joins(self) -> list[tuple[float, dict[str, Any], dict[str, Any]]]:
        """Az illesztések: (kimeneti idő, az előző szakasz, a következő)."""
        out, acc = [], 0.0
        for i, x in enumerate(self.ss):
            if i:
                out.append((round(acc, 4), self.ss[i - 1], x))
            acc += x["b"] - x["a"]
        return out


async def timeline(base: dict[str, Any], src: Path, W: int, H: int, fps: float, work: Path, *,
                   size: Optional[tuple[int, int]] = None, bt709: bool = False) -> Timeline:
    """A base idővonala egy célméretre (size: a munkamásolaté; nincs: a teljes W×H). bt709: a forrás színmátrixa
    BT.709-re (a gyors render végső menete, fast_render.base_filters)."""
    from app.tools import crafts, fast_render, mediaprobe, video_fx, videonorm
    tw, th = size or (W, H)
    ss = segs(base.get("keep"))
    paths: dict[Optional[str], Path] = {None: src}
    for x in ss:
        if x["src"] and x["src"] not in paths:
            paths[x["src"]] = crafts.existing(x["src"])
    infos = {k: await mediaprobe.info(p) for k, p in paths.items()}
    # MINDEN forrás lánca ugyanarra a kockasebességre (a fő felvételé is): a concat a különböző kockasebességű
    # bemenetekből „VFR” kimenetet csinál (frame_rate 1/0), és az ffmpeg a kódolásnál a time base-zel (1 µs) állandó
    # kockasebességre töltené ki — 10-25, valós anyagon (iPhone: 30 tbr, 29,993 átlag; a klip fps=29,993): minden kocka
    # ezerszer ismételve, 0,001× sebesség, a munkamásolat egy szelete 30 perc után időtúllépéssel állt meg
    tail = f"fps={fps:g},format=yuv420p,setsar=1,settb=AVTB"
    chains: dict[Optional[str], Any] = {}
    m = infos[None]
    mw, mh = int(m["display_width"] or W), int(m["display_height"] or H)
    main = ([f"scale={tw}:{th}"] if (tw, th) != (mw, mh) else []) + chain(base, m, work) + \
        (await fast_render.base_filters(src) if bt709 else [])
    chains[None] = lambda tag, f=",".join(main + [tail]): f
    for k, info in infos.items():
        if k is None:
            continue
        x = next(s for s in ss if s["src"] == k)
        # a klip színmátrixa mindig BT.709-re: a videó képébe kerül, és azt mindenki (a motor is) BT.709-ként olvassa
        pre = videonorm.tonemap_filters(info) + (video_fx.chain(x["match"], work, None) if x.get("match") else []) + \
            await fast_render.base_filters(paths[k])
        fit = "contain" if videonorm.same_shape(info, tw, th) else "blur"
        chains[k] = (lambda tag, pre=pre, fit=fit:
                     ",".join(pre + [videonorm.fit_chain(tw, th, fit, tag), tail]))
    return Timeline(base.get("keep"), paths, infos, chains, (tw, th), fps)


async def transitions(tl: Timeline, work: Path, tag: str, enc: list[str]) -> list[dict[str, Any]]:
    """A „dissolve” illesztések képi áttűnése rövid darabként (a célméreten), a kimeneti [t, t + d) időre — a
    renderelők rétegként teszik a felvétel fölé (fast_render.window_graph items). Az előző szakasz a vágás utáni
    képeivel úszik át (ha a forrásnak nincs folytatása, az utolsó kockája áll)."""
    from app.config import settings
    from app.convert.engines import run
    items = []
    for i, (t, prev, nxt) in enumerate(tl.joins()):
        if nxt.get("join") != "dissolve":
            continue
        d = round(min(DISSOLVE_S, (nxt["b"] - nxt["a"]) / 2), 3)
        pa = tl.infos[prev["src"]]
        more = float(pa.get("duration") or 0) - prev["b"] > d + 0.05
        a0 = prev["b"] if more else max(0.0, prev["b"] - 1.5 / max(float(pa.get("fps") or 30), 1))
        name = f"fr-tr-{tag}-{i:03d}.mp4"
        fr = f"fps={tl.fps:g}"
        graph = (f"[0:v]{tl.chain(prev['src'], 'xa')},{fr},tpad=stop_mode=clone:stop_duration={d + 1:.3f},"
                 f"trim=duration={d:.4f}[x0];[1:v]{tl.chain(nxt['src'], 'xb')},{fr},trim=duration={d:.4f}[x1];"
                 f"[x0][x1]xfade=transition=fade:duration={d:.4f}:offset=0,format=yuv420p[v]")
        await run("ffmpeg", "-v", "error", "-y", "-ss", f"{a0:.4f}", "-i", str(tl.paths[prev["src"]]), "-ss", f"{nxt['a']:.4f}",
                  "-t", f"{d + 0.2:.4f}", "-i", str(tl.paths[nxt["src"]]), "-filter_complex", graph, "-map", "[v]", "-an",
                  "-t", f"{d:.4f}", *enc, name, timeout=settings.VIDEO_TIMEOUT_S, cwd=work)
        items.append({"name": name, "a": t, "b": round(t + d, 4), "enable": f"between(t,{t:.4f},{t + d - 1e-4:.4f})",
                      "loop": False})
    return items


async def tl_audio(tl: Timeline, dst: Path, cwd: Path) -> None:
    """A szakaszok hangja egymás után: a klipé a hangerő-illesztéssel; sima vágásnál FADE_S úsztatás, smooth / dissolve
    illesztésnél XFADE_S hang-áttűnés (az előző szakasz a vágás utáni hangjával — a hossz nem változik); hang nélküli
    forrásnál csend → AAC."""
    from app.config import settings
    from app.convert.engines import run
    from app.tools import encoding
    n = len(tl.ss)
    ext = []
    for i, x in enumerate(tl.ss):                       # a következő illesztés áttűnése: ennyivel tovább szól ez a darab
        nxt = tl.ss[i + 1] if i + 1 < n else None
        room = float(tl.infos[x["src"]].get("duration") or 0) - x["b"]
        ok = nxt is not None and nxt.get("join") in ("smooth", "dissolve") and room > XFADE_S + 0.02 and \
            nxt["b"] - nxt["a"] > 2 * XFADE_S and x["b"] - x["a"] > 2 * XFADE_S
        ext.append(XFADE_S if ok else 0.0)
    args, chains = [], []
    for k, x in enumerate(tl.ss):
        L = x["b"] - x["a"] + ext[k]
        st = tl.infos[x["src"]].get("audio_stream")
        if st is None:
            args += ["-f", "lavfi", "-t", f"{L:.4f}", "-i", "anullsrc=r=48000:cl=stereo"]
            lab = f"[{k}:a]"
        else:
            args += ["-ss", f"{x['a']:.4f}", "-t", f"{L:.4f}", "-i", str(tl.paths[x["src"]])]
            lab = f"[{k}:a:{st}]"
        f = min(FADE_S, (x["b"] - x["a"]) / 4)
        fx = ["aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
        if x.get("gain"):
            fx.append(f"volume={float(x['gain']):.2f}dB")
        if not (k and ext[k - 1]):
            fx.append(f"afade=t=in:d={f:.3f}")
        if not ext[k]:
            fx.append(f"afade=t=out:st={max(0.0, L - f):.4f}:d={f:.3f}")
        chains.append(f"{lab}{','.join(fx)}[a{k}]")
    cur = "[a0]"
    for k in range(1, n):
        mix = f"acrossfade=d={ext[k - 1]:.3f}:c1=tri:c2=tri" if ext[k - 1] else "concat=n=2:v=0:a=1"
        chains.append(f"{cur}[a{k}]{mix}[m{k}]")
        cur = f"[m{k}]"
    await run("ffmpeg", "-v", "error", "-y", *args, "-filter_complex", ";".join(chains), "-map", cur, *encoding.aac(), str(dst),
              timeout=settings.VIDEO_TIMEOUT_S, cwd=cwd)


# ---------- hova kerüljön a beszúrás, és hogy ne tűnjön fel (az anyagból, szabállyal — a szerver nem gondolkodik LLM-mel) ----------
# 10-23, Kommandant: „a második kis videót vágjuk be a 16–19. perc közé értelemszerűen”, „a harmadikat tedd a végére, csak ne
# tűnjön fel nagyon, hogy vágás”. A hely: a fő felvétel szünetei az ablakban (silencedetect), a mondatvég a feliratokból; a
# leghosszabb mondatvégi szünet, ha nincs, a leghosszabb szünet. Az illesztés: a klip eleje / vége csendig vágva, a hangereje
# (EBU R128) és a színei (átlagos fényesség, telítettség) a fő felvételéhez igazítva, a hang 0,25 s alatt úszik át.

PAUSE_DB = -35            # ennél halkabb = szünet
PAUSE_MIN_S = 0.3
_SENTENCE_END = re.compile(r"[.!?…]['\"”’)]*\s*$")


def out_near(keep: Any, s: float) -> float:
    """A fő felvétel ideje → kimeneti idő; ha az a pont ki van vágva, a következő megmaradt pont."""
    t = to_out(keep, s, None)
    if t is not None:
        return t
    for off, x in _offsets(keep):
        if x["src"] is None and x["a"] >= s:
            return off
    return length(keep, s)


def pieces_out(keep: Any, cs: float, ce: float) -> list[tuple[Optional[str], float, float, float]]:
    """Mint a pieces, a darab kimeneti kezdetével: (forrás, a, b, kimeneti kezdet)."""
    if not keep:
        return [(None, cs, ce, cs)]
    out = []
    for off, x in _offsets(keep):
        lo, hi = max(cs, off), min(ce, off + (x["b"] - x["a"]))
        if hi - lo > 1e-6:
            out.append((x["src"], round(x["a"] + lo - off, 4), round(x["a"] + hi - off, 4), round(lo, 4)))
    return out


async def pauses(path: Path, a: float, b: float, stream: Optional[int]) -> list[tuple[float, float]]:
    """A forrás [a, b) szakaszának szünetei (silencedetect) a forrás idejében."""
    from app.convert.engines import run
    if stream is None or b - a < PAUSE_MIN_S:
        return []
    _, err = await run("ffmpeg", "-hide_banner", "-nostats", "-ss", f"{a:.4f}", "-t", f"{b - a:.4f}", "-i", str(path),
                       "-map", f"0:a:{stream}", "-af", f"silencedetect=noise={PAUSE_DB}dB:d={PAUSE_MIN_S}", "-f", "null", "-",
                       timeout=600)
    out, start = [], None
    for kind, v in re.findall(r"silence_(start|end): (-?[\d.]+)", err.decode("utf-8", "replace")):
        if kind == "start":
            start = max(0.0, float(v))
        elif start is not None:
            out.append((round(a + start, 3), round(a + min(float(v), b - a), 3)))
            start = None
    if start is not None:
        out.append((round(a + start, 3), round(b, 3)))
    return out


async def candidates(keep: Any, src: Path, info: dict[str, Any], w0: float, w1: float, cues: list[dict[str, Any]],
                     fps: float) -> list[dict[str, Any]]:
    """A beszúrás jelöltjei a kimeneti [w0, w1] ablakban: a fő felvétel szünetei ({at, pause, sentence_end})."""
    out = []
    for s, a, b, o in pieces_out(keep, w0, w1):
        if s is not None:
            continue
        for p0, p1 in await pauses(src, a, b, info.get("audio_stream")):
            f, t = o + p0 - a, o + p1 - a
            ends = any(f - 0.6 <= float(c["end"]) <= t + 0.2 and _SENTENCE_END.search(str(c.get("text") or ""))
                       for c in cues or [])
            out.append({"at": round(round((f + t) / 2 * fps) / fps, 4), "pause": round(t - f, 2), "sentence_end": bool(ends)})
    return out


def pick(cands: list[dict[str, Any]], w0: float, w1: float, fps: float) -> tuple[float, str]:
    """A legjobb hely: a leghosszabb mondatvégi szünet; ha nincs, a leghosszabb szünet; ha az sincs, az ablak közepe."""
    ends = [c for c in cands if c["sentence_end"]]
    if cands:
        c = max(ends or cands, key=lambda x: x["pause"])
        return c["at"], (f"end of a sentence, {c['pause']:.1f} s pause" if c["sentence_end"] else f"a {c['pause']:.1f} s pause")
    return round(round((w0 + w1) / 2 * fps) / fps, 4), "no pause in the window — its middle"


async def loudness(path: Path, a: float, b: float, stream: Optional[int]) -> Optional[float]:
    """Integrált hangosság (EBU R128, LUFS) a forrás [a, b) szakaszán; csendnél / hang nélkül None."""
    from app.convert.engines import run
    if stream is None or b - a < 0.5:
        return None
    _, err = await run("ffmpeg", "-hide_banner", "-nostats", "-ss", f"{a:.4f}", "-t", f"{b - a:.4f}", "-i", str(path),
                       "-map", f"0:a:{stream}", "-af", "ebur128", "-f", "null", "-", timeout=600)
    m = re.findall(r"\bI:\s+(-?[\d.]+) LUFS", err.decode("utf-8", "replace"))
    v = float(m[-1]) if m else None
    return v if v is not None and v > -60 else None


async def look(path: Path, info: dict[str, Any], a: float, b: float) -> Optional[dict[str, float]]:
    """A kép átlagos fényessége és telítettsége a forrás [a, b) szakaszán (kulcskockák, SDR-re képezve, 8 bit)."""
    from app.convert.engines import run
    from app.tools import videonorm
    vf = ",".join(videonorm.head_filters(info) + ["signalstats", "metadata=print:file=-"])
    out, _ = await run("ffmpeg", "-hide_banner", "-ss", f"{a:.4f}", "-t", f"{max(0.5, b - a):.4f}", "-skip_frame", "nokey",
                       "-i", str(path), "-map", "0:v:0", "-vf", vf, "-vsync", "0", "-frames:v", "120", "-f", "null", "-",
                       timeout=600)
    vals: dict[str, list[float]] = {}
    for k, v in re.findall(r"lavfi\.signalstats\.(YAVG|SATAVG)=([\d.]+)", out.decode("utf-8", "replace")):
        vals.setdefault(k, []).append(float(v))
    return {k: sum(v) / len(v) for k, v in vals.items()} if vals.get("YAVG") else None


def match_look(main: Optional[dict[str, float]], clip: Optional[dict[str, float]]) -> dict[str, float]:
    """A klip színei a fő felvétel felé: gamma (az átlagos fényesség), telítettség — korlátosan, apró eltérésre semmi."""
    if not main or not clip:
        return {}
    n = lambda v: min(0.95, max(0.05, (v - 16) / 219))                                 # noqa: E731
    out: dict[str, float] = {}
    g = math.log(n(clip["YAVG"])) / math.log(n(main["YAVG"]))
    if abs(g - 1) >= 0.04:
        out["gamma"] = round(min(1.35, max(0.75, g)), 3)
    if clip.get("SATAVG", 0) >= 3 and main.get("SATAVG"):
        sat = main["SATAVG"] / clip["SATAVG"]
        if abs(sat - 1) >= 0.05:
            out["saturation"] = round(min(1.35, max(0.75, sat)), 3)
    return out


async def trim_silence(path: Path, info: dict[str, Any], a: float, b: float) -> tuple[float, float]:
    """A klip elején / végén álló csend levágva (legfeljebb 0,2 s marad): a vágás így beszédszünetre esik."""
    st, a0, b0 = info.get("audio_stream"), a, b
    head = await pauses(path, a, min(b, a + 10), st)
    if head and head[0][0] <= a + 0.05:
        a = max(a, head[0][1] - 0.2)
    tail = await pauses(path, max(a, b - 10), b, st)
    if tail and tail[-1][1] >= b - 0.05:
        b = min(b, tail[-1][0] + 0.2)
    return (round(a, 3), round(b, 3)) if b - a >= 0.5 else (a0, b0)
