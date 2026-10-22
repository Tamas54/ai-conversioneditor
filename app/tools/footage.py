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
import os
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


def needed(m: dict[str, Any], adjust: Optional[dict[str, Any]] = None, nbytes: int = 0) -> bool:
    """Kell-e munkamásolat: a motor a tárolt (elforgatás nélküli) kockát olvasná, a HDR-t SDR-nek venné, korrekciót nem tud;
    és a böngésző-szerkesztő az egész fájlt a memóriába tölti — a nagy felvételt (méretben vagy bájtban) se kapja meg."""
    W, H = m.get("display_width") or m.get("width") or 0, m.get("display_height") or m.get("height") or 0
    return bool((m.get("rotation") or 0) % 360 or m.get("hdr") or adjust or max(W, H) > PROXY_SIDE or nbytes > PROXY_BYTES)


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
    key = json.dumps({"v": 1, "side": PROXY_SIDE, "budget": PROXY_BUDGET_MB, "adjust": base.get("adjust") or {}},
                     sort_keys=True)
    async with _BUSY.setdefault(fid0 + key, asyncio.Lock()):
        hit = ((read_meta(fid0) or {}).get("proxies") or {}).get(key) if fid0 else None
        try:
            if hit:
                crafts.existing(hit["media_id"])
                b = {k: v for k, v in base.items() if k not in _KEEP + ("clean_media_id",)}
                return {**b, **{k: hit[k] for k in _KEEP if k in hit}}, list(hit.get("applied") or [])
        except FileNotFoundError:
            pass
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
        stab = await video_fx._stabilize_pass(src, head or ["null"], work) if adj.get("stabilize") else []
        vf = head + stab + (video_fx.chain({k: v for k, v in adj.items() if k != "stabilize"}, work, b.get("auto")) if adj else [])
        vf += ["format=yuv420p"]
        k = m.get("audio_stream")
        acodec = ["-c:a", "copy"] if m.get("acodec") == "aac" else encoding.aac()
        tmp = work / "proxy.mp4"
        dur = float(m.get("duration") or b.get("duration") or 0)
        gop = str(max(1, round(float(m.get("fps") or 30))))
        enc = ["-vf", ",".join(vf), "-c:v", "libx264", "-preset", "veryfast", *_rate(dur), "-g", gop, "-pix_fmt", "yuv420p"]
        cuts = [(0.0, dur)] if stab else fast_render.chunks(dur, float(m.get("fps") or 30))
        if len(cuts) <= 1:
            aud = ["-map", f"0:a:{k}", *acodec] if k is not None else ["-an"]
            await run("ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", *aud, *enc, "-movflags", "+faststart",
                      str(tmp), timeout=settings.VIDEO_TIMEOUT_S, cwd=work)
        else:                       # hosszú felvétel: időszeletek egyszerre (a szűrőlánc egy szálon fut), összefűzve, a hang egyben
            threads = str(max(2, (os.cpu_count() or 4) // len(cuts)))

            async def piece(i: int, a: float, e: float) -> str:
                name = f"proxy-{i:03d}.mp4"
                await run("ffmpeg", "-v", "error", "-y", "-ss", f"{a:.4f}", "-t", f"{e - a:.4f}", "-i", str(src), "-map", "0:v:0",
                          "-an", *enc, "-threads", threads, name, timeout=settings.VIDEO_TIMEOUT_S, cwd=work)
                return name
            names = await asyncio.gather(*(piece(i, a, e) for i, (a, e) in enumerate(cuts)))
            (work / "proxy.txt").write_text("".join(f"file '{n}'\n" for n in names), "utf-8")
            aud = ["-i", str(src), "-map", "0:v:0", "-map", f"1:a:{k}", *acodec] if k is not None else ["-map", "0:v:0"]
            await run("ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "proxy.txt", *aud, "-c:v", "copy",
                      "-t", f"{dur:.4f}", "-movflags", "+faststart", str(tmp), timeout=settings.VIDEO_TIMEOUT_S, cwd=work)
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
    """A felvétel [a, b) szakasza teljes méretben, a lánccal (és b > plain esetén a kimerevített véggel) — a motor ezt
    rajzolja ott, ahol a felvétel maga mozog. Köztes fájl: a végső kódolás előtti minőségen (VIDEO_CRF_INTERMEDIATE)."""
    from app.config import settings
    from app.convert.engines import run
    from app.tools import encoding, mediaprobe
    m = await mediaprobe.info(src)
    vf = chain(base, m, work) + ["format=yuv420p"]
    if b > plain:
        vf.append(f"tpad=stop_mode=clone:stop_duration={b - plain + 1:.3f}")
    await run("ffmpeg", "-v", "error", "-y", "-ss", f"{a:.4f}", "-i", str(src), "-map", "0:v:0", "-an", "-vf", ",".join(vf),
              "-t", f"{b - a:.4f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(encoding.intermediate_crf()),
              "-pix_fmt", "yuv420p", str(dst), timeout=settings.VIDEO_TIMEOUT_S, cwd=work)


async def full(base: dict[str, Any], src: Path, work: Path) -> Path:
    """Az egész felvétel teljes méretben, a lánccal (a stabilizálással is) — a régi renderhez és a stabilizált
    felvételhez. Egyszer készül munkamappánként. A hossza a munkamásolaté (a kimerevítés a projektben időtérkép)."""
    from app.tools import mediaprobe
    dst = work / "fr-footage-full.mp4"
    if dst.exists():
        return dst
    tmp = work / "fr-footage-full.tmp.mp4"
    if stabilized(base):
        await _full_stabilized(base, src, tmp, work)
    else:
        d = float((await mediaprobe.info(src))["duration"] or 0)
        await clip(base, src, tmp, 0.0, d, d + 1, work)
    tmp.rename(dst)
    return dst


async def _full_stabilized(base: dict[str, Any], src: Path, dst: Path, work: Path) -> None:
    """Stabilizált teljes változat: vidstab két menetben az eredetin, utána a lánc (a tárolt auto-méréssel)."""
    from app.config import settings
    from app.convert.engines import run
    from app.tools import encoding, mediaprobe, video_fx, videonorm
    m = await mediaprobe.info(src)
    head = videonorm.tonemap_filters(m)
    stab = await video_fx._stabilize_pass(src, head or ["null"], work)
    adj = {k: v for k, v in (base.get("adjust") or {}).items() if k != "stabilize"}
    vf = head + stab + (video_fx.chain(adj, work, base.get("auto")) if adj else []) + ["format=yuv420p"]
    await run("ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-an", "-vf", ",".join(vf), "-c:v", "libx264",
              "-preset", "veryfast", "-crf", str(encoding.intermediate_crf()), "-pix_fmt", "yuv420p", str(dst),
              timeout=settings.VIDEO_TIMEOUT_S, cwd=work)


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
