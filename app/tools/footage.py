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
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("aice.footage")

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


def _key(base: dict[str, Any], keep: Any = "same") -> str:
    """A munkamásolat kulcsa az eredeti metájában (proxies): a recept (korrekció, idővonal) és a méret."""
    return json.dumps({"v": 2, "side": PROXY_SIDE, "budget": PROXY_BUDGET_MB, "adjust": base.get("adjust") or {},
                       "keep": (base.get("keep") if keep == "same" else keep) or None}, sort_keys=True)


def cached(base: dict[str, Any]) -> Optional[dict[str, Any]]:
    """A recepthez már elkészült munkamásolat (proxies-bejegyzés), ha a fájlja még megvan; különben None."""
    from app.storage import read_meta
    from app.tools import crafts
    fid0 = str(base.get("file_id") or "")
    hit = ((read_meta(fid0) or {}).get("proxies") or {}).get(_key(base)) if fid0 else None
    try:
        return hit if hit and crafts.existing(hit["media_id"]) else None
    except FileNotFoundError:
        return None


def timeline_id(keep: Any) -> Optional[str]:
    """Az idővonal azonossága a HANG szempontjából (a darabok sorban: forrás, a, b, illesztés, hangerő, színillesztés);
    None = az egész felvétel. Két munkamásolat hangja ugyanaz, ha ez egyezik — a színkorrekciójuk lehet más."""
    if not keep:
        return None
    return json.dumps([[x["src"], round(float(x["a"]), 3), round(float(x["b"]), 3), x.get("join") or "cut", x.get("gain"),
                        x.get("match") or {}] for x in segs(keep)], sort_keys=True)


def registered(base: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """A felvétel kész munkamásolatai (az eredeti metájában: proxies), amelyek fájlja még megvan → [(a kulcs: adjust,
    keep…, a bejegyzés: media_id, asset, proxy, auto…)]."""
    from app.storage import read_meta
    from app.tools import crafts
    fid0 = str(base.get("file_id") or "")
    out = []
    for k, hit in (((read_meta(fid0) or {}).get("proxies") or {}).items() if fid0 else []):
        try:
            kd = json.loads(k)
            crafts.existing(hit["media_id"])
        except (ValueError, KeyError, FileNotFoundError, TypeError):
            continue
        if kd.get("v") != 2 or kd.get("side") != PROXY_SIDE or kd.get("budget") != PROXY_BUDGET_MB:
            continue
        out.append((kd, hit))
    return out


def same_timeline(base: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """A kész munkamásolatok, amelyek idővonala (hangja) a base-é — a színkorrekciójuk bármi lehet."""
    want = timeline_id(base.get("keep"))
    return [(kd, hit) for kd, hit in registered(base) if timeline_id(kd.get("keep")) == want]


def ready_soon(base: dict[str, Any]) -> bool:
    """Másodpercek alatt meglesz-e a munkamásolat: már elkészült, vagy egy meglévőből vágható (smart cut — a vágás,
    a beszúrás csak a vágáspontok körül kódol)."""
    return cached(base) is not None or bool(donors(base))


async def make_proxy(base: dict[str, Any], src: Path) -> tuple[dict[str, Any], list[str]]:
    """A munkamásolat → (az új base: media_id, asset, proxy-méret, rotated, hdr, auto; a korrekció leírása). Ugyanabból a
    feltöltésből ugyanazzal a korrekcióval egyszer készül: a következő videó (újabb változat ugyanarról a felvételről) a
    meglévőt kapja (az eredeti metájában: proxies), amíg a fájlja megvan. Új vágásnál / beszúrásnál ugyanazzal a
    korrekcióval egy meglévő munkamásolatból (smart_proxy: másolva, csak a vágáspontok körül kódolva)."""
    from app.storage import read_meta, write_meta
    from app.tools import crafts
    fid0 = str(base.get("file_id") or "")
    key = _key(base)
    async with _BUSY.setdefault(fid0 + key, asyncio.Lock()):
        hit = ((read_meta(fid0) or {}).get("proxies") or {}).get(key) if fid0 else None
        try:
            if hit:
                crafts.existing(hit["media_id"])
                b = {k: v for k, v in base.items() if k not in _KEEP + ("clean_media_id",)}
                return {**b, **{k: hit[k] for k in _KEEP if k in hit}}, list(hit.get("applied") or [])
        except FileNotFoundError:
            pass
        made = None
        for old_keep, donor in donors(base):
            try:
                made = await smart_proxy(base, src, old_keep, donor)
            except Exception as e:                           # bármi eltér: a teljes újrakódolás fut (naplózva)
                log.warning("smart cut munkamásolat elakadt (%s): %s — a teljes úton", donor.get("media_id"), e)
                made = None
            if made:
                break
        if made:
            b, applied = made
        else:
            from app import jobs
            # a munkamásolat egy nehéz munka (app/jobs.py): sorban vár, ha minden hely foglalt; a szeletei egy helyen, a
            # saját -threads-ükkel. A gyorsítótárból jött másolat (fent) nem kér helyet: a szerkesztés előnézete nem vár.
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


# ---------- a munkamásolat vágása újrakódolás nélkül (smart cut, 10-25) ----------
# PROFILE.md 3.d: a vágás (05:00–05:20) +430 s volt — a munkamásolat kulcsában a keep is benne van, az új másolat az
# eredetiből készült újra (4 szelet × 404 s, + a hang 23,7 s). A munkamásolat a SAJÁT x264-kimenetünk (zárt GOP, kulcskocka
# másodpercenként: -g fps, mindig ugyanazok a kapcsolók), így egy MEGLÉVŐ munkamásolatból (ugyanazzal a korrekcióval, más
# idővonallal) a megmaradó szakaszok kulcskockától kulcskockáig bitre pontosan átmásolhatók; csak ami a régiben nincs meg
# (a vágáspontok körüli részleges GOP-ok, egy beszúrt klip, egy új áttűnés) készül az eredetiből — ugyanazzal a lánccal
# és kódolóval, ahogy a teljes újrakódolás egy szelete (_tl_window). Az idővonal ugyanaz (kockaszám, sorrend): a
# munkamásolat ideje = a kimenet ideje, minden fogyasztó változatlan. A hang az új idővonalból (tl_audio, az eredetiből).
# Ellenőrizve, mielőtt kiadjuk: a kódolt darabok paraméterkészlete (avcC) bájtra a régié, a kockák száma a vártszám, és
# minden illesztés után az első kockák dekódolva a darabéval azonosak. Bármi eltér: a teljes újrakódolás fut.
# VIDEO_SMART_PROXY=0 kikapcsolja.

SMART_MIN_REUSE = 0.5          # a régi munkamásolatból legalább ennyi (az új kockáinak aránya) jöjjön, különben teljes út
SMART_MAX_ENCODE_S = 180.0     # ennél több újrakódolandó másodperc: a teljes út (párhuzamos szeleteivel) se sokkal lassabb
SMART_PIECE_S = 30.0           # az újrakódolt szakaszok darabja (egyszerre több fut)


def _smart_on() -> bool:
    return os.getenv("VIDEO_SMART_PROXY", "1").strip().lower() not in ("0", "false", "no", "off")


def _frame(t: float, fps: float) -> int:
    return round(t * fps + 1e-6)


def _dissolve_key(ss: list[dict[str, Any]], i: int, fps: float) -> Optional[tuple[Any, ...]]:
    """Az i. szakasz elején álló képi áttűnés azonosítója (amitől a képe függ: transitions) és a kockaszáma; None: nincs."""
    if i <= 0 or i >= len(ss) or ss[i].get("join") != "dissolve":
        return None
    p, x = ss[i - 1], ss[i]
    n = max(2, round(min(DISSOLVE_S, (x["b"] - x["a"]) / 2) * fps))
    return (p["src"], round(p["a"], 4), round(p["b"], 4), x["src"], round(x["a"], 4), json.dumps(x.get("match") or {},
            sort_keys=True), n)


def reuse(old_keep: Any, new_keep: Any, fps: float) -> list[tuple[int, int, int]]:
    """Az új idővonal mely kockái vannak meg a régi munkamásolatban → futamok [(új első kocka, régi első kocka, db)].
    Egy szakasz akkor jön a régiből, ha ugyanaz a forrás, ugyanaz a képi illesztés (match), és a régi idővonal egy
    szakasza tartalmazza; az áttűnések kockái csak akkor, ha a régiben ugyanaz az áttűnés áll ugyanott."""
    olds = segs(old_keep) or [{"a": 0.0, "b": _BIG, "src": None}]
    news = segs(new_keep) or [{"a": 0.0, "b": _BIG, "src": None}]
    o_off, acc = [], 0.0
    for y in olds:
        o_off.append(acc)
        acc += y["b"] - y["a"]
    runs: list[tuple[int, int, int]] = []
    on = 0.0
    for i, x in enumerate(news):
        nf0, nf1 = _frame(on, fps), _frame(on + x["b"] - x["a"], fps)
        on += x["b"] - x["a"]
        for j, y in enumerate(olds):
            if y["src"] != x["src"] or (y.get("match") or {}) != (x.get("match") or {}):
                continue
            if not (y["a"] - 1e-4 <= x["a"] and x["b"] <= y["b"] + 1e-4):
                continue
            of0 = _frame(o_off[j] + x["a"] - y["a"], fps)
            n = nf1 - nf0
            if y["b"] < _BIG / 2:
                n = min(n, _frame(o_off[j] + y["b"] - y["a"], fps) - of0)
            delta = of0 - _frame(o_off[j], fps)
            dn, do = _dissolve_key(news, i, fps), _dissolve_key(olds, j, fps)
            same = delta == 0 and dn is not None and dn == do
            skip = 0
            if dn and not same:
                skip = dn[-1]
            if do and not same:
                skip = max(skip, do[-1] - delta)
            if n - skip > 0:
                runs.append((nf0 + skip, of0 + skip, n - skip))
            break
    merged: list[tuple[int, int, int]] = []
    for r in runs:
        if merged and merged[-1][0] + merged[-1][2] == r[0] and merged[-1][1] + merged[-1][2] == r[1]:
            merged[-1] = (merged[-1][0], merged[-1][1], merged[-1][2] + r[2])
        else:
            merged.append(r)
    return merged


def reuse_frames(old_keep: Any, new_keep: Any, fps: float, times: list[float]) -> list[tuple[int, int, int]]:
    """Mint a reuse, kockánként, a régi munkamásolat VALÓS kockaidőivel (times: megjelenítési sorrendben, s): az új
    idővonal j. kockája (a rács: fps) a forrás melyik pillanata, az a régi idővonalon mikor van, és ott a régi
    munkamásolatnak van-e kockája (fél kockán belül). Futam: egymást követő új kockák egymást követő, egyenletesen
    (±¼ kocka) álló régi kockákon. Így egy kiesett / megduplázott kocka, egy szelet-illesztés eltolódása a régiben csak
    ott szakítja meg a másolást (10-25, a 30 perces donorban: egy 2 kockás lyuk és két 0,65 ms-os csúszás — az
    index-aritmetika a lyuk után egy kockával mellé másolt volna)."""
    import bisect
    olds = segs(old_keep) or [{"a": 0.0, "b": _BIG, "src": None}]
    news = segs(new_keep) or [{"a": 0.0, "b": _BIG, "src": None}]
    o_off, acc = [], 0.0
    for y in olds:
        o_off.append(acc)
        acc += y["b"] - y["a"]
    half, step = 0.5 / fps, 1.0 / fps
    pairs: list[tuple[int, int]] = []
    on = 0.0
    for p_, x in enumerate(news):
        f0, f1 = _frame(on, fps), _frame(on + x["b"] - x["a"], fps)
        on += x["b"] - x["a"]
        dn = _dissolve_key(news, p_, fps)
        cands = [(q, y) for q, y in enumerate(olds) if y["src"] == x["src"] and (y.get("match") or {}) == (x.get("match") or {})]
        for j in range(f0, f1):
            sp = x["a"] + (j - f0) / fps                          # a forrás pillanata (a teljes újrakódolás kockája)
            hit = next(((q, y) for q, y in cands if y["a"] - 1e-6 <= sp < y["b"] - 1e-6), None)
            if hit is None:
                continue
            q, y = hit
            t_old = o_off[q] + sp - y["a"]
            k = bisect.bisect_left(times, t_old - half)
            if k >= len(times) or abs(times[k] - t_old) >= half:
                continue
            do = _dissolve_key(olds, q, fps)
            r_new, r_old = j - f0, round((t_old - o_off[q]) * fps)
            same = dn is not None and dn == do and r_new == r_old
            if (dn and r_new < dn[-1] and not same) or (do and r_old < do[-1] and not same):
                continue                                          # egy áttűnés kockája, ami a régiben nem ugyanaz
            pairs.append((j, k))
    runs: list[tuple[int, int, int]] = []
    for j, k in pairs:
        if runs and runs[-1][0] + runs[-1][2] == j and runs[-1][1] + runs[-1][2] == k \
                and abs(times[k] - times[k - 1] - step) <= step / 4:
            runs[-1] = (runs[-1][0], runs[-1][1], runs[-1][2] + 1)
        else:
            runs.append((j, k, 1))
    return runs


def stitch(runs: list[tuple[int, int, int]], keys: list[int], n_old: int, total: int,
           piece: int = 0) -> list[tuple[Any, ...]]:
    """A futamokból a munkamásolat darabjai sorban: ("copy", új első kocka, régi első kocka, db) — a régi kulcskockájától
    a következő futamon belüli kulcskockájáig (vagy a végéig) —, és ("enc", új első kocka, új utolsó utáni) — a többi,
    legfeljebb piece kockás darabokban (0: egyben). Csak tiszta kulcskockán kezdődik másolt darab."""
    ks = sorted(set(k for k in keys if 0 <= k < n_old) | {n_old})
    ops: list[tuple[Any, ...]] = []
    cur = 0

    def enc(a: int, b: int) -> None:
        step = piece if piece > 0 else max(1, b - a)
        while a < b:
            ops.append(("enc", a, min(b, a + step)))
            a += step
    for nf0, of0, n in runs:
        n = min(n, n_old - of0)
        k1 = next((k for k in ks if k >= of0), None)
        k2 = max((k for k in ks if k <= of0 + n), default=None)
        if k1 is None or k2 is None or k2 <= k1 or nf0 + (k1 - of0) < cur:
            continue
        c0 = nf0 + (k1 - of0)
        if c0 > cur:
            enc(cur, c0)
        ops.append(("copy", c0, k1, k2 - k1))
        cur = c0 + (k2 - k1)
    if cur < total:
        enc(cur, total)
    return ops


def donors(base: dict[str, Any]) -> list[tuple[Any, dict[str, Any]]]:
    """A meglévő munkamásolatok ugyanerről a felvételről ugyanazzal a korrekcióval, más idővonallal (az eredeti metájában:
    proxies), amelyekből az új (vágott / beszúrt) idővonal zöme átmásolható — a legtöbbet adó elöl. Stabilizálásnál
    nincs (az egész felvétel mozgásából számol)."""
    from app.storage import read_meta
    from app.tools import crafts
    fid0 = str(base.get("file_id") or "")
    if not fid0 or not base.get("keep") or stabilized(base) or not _smart_on():
        return []
    fps = float(base.get("fps") or 30)
    total = max(1, _frame(length(base["keep"], 0.0), fps))
    out = []
    for k, hit in ((read_meta(fid0) or {}).get("proxies") or {}).items():
        try:
            kd = json.loads(k)
            crafts.existing(hit["media_id"])
        except (ValueError, KeyError, FileNotFoundError):
            continue
        if kd.get("v") != 2 or kd.get("side") != PROXY_SIDE or kd.get("budget") != PROXY_BUDGET_MB \
                or (kd.get("adjust") or {}) != (base.get("adjust") or {}) or (kd.get("keep") or None) == base["keep"]:
            continue
        got = sum(n for _, _, n in reuse(kd.get("keep"), base["keep"], fps))
        if got >= SMART_MIN_REUSE * total:
            out.append((got, kd.get("keep"), hit))
    return [(kp, hit) for _, kp, hit in sorted(out, key=lambda x: -x[0])]


async def _grid(path: Path, pk: list[tuple[int, bool]]) -> Optional[tuple[float, list[float]]]:
    """A munkamásolat kockarácsa (kocka/s: a sáv névleges sebessége, ha a kockák ≥ 99 %-a ennyinként jön) és a kockák
    valós ideje (s, megjelenítési sorrendben, 0-tól). None: nincs ilyen rács. (A vágatlan munkamásolat az iPhone-felvétel
    névleges 30-ján áll, a vágott idővonal a mért átlagán — 29,993 —: más rács; az új másolat a régiéhez igazodik.)"""
    from fractions import Fraction
    from app.convert.engines import run
    out, _ = await run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=time_base,r_frame_rate",
                       "-of", "json", str(path), timeout=60)
    st = (json.loads(out.decode("utf-8", "replace") or "{}").get("streams") or [{}])[0]
    try:
        tb, rate = Fraction(st["time_base"]), Fraction(st["r_frame_rate"])
    except (KeyError, ValueError, ZeroDivisionError):
        return None
    if rate <= 0 or len(pk) < 2:
        return None
    ts = sorted(p for p, _ in pk)
    step = 1 / rate
    regular = sum(1 for a, b in zip(ts, ts[1:]) if abs((b - a) * tb - step) <= step / 4)
    if regular < 0.99 * (len(ts) - 1) or abs(ts[0] * tb) > step / 2:
        return None
    return float(rate), [float(p * tb) for p in ts]


async def _packets(path: Path) -> list[tuple[int, bool]]:
    """A videósáv csomagjai dekódolási sorrendben: (pts, kulcskocka-e)."""
    from app.convert.engines import run
    out, _ = await run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts,flags",
                       "-of", "compact=p=0:nk=1", str(path), timeout=600)
    pk = []
    for ln in out.decode("utf-8", "replace").split():
        f = ln.split("|")
        if len(f) >= 2 and f[0].lstrip("-").isdigit():
            pk.append((int(f[0]), "K" in f[1]))
    return pk


def clean_keys(pk: list[tuple[int, bool]]) -> list[int]:
    """A tiszta vágópontok (megjelenítési sorszámmal): kulcskocka, előtte dekódolt mind korábban, utána dekódolt mind
    később látszik (a zárt GOP) — innen a csomagok változatlanul másolhatók, a kockák ugyanazok."""
    order = {p: i for i, p in enumerate(sorted(p for p, _ in pk))}
    n, out = len(pk), []
    suf = [0] * n
    run_min = None
    for i in range(n - 1, -1, -1):
        run_min = pk[i][0] if run_min is None else min(run_min, pk[i][0])
        suf[i] = run_min
    run_max = None
    for i, (p, key) in enumerate(pk):
        if key and (run_max is None or run_max < p) and suf[i] == p:
            out.append(order[p])
        run_max = p if run_max is None else max(run_max, p)
    return out


async def _extradata(path: Path) -> str:
    from app.convert.engines import run
    out, _ = await run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-show_data", "-of", "json",
                       str(path), timeout=120)
    st = (json.loads(out.decode("utf-8", "replace") or "{}").get("streams") or [{}])[0]
    return "".join(ln.split(":", 1)[1][:40].replace(" ", "") for ln in str(st.get("extradata") or "").splitlines() if ":" in ln)


async def _audio_copy_of(orig: Path, k: Optional[int], copy: Path) -> bool:
    """A munkamásolat hangja az eredeti hangsávjának bitre azonos másolata-e (a vágatlan másolat AAC-nál -c copy-val
    viszi): az első 10 s csomagjai (md5) ugyanazok. Ha igen, a hang onnan dekódolva ugyanaz a minta, mint az eredetiből —
    a 3 GB-os eredeti helyett a kicsi másolatot kell olvasni (10-25, a 30 perces vágás hangja hideg lemezről 60 s volt)."""
    from app.convert.engines import run
    if k is None:
        return False

    async def md5s(p: Path, stream: str) -> list[str]:
        out, _ = await run("ffmpeg", "-v", "error", "-t", "10", "-i", str(p), "-map", stream, "-c", "copy", "-f", "framemd5", "-",
                           timeout=120)
        return [ln.rsplit(",", 1)[1].strip() for ln in out.decode("utf-8", "replace").splitlines() if ln and not ln.startswith("#")]

    async def pcm(p: Path, stream: str, at: float) -> str:        # dekódolva, kereséssel: az előtöltés / az időbélyegek is
        out, _ = await run("ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-t", "1", "-i", str(p), "-map", stream, "-f", "md5", "-",
                           timeout=120)
        return out.decode().strip()
    try:
        a, b = await md5s(orig, f"0:a:{k}"), await md5s(copy, "0:a:0")
        same = bool(a) and a == b
        for at in (0.0, 61.37):
            same = same and await pcm(orig, f"0:a:{k}", at) == await pcm(copy, "0:a:0", at)
    except Exception:
        return False
    return same


async def _sar(path: Path) -> str:
    """A sáv képpont-aránya setsar-értéknek: "0" (nincs megadva — a vágatlan munkamásolaté), "1" (1:1 — az idővonalé:
    setsar=1), vagy num/den. Az SPS-be kerül (VUI): ha a kódolt darabé más, a paraméterkészlet sem egyezik."""
    from app.convert.engines import run
    out, _ = await run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=sample_aspect_ratio",
                       "-of", "csv=p=0", str(path), timeout=60)
    v = out.decode().strip()
    if not v or v in ("N/A", "0:1"):
        return "0"
    return "1" if v == "1:1" else v.replace(":", "/")


async def _count(path: Path) -> int:
    """A videósáv kockáinak száma (MP4: a mintatábla szerint — nem kell végigolvasni)."""
    from app.convert.engines import run
    out, _ = await run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=nb_frames",
                       "-of", "csv=p=0", str(path), timeout=120)
    v = (out.decode().strip().split() or ["0"])[0]
    return int(v) if v.isdigit() else -1


async def _md5s(path: Path, t: float, n: int) -> list[str]:
    from app.convert.engines import run
    out, _ = await run("ffmpeg", "-v", "error", "-ss", f"{t:.6f}", "-i", str(path), "-map", "0:v:0", "-frames:v", str(n),
                       "-f", "framemd5", "-", timeout=300)
    return [ln.rsplit(",", 1)[1].strip() for ln in out.decode("utf-8", "replace").splitlines() if ln and not ln.startswith("#")]


async def smart_proxy(base: dict[str, Any], src: Path, old_keep: Any, donor: dict[str, Any]) -> Optional[tuple[dict[str, Any], list[str]]]:
    """Az új idővonal munkamásolata egy meglévőből (donor: proxies-bejegyzés ugyanazzal a korrekcióval, old_keep
    idővonallal) — lásd fent. → (az új base, a korrekció leírása), vagy None, ha nem éri meg / nem biztonságos (a hívó
    ilyenkor a teljes úton készít)."""
    import shutil
    import tempfile
    import time
    from app.config import settings
    from app.convert.engines import run
    from app.storage import output_path, temp_path, write_meta
    from app.storage.files import new_file_id
    from app.tools import crafts, mediaprobe, video_fx
    t_start = time.time()
    m = await mediaprobe.info(src)
    b = {k: v for k, v in base.items() if k not in ("media_id", "proxy", "auto", "clean_media_id", "rotated", "hdr")}
    W, H = int(m["display_width"] or b["width"]), int(m["display_height"] or b["height"])
    pw, ph = size(W, H)
    if donor.get("proxy") and (int(donor["proxy"]["w"]), int(donor["proxy"]["h"])) != (pw, ph):
        return None
    if donor.get("auto") is not None:
        b["auto"] = donor["auto"]                           # ugyanaz a korrekció, ugyanaz az eredetin mért auto
    adj = b.get("adjust") or {}
    old = crafts.existing(donor["media_id"])
    fps = float(m.get("fps") or 30)                         # a kódoló GOP-ja (_build_proxy: -g round(fps))
    pk = await _packets(old)
    grid = await _grid(old, pk)                             # a régi munkamásolat rácsa: az új is ezen áll (lásd _grid)
    if not grid:
        log.info("smart cut: a régi munkamásolatnak nincs állandó kockarácsa (%s)", old.name)
        return None
    fps_v, times = grid
    runs = reuse_frames(old_keep, b["keep"], fps_v, times)
    work = Path(tempfile.mkdtemp(prefix="proxy-smart-", dir=str(temp_path("").parent)))
    try:
        tl = await timeline(b, src, W, H, fps_v, work, size=(pw, ph))
        dur = tl.length
        total = _frame(dur, fps_v)
        ops = stitch(runs, clean_keys(pk), len(pk), total, piece=max(1, round(SMART_PIECE_S * fps_v)))
        enc_frames = sum(op[2] - op[1] for op in ops if op[0] == "enc")
        copied = sum(op[3] for op in ops if op[0] == "copy")
        if copied < SMART_MIN_REUSE * total or enc_frames > SMART_MAX_ENCODE_S * fps_v:
            log.info("smart cut: nem éri meg (másolva %d, kódolva %d kocka a %d-ból)", copied, enc_frames, total)
            return None
        gop = str(max(1, round(fps)))
        venc = ["-c:v", "libx264", "-preset", "veryfast", *_rate(dur), "-g", gop, "-pix_fmt", "yuv420p"]
        encs = [op for op in ops if op[0] == "enc"]
        sar = await _sar(old)                               # (a régiével egyező SPS-hez a képpont-arány is ugyanaz)
        threads = ["-threads", str(max(2, (os.cpu_count() or 4) // max(1, min(4, len(encs)))))]
        items = await transitions(tl, work, "p", venc) if encs else []
        gate = asyncio.Semaphore(4)

        async def encode(k: int, f0: int, f1: int) -> str:
            name = f"smart-enc-{k:04d}.mp4"
            async with gate:
                await _tl_window(tl, f0 / fps_v, f1 / fps_v, ["format=yuv420p", f"setsar={sar}"], items, venc + threads,
                                 work / name, work)
            return name

        async def audio() -> list[str]:
            if any(i.get("audio_stream") is not None for i in tl.infos.values()):
                tl_a = tl
                if old_keep is None and await _audio_copy_of(src, m.get("audio_stream"), old):
                    import copy as copy_mod                # a fő felvétel hangja a régi másolatból (ugyanazok a csomagok)
                    tl_a = copy_mod.copy(tl)
                    tl_a.paths, tl_a.infos = {**tl.paths, None: old}, {**tl.infos, None: await mediaprobe.info(old)}
                await tl_audio(tl_a, work / "proxy-a.m4a", work)
                return ["-i", "proxy-a.m4a", "-map", "1:a:0", "-c:a", "copy"]
            return []
        bounds = sorted(({op[2] for op in ops if op[0] == "copy"} | {op[2] + op[3] for op in ops if op[0] == "copy"})
                        - {0, len(pk)})
        seg_args = ["-segment_frames", ",".join(str(x) for x in bounds)] if bounds else ["-segment_time", "100000000"]

        async def segments() -> None:                       # a régi munkamásolat a vágópontokon darabolva, másolva
            # (-avoid_negative_ts disabled: különben az első darab a B-kockák késleltetésével eltolva kezdődik — 0,067 s —,
            # és az összefűzés minden utána jövő kockát ennyivel később tenne)
            await run("ffmpeg", "-v", "error", "-y", "-i", str(old), "-map", "0:v:0", "-c", "copy", "-avoid_negative_ts",
                      "disabled", "-f", "segment", *seg_args, "-reset_timestamps", "1", "-segment_format", "mp4",
                      "smart-seg-%04d.mp4",
                      timeout=settings.VIDEO_TIMEOUT_S, cwd=work)
        names_enc, aud, _ = await asyncio.gather(asyncio.gather(*(encode(k, op[1], op[2]) for k, op in enumerate(encs))),
                                                 audio(), segments())
        seg_start = [0] + bounds
        it = iter(names_enc)
        parts: list[tuple[str, int, int]] = []              # (fájl, új első kocka, db)
        for op in ops:
            if op[0] == "enc":
                parts.append((next(it), op[1], op[2] - op[1]))
                continue
            _, c0, k1, n = op
            for si, s0 in enumerate(seg_start):
                s1 = seg_start[si + 1] if si + 1 < len(seg_start) else len(pk)
                if k1 <= s0 and s1 <= k1 + n:
                    parts.append((f"smart-seg-{si:04d}.mp4", c0 + s0 - k1, s1 - s0))
        # ellenőrzés: paraméterkészlet, darabonként a kockák száma
        want = await _extradata(old)
        for name in names_enc:
            if await _extradata(work / name) != want:
                log.info("smart cut: a kódolt darab paraméterkészlete más (%s)", name)
                return None
        for name, _f0, n in parts:
            if await _count(work / name) != n:
                log.info("smart cut: %s kockaszáma nem %d", name, n)
                return None
        # minden darab a rács pontos helyére (a régi másolat illesztésein 0,65 ms-os csúszás lehet a saját idejében)
        (work / "smart.txt").write_text("".join(f"file '{n}'\nduration {k / fps_v:.6f}\n" for n, _, k in parts), "utf-8")
        await run("ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "smart.txt", "-c", "copy", "proxy-v.mp4",
                  timeout=settings.VIDEO_TIMEOUT_S, cwd=work)
        if await _count(work / "proxy-v.mp4") != total:
            log.info("smart cut: az összefűzött másolat kockaszáma nem %d", total)
            return None
        for name, f0, n in parts[1:9]:                      # az illesztések után ugyanazok a kockák, mint a darabban
            if await _md5s(work / "proxy-v.mp4", (f0 - 0.5) / fps_v, 2) != await _md5s(work / name, 0.0, 2):
                log.info("smart cut: a %d. kockánál (%s) az összefűzött kép más", f0, name)
                return None
        tmp = work / "proxy.mp4"
        await run("ffmpeg", "-v", "error", "-y", "-i", "proxy-v.mp4", *aud, "-map", "0:v:0", "-c:v", "copy", "-t", f"{dur:.4f}",
                  "-movflags", "+faststart", str(tmp), timeout=settings.VIDEO_TIMEOUT_S, cwd=work)
        fid = new_file_id("mp4")
        shutil.move(str(tmp), output_path(fid))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    write_meta(fid, operation="footage: the editor's copy", source_file_id=None, extra={"ephemeral": True})
    applied = video_fx.describe(adj, b.get("auto")) if adj else []
    b.update(media_id=fid, asset=f"footage-{fid.split('.')[0][:8]}.mp4", proxy={"w": pw, "h": ph})
    if (m.get("rotation") or 0) % 360:
        b["rotated"] = int(m["rotation"])
    if m.get("hdr"):
        b["hdr"] = m["hdr"]
    log.info("smart cut munkamásolat: %d kocka, ebből %d másolva, %d kódolva, %d darab (%.1f s)", total, copied, enc_frames,
             len(parts), time.time() - t_start)
    return b, applied


ALIGN_PAD = 4            # frames: the full-size piece starts this much earlier, the content match picks the right one


ALIGN_GAIN = 0.6         # a shift only when its match is clearly better than the unshifted one (distance × 0.6)


def frame_offset(clip_frames: Any, copy_frames: Any, pad: int) -> int:
    """Which frame of the full-size piece is the working copy's first frame (by picture content: normalised small luma,
    the mean squared difference over the next frames) → 0 … 2·pad. pad (no shift) unless another frame matches CLEARLY
    better (ALIGN_GAIN): an ambiguous match — a still picture, a pattern — never moves the piece."""
    import numpy as np
    m = min(len(copy_frames), max(1, len(clip_frames) - 2 * pad))
    ds = {}
    for j in range(0, 2 * pad + 1):
        if j + m > len(clip_frames):
            break
        ds[j] = float(np.mean([(clip_frames[j + i] - copy_frames[i]) ** 2 for i in range(m)]))
    if not ds or pad not in ds:
        return pad
    best = min(ds, key=lambda j: (ds[j], abs(j - pad)))
    return best if best != pad and ds[best] < ALIGN_GAIN * ds[pad] else pad


_VFR: dict[str, bool] = {}


def variable_rate(path: Path) -> bool:
    """A forrás változó kockasebességű-e (a telefonfelvétel: r_frame_rate 30/1, avg 29,993) — csak ott csúszhat a darab."""
    import json as _json
    import subprocess
    key = f"{path}:{path.stat().st_size}"
    if key not in _VFR:
        try:
            v = _json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                            "stream=r_frame_rate,avg_frame_rate", "-of", "json", str(path)],
                                           capture_output=True, text=True, check=True).stdout)["streams"][0]
            fr = lambda x: (lambda a, b: float(a) / float(b) if float(b) else 0.0)(*str(x).split("/")) if "/" in str(x) else float(x or 0)  # noqa: E731
            r, a = fr(v.get("r_frame_rate")), fr(v.get("avg_frame_rate"))
            _VFR[key] = bool(r and a and abs(r - a) / r > 1e-5)       # (S4: 30 vs 29.993 — a 0.02 % difference)
        except Exception:                                   # noqa: BLE001 — unknown: treated as constant (no shift)
            _VFR[key] = False
    return _VFR[key]


def _small_frames(path: Path, n: int, ss: Optional[float] = None) -> Any:
    import subprocess
    import numpy as np
    args = ["ffmpeg", "-v", "error"] + (["-ss", f"{ss:.6f}"] if ss is not None else []) + ["-i", str(path), "-frames:v", str(n),
            "-vf", "scale=160:90", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    raw = subprocess.run(args, capture_output=True, check=True).stdout
    a = np.frombuffer(raw, np.uint8).reshape(-1, 90, 160).astype(np.float32)
    return (a - a.mean(axis=(1, 2), keepdims=True)) / (a.std(axis=(1, 2), keepdims=True) + 1e-6)


async def clip(base: dict[str, Any], src: Path, dst: Path, a: float, b: float, plain: float, work: Path,
               align_to: Optional[Path] = None) -> float:
    """A videó [a, b) szakasza teljes méretben (vágott felvételnél a megtartott részekből), a lánccal (és b > plain
    esetén a kimerevített véggel) — a motor ezt rajzolja ott, ahol a felvétel maga mozog. Köztes fájl: a végső kódolás
    előtti minőségen (VIDEO_CRF_INTERMEDIATE). → hány másodperccel a darab eleje UTÁN van az a (0, ha nincs igazítás).
    align_to: a munkamásolat — VÁLTOZÓ kockasebességű forrásnál a darab kockái TARTALOM szerint igazodnak hozzá (állandónál
    semmi sem változik; és csak egyértelmű egyezésre tolódik — frame_offset) (10-26, S4: egy változó kockasebességű
    telefonfelvételből a darab a kezdőpontjától függően 1–2 kockával előbbre vagy hátrébb járt, mint a munkamásolat — a
    rétegek, a beszélő maszkja elcsúszott rajta): a darab ALIGN_PAD kockával korábban indul, és a munkamásolat a-beli
    kockájának megfelelő kocka a kezdete (a hívó az időtérképpel innen indítja: swap(skip=…))."""
    if align_to is not None and await asyncio.to_thread(variable_rate, src):     # (a constant rate: exactly as before)
        from app.tools import mediaprobe
        mc = await mediaprobe.info(align_to)
        fps_c = float(mc.get("fps") or base.get("fps") or 30)
        pad = min(ALIGN_PAD, int(a * fps_c + 1e-6))
        a0 = a - pad / fps_c
        await clip(base, src, dst, a0, b, plain, work)
        k0 = int(round(a * fps_c))
        cf = await asyncio.to_thread(_small_frames, dst, 2 * pad + 8)
        wf = await asyncio.to_thread(_small_frames, align_to, 8, (k0 - 0.5) / fps_c)   # (−½: its pts may sit a bit after k/fps)
        j = await asyncio.to_thread(frame_offset, cf, wf, pad)
        log.info("full-size piece %.3f–%.3f: the copy's frame is the piece's frame %d (pad %d)", a, b, j, pad)
        return j / fps_c
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
        return 0.0
    vf = chain(base, m, work) + ["format=yuv420p"] + tail
    await _video_window(src, [(cs, ce)], ",".join(vf), ["-t", f"{b - a:.4f}"] + enc, dst, work)
    return 0.0


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
function shp(s, f) {         // egy maszk alakja a forrás pixeleiben (a beszélő ablak maszkja, facecam.py) × f
  function m(a) { var o = []; for (var q = 0; q < a.length; q++) o.push([a[q][0] * f, a[q][1] * f]); return o; }
  var n = new Shape(); n.vertices = m(s.vertices); n.inTangents = m(s.inTangents); n.outTangents = m(s.outTangents);
  n.closed = s.closed; return n;
}
function convert(L, k) {
  var T = L.property("ADBE Transform Group");
  mul(T.property("ADBE Anchor Point"), k); mul(T.property("ADBE Scale"), 1 / k);
  var MG = L.property("ADBE Mask Parade");
  for (var i = 1; MG && i <= MG.numProperties; i++) {
    var M = MG.property(i), P = M.property("ADBE Mask Shape"), F = M.property("ADBE Mask Feather");
    if (P.numKeys) { for (var j = 1; j <= P.numKeys; j++) P.setValueAtKey(j, shp(P.keyValue(j), k)); }
    else P.setValue(shp(P.value, k));
    var fv = F.value; F.setValue([fv[0] * k, fv[1] * k]);
  }
}
function same(L) { try { return !!L.source && L.source.id === item.id; } catch (e) { return false; } }   // (=== a burkolón hamis)
var w0 = item.width, n = 0, users = [];
for (var i = 1; i <= app.project.numItems; i++) {
  var c = app.project.item(i);
  if (!(c instanceof CompItem)) continue;
  for (var j = 1; j <= c.numLayers; j++) if (same(c.layer(j))) users.push([c, c.layer(j)]);
}
var others = 0;              // (replaceSource-ot a motor még nem tud: egy darabnál minden használója a fő kompozícióban legyen —
for (var u = 0; u < users.length; u++) if (users[u][0].id !== comp.id) others++;   // a felvétel és a pakli beszélője)
if (start !== null && (users.length < 1 || others))
  throw new Error("the footage is used by " + others + " layer(s) outside the main composition");
item.replace(new File(file));
var k = item.width / w0;
for (var u = 0; u < users.length; u++) {
  var L = users[u][1];
  if (start !== null) {   // egy szakasz darabja a [start, …) időre: időtérképpel (a startTime a kulcsokat is eltolná)
    var skip = __SKIP__;      // the piece's frame that is the working copy's frame at start (footage.clip align_to)
    var d = item.duration - 1 / comp.frameRate - skip, i0 = L.inPoint, o0 = L.outPoint;   // (a beszélő: csak a pakli idejére)
    L.timeRemapEnabled = true;
    var TR = L.property("ADBE Time Remapping");
    while (TR.numKeys > 0) TR.removeKey(1);
    TR.setValueAtTime(start, skip); TR.setValueAtTime(start + d, skip + d);
    L.inPoint = Math.max(start, i0); L.outPoint = Math.min(comp.duration, start + item.duration - skip, o0);
  }
  convert(L, k); n++;
}
writeLn("swapped " + n);
"""


async def swap(project: Path, comp: str, asset: str, file: str, name: str, start: Optional[float] = None,
               skip: float = 0.0) -> str:
    """A projekt egy változata (--save-as), amelyben a munkamásolat (asset) helyén a teljes méretű felvétel (file, a
    munkamappában) áll. start=None: az egész felvétel (ugyanaz az idővonal); start=a: egy [a, …) szakasz darabja, csak a
    fő kompozíció felvétel-rétegén. → a változat fájlneve."""
    from app.tools import video
    code = (_SWAP.replace("__ASSET__", json.dumps(asset)).replace("__FILE__", json.dumps(file))
            .replace("__START__", "null" if start is None else f"{start:.6f}").replace("__COMP__", json.dumps(comp))
            .replace("__SKIP__", f"{max(0.0, skip):.6f}"))
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
PAD_S = 2.0               # egy darab képe legfeljebb ennyivel lehet rövidebb a receptbelinél (az utolsó kocka áll)
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


def whole(keep: Any, dur: float) -> bool:
    """Egy darab, a fő felvétel egésze (a vágás visszavonva, a klipek kivéve): a recept üres lehet."""
    ss = segs(keep)
    return len(ss) == 1 and ss[0]["src"] is None and ss[0]["a"] <= 1e-3 and ss[0]["b"] >= dur - 1e-3


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

    def frame(self, t: float) -> int:
        """A kimeneti idő kockája (a videó kockarácsán)."""
        return round(t * self.fps + 1e-6)

    def inputs(self, cs: float, ce: float, first: int = 0) -> tuple[list[str], str, str]:
        """A kimeneti [cs, ce) ablak: bemenetek, gráf-előtag (';'-vel zárva), a kész kép címkéje. Minden darab pontosan
        annyi kocka, amennyi a videó kockarácsán rá esik (0-tól, a végén szükség szerint az utolsó kocka ismételve): a
        -ss, a kockasebesség-átalakítás és a forrás vége (a tároló hosszabb lehet a képnél) ±1 kockát csúsztatna, és a
        váltás a rétegek, az áttűnések ideje elé / mögé esne — 10-25, valós anyagon a klip egy kockája villant az
        áttűnés előtt."""
        ps = pieces_out(self.ss, cs, ce)
        args, parts = [], []
        for k, (src, a, b, o) in enumerate(ps):
            n = max(1, self.frame(o + b - a) - self.frame(o))
            args += ["-ss", f"{a:.4f}", "-t", f"{b - a:.4f}", "-i", str(self.paths[src])]
            parts.append(f"[{first + k}:v]setpts=PTS-STARTPTS,{self.chain(src, f'tl{k}')},"
                         f"tpad=stop_mode=clone:stop_duration={PAD_S:g},trim=end_frame={n}[tv{k}]")
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
    képeivel úszik át (ha a forrásnak nincs folytatása, az utolsó kockája áll). A réteg a videó kockarácsán: az első
    kockája fél kockával a váltás kockája előtt indul (a kerekítés se hagyja ki — 10-25: a klip egy kockája villant)."""
    from app.config import settings
    from app.convert.engines import run
    items = []
    for i, (t, prev, nxt) in enumerate(tl.joins()):
        if nxt.get("join") != "dissolve":
            continue
        n = max(2, round(min(DISSOLVE_S, (nxt["b"] - nxt["a"]) / 2) * tl.fps))
        d = n / tl.fps
        pa = tl.infos[prev["src"]]
        fr = f"fps={tl.fps:g}"
        hold = f"tpad=stop_mode=clone:stop_duration={d + 1:.3f},trim=end_frame={n}"
        if float(pa.get("duration") or 0) - prev["b"] > d + 0.05:   # az előző forrás a vágás után is folytatódik
            in0 = ["-ss", f"{prev['b']:.4f}", "-i", str(tl.paths[prev["src"]])]
            x0 = f"[0:v]setpts=PTS-STARTPTS,{tl.chain(prev['src'], 'xa')},{fr},{hold}[x0]"
        else:                       # nincs folytatás: az utolsó kockája áll (a kép a tároló vége előtt is véget érhet)
            in0 = ["-ss", f"{max(prev['a'], prev['b'] - 1.0):.4f}", "-i", str(tl.paths[prev["src"]])]
            x0 = f"[0:v]{tl.chain(prev['src'], 'xa')},{fr},reverse,trim=end_frame=1,setpts=PTS-STARTPTS,{hold}[x0]"
        graph = (f"{x0};[1:v]setpts=PTS-STARTPTS,{tl.chain(nxt['src'], 'xb')},{fr},{hold}[x1];"
                 f"[x0][x1]xfade=transition=fade:duration={d:.4f}:offset=0,format=yuv420p[v]")
        name = f"fr-tr-{tag}-{i:03d}.mp4"
        await run("ffmpeg", "-v", "error", "-y", *in0, "-ss", f"{nxt['a']:.4f}", "-t", f"{d + 0.2:.4f}", "-i",
                  str(tl.paths[nxt["src"]]), "-filter_complex", graph, "-map", "[v]", "-an", "-frames:v", str(n), *enc, name,
                  timeout=settings.VIDEO_TIMEOUT_S, cwd=work)
        f0 = tl.frame(t) / tl.fps - 0.5 / tl.fps
        items.append({"name": name, "a": round(f0, 4), "b": round(f0 + d, 4),
                      "enable": f"between(t,{f0:.4f},{f0 + d - 1e-4:.4f})", "loop": False})
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
