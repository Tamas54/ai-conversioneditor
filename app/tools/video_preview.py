"""Gyors előnézet minden kérésre (10-23, Kommandant: „az eredményt GYORSAN akarom látni, itt a minőség mellékes…
a tökéletes eredmény sokáig tarthat, de ki kell írni, mennyi idő”).

Egy hosszú videónál (a végleges render becslése VIDEO_INLINE_RENDER_S fölött) az overlay / edit nem várja meg a
végleges renderet: a projekt elkészül, a motor a munkamásolatból (kicsiben, vázlatminőségben) csak a megváltozott
szakaszokat rajzolja meg — egy rövid MP4, rajta minden szakasz időkóddal —, a végleges render pedig háttérmunkaként
indul, becsült idővel (app/tools/render_eta.py). Ami változott:
  - új, módosított, törölt réteg: az ideje ±PAD;
  - vágáspont, egy beszúrt klip eleje és vége: ±CUT_PAD;
  - felirat: az új / átírt sorok (legfeljebb néhány);
  - zárókártya: a vége; kamera: a mozgások;
  - korrekció, akcentus, feliratstílus, zene: néhány minta a videó hosszában.
A teljes előnézet legfeljebb PREVIEW_MAX_S hosszú: ami nem fér bele, azt a válasz megszámolja (render scene=Lx-szel
egyenként is megnézhető).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("aice.video_preview")

PAD, CUT_PAD, SAMPLE_S = 2.0, 3.0, 4.0
_gate = None


def max_s() -> float:
    try:
        return max(5.0, float(os.getenv("VIDEO_PREVIEW_MAX_S", "60")))
    except ValueError:
        return 60.0


def inline_limit_s() -> float:
    """Ennél rövidebbre becsült végleges render a kérésen belül fut (nincs mit előnézni); fölötte: előnézet + háttér."""
    try:
        return float(os.getenv("VIDEO_INLINE_RENDER_S", "120"))
    except ValueError:
        return 120.0


def gate():
    """Az előnézet saját kapuja: nem áll be a végleges render (egyszerre egy) mögé."""
    import asyncio
    global _gate
    if _gate is None:
        _gate = asyncio.Semaphore(2)
    return _gate


def _span(a: float, b: float, label: str, pad: float, total: float) -> tuple[float, float, str]:
    return max(0.0, a - pad), min(total, b + pad), label


def _samples(total: float, label: str, n: int = 3) -> list[tuple[float, float, str]]:
    return [(max(0.0, total * f - SAMPLE_S / 2), min(total, total * f + SAMPLE_S / 2), label) for f in (0.1, 0.5, 0.9)[:n]]


def _clip_name(fid: str) -> str:
    from app.storage import display_label
    try:
        return display_label(fid)
    except Exception:
        return fid


def _joins(keep: Any) -> list[tuple[float, str, tuple[Any, ...]]]:
    """Az idővonal illesztései a videó idejében (footage.keep: vágás, beszúrt klip): (idő, mi, azonosító) — a beszúrt
    klip eleje és vége is (ott kell látni, észrevehető-e a váltás). Az azonosító a két szomszédos darab forrásideje:
    egy másik helyen végzett vágás / beszúrás után is ugyanaz."""
    from app.tools import footage
    offs = footage._offsets(keep) if keep else []
    out = []
    for (_, prev), (t, x) in zip(offs, offs[1:]):
        what = (f"clip {_clip_name(x['src'])} starts" if x["src"] else
                f"clip {_clip_name(prev['src'])} ends" if prev["src"] else "cut")
        out.append((round(t, 3), what, (prev["src"], round(prev["b"], 3), x["src"], round(x["a"], 3))))
    return out


def _ly(ly: dict[str, Any]) -> tuple[float, float, str]:
    a = float(ly.get("at") or 0)
    return a, a + float(ly.get("dur") or 0), f"{ly.get('id')} {ly.get('type') or ''}".strip()


def persistent(ly: dict[str, Any]) -> bool:
    """Végig látható réteg (vízjel, logó: a gyors render PERSISTENT_S-nél hosszabbja): nem alkot szakaszt — a 3,5–1815 s-os
    vízjel mindent egy szakaszba olvasztott, a 60 s-os korlát levágta, és az előnézet csak az első percet mutatta, a
    címkéje mégis mind a 24 réteget felsorolta (PROFILE.md 6.4); helyette néhány minta a videó hosszában."""
    from app.tools import fast_render
    return float(ly.get("dur") or 0) >= fast_render.PERSISTENT_S


def _layer_spans(lys: list[dict[str, Any]], total: float, suffix: str = "") -> list[tuple[float, float, str]]:
    out, held = [], []
    for ly in lys:
        a, b, lab = _ly(ly)
        if persistent(ly):
            held.append(lab + suffix)
        else:
            out.append(_span(a, b, lab + suffix, PAD, total))
    if held:
        out += _samples(total, ", ".join(held), 2)
    return out


def spans_new(spec: dict[str, Any], total: float) -> list[tuple[float, float, str]]:
    """Új videó: minden (rövid) réteg, vágáspont, a zárókártya; felirat csak, ha más nincs (az első sorok)."""
    story, base = spec.get("story") or {}, spec.get("base") or {}
    lys = story.get("layers") or []
    out = _layer_spans([ly for ly in lys if not persistent(ly)], total)
    out += [_span(t, t, what, CUT_PAD, total) for t, what, _ in _joins(base.get("keep"))]
    if base.get("end_card"):
        out.append(_span(float(base["plain"]["duration"]), total, "end card", 1.0, total))
    subs = story.get("subtitles") or []
    if subs and not out:
        out += [_span(float(c["start"]), float(c["end"]), "subtitles", 0.5, total) for c in subs[:3]]
    if not out and any(persistent(ly) for ly in lys):          # csak végig látható réteg: minták
        out += _layer_spans([ly for ly in lys if persistent(ly)], total)
    return out


def spans_edit(old: dict[str, Any], new: dict[str, Any], changes: dict[str, Any], total: float,
               remap: Any = None) -> list[tuple[float, float, str]]:
    """Szerkesztés: ami a kérésben változott. remap(a, b) → (a', b') | None: a régi idő az újban (vágás után)."""
    olds = {ly["id"]: ly for ly in (old.get("story") or {}).get("layers") or []}
    news = {ly["id"]: ly for ly in (new.get("story") or {}).get("layers") or []}
    touched = {str(c.get("id")) for c in changes.get("layers") or [] if isinstance(c, dict) and c.get("id")}
    out = _layer_spans([ly for lid, ly in news.items() if lid not in olds or lid in touched], total)
    for lid in touched - set(news):                              # törölt réteg: ahol volt (az új időben)
        if lid in olds:
            a, b, lab = _ly(olds[lid])
            if persistent(olds[lid]):
                out += _samples(total, f"{lab} (removed)", 2)
                continue
            ab = remap(a, b) if remap else (a, b)
            if ab:
                out.append(_span(ab[0], ab[1], f"{lab} (removed)", PAD, total))
    if "cut" in changes or "insert" in changes:                 # az új illesztések, és ahol egy régi megszűnt
        from app.tools import footage
        ok, nk = (old.get("base") or {}).get("keep"), (new.get("base") or {}).get("keep")
        was = {ident: (t, what) for t, what, ident in _joins(ok)}
        now = _joins(nk)
        out += [_span(t, t, what, CUT_PAD, total) for t, what, ident in now if ident not in was]
        idents, gone = {i for _, _, i in now}, {}
        for ident, (t, _what) in was.items():                   # (egy kivett klip két illesztése egy helyre esik)
            if ident not in idents:
                clip = ident[2] or ident[0]
                gone.setdefault((round(footage.remap_near(t, ok, nk), 3), clip), None)
        out += [_span(t2, t2, f"clip {_clip_name(clip)} taken out" if clip else "cut undone", CUT_PAD, total)
                for t2, clip in gone]
    if any(k in changes for k in ("end_card", "end_title", "end_subtitle")) and (new.get("base") or {}).get("end_card"):
        out.append(_span(float(new["base"]["plain"]["duration"]), total, "end card", 1.0, total))
    if "camera" in changes:
        cam = (new.get("opts") or {}).get("camera")
        for mv in cam if isinstance(cam, list) else []:
            a = float(mv.get("at") or 0)
            out.append(_span(a, a + float(mv.get("dur") or 0.6), "camera", PAD, total))
    if "subtitles" in changes:
        seen = {(c.get("text"), round(float(c.get("start", 0)), 1)) for c in (old.get("story") or {}).get("subtitles") or []}
        fresh = [c for c in (new.get("story") or {}).get("subtitles") or []
                 if (c.get("text"), round(float(c.get("start", 0)), 1)) not in seen]
        out += [_span(float(c["start"]), float(c["end"]), "subtitles", 0.5, total) for c in fresh[:4]]
    if "adjust" in changes:
        out += _samples(total, "colour and light")
    if "music" in changes or "music_file_id" in changes:
        out += _samples(total, "music", 2)
    if any(k in changes for k in ("accent", "title", "caption_style")) and not out:
        lys = sorted((ly for ly in news.values() if not persistent(ly)), key=lambda x: float(x.get("at") or 0))[:2]
        out += [_span(*_ly(ly)[:2], _ly(ly)[2], PAD, total) for ly in lys]
        subs = (new.get("story") or {}).get("subtitles") or []
        out += [_span(float(c["start"]), float(c["end"]), "subtitles", 0.5, total) for c in subs[:1]]
    return out


def pick(spans: list[tuple[float, float, str]], total: float, cap: Optional[float] = None,
         fps: float = 30.0) -> tuple[list[tuple[float, float, str]], int]:
    """Időrendben összevonva (a közeli szakaszok egybe), kockahatárra, a felső korlátig. → (szakaszok, kimaradt db)."""
    cap = max_s() if cap is None else cap
    snap = lambda x: round(round(x * fps) / fps, 4)                                         # noqa: E731
    merged: list[list[Any]] = []
    for a, b, lab in sorted((s for s in spans if s[1] - s[0] > 0.05), key=lambda s: s[0]):
        if merged and a <= merged[-1][1] + 1.0:
            merged[-1][1] = max(merged[-1][1], b)
            if lab not in merged[-1][2]:
                merged[-1][2].append(lab)
        else:
            merged.append([a, b, [lab]])
    out, used, left = [], 0.0, 0
    for a, b, labs in merged:
        a, b = snap(max(0.0, a)), snap(min(total, b))
        if b - a < 0.2:
            continue
        if used + (b - a) > cap:
            if used < cap - 2.0 and not out:                     # az első szakasz akkor is (levágva)
                out.append((a, snap(a + cap), ", ".join(labs)))
                used = cap
            else:
                left += 1
            continue
        out.append((a, b, ", ".join(labs)))
        used += b - a
    return out, left


async def render(work: Path, project: str, comp: str, spans: list[tuple[float, float, str]], scale: float,
                 out: Path, ass: Optional[str] = None) -> dict[str, Any]:
    """A szakaszok a motorral, kicsiben, vázlatminőségben (a munkamásolatból), egymás után egy MP4-be, mindegyiken
    az időkódja. ass: a felirat (fast_render.ass_subtitles, a videó idejében) — a hosszú videó projektje a kérés útjában
    felirat nélkül épül (video.ensure_full), a feliratot itt a libass rajzolja rá. → {seconds, parts, length}."""
    from app.convert.engines import run
    from app.tools import fast_render, video
    t0 = time.time()
    names = []
    async with gate():
        for k, (a, b, _lab) in enumerate(spans):
            name = f"pv-{k:03d}.mp4"
            await video._run("render", "--project", project, "--comp", comp, "--out", name, "--format", "h264",
                             "--start", f"{a:.4f}", "--end", f"{b:.4f}", "--resolution", f"{scale:.4f}", "--quality", "draft",
                             "--bitrate", "1500", "--audio", "auto", "--json", cwd=work)
            names.append(name)
        from app.tools import mediaprobe
        font = Path(fast_render.INTER_DIR, "Inter-SemiBold.otf")
        if ass:
            (work / "pv-subs.ass").write_text(ass, "utf-8")
        heard = [bool((await mediaprobe.info(work / n)).get("has_audio")) for n in names]
        args, chains, extra = [], [], len(names)
        for k, (name, (a, b, lab)) in enumerate(zip(names, spans)):
            args += ["-i", name]
            (work / f"pv-{k:03d}.txt").write_text(f"{video._tc(a)}–{video._tc(b)}  {lab}", "utf-8")   # (textfile: nincs escape)
            text = (f",drawtext=fontfile={font}:textfile=pv-{k:03d}.txt:x=12:y=12:fontsize=h/24:fontcolor=white:"
                    "box=1:boxcolor=black@0.55:boxborderw=6") if font.exists() else ""
            subs = (f",setpts=PTS+{a:.4f}/TB,ass=pv-subs.ass:fontsdir={fast_render.INTER_DIR},setpts=PTS-STARTPTS"
                    if ass else "")                       # (a darab 0-tól indul, a felirat a videó idejében)
            chains.append(f"[{k}:v]setsar=1{subs}{text}[v{k}]")
        if any(heard):                                   # hang nélküli darab: csend a helyén (a concat mindenhol hangot vár)
            for k, (a, b, _lab) in enumerate(spans):
                if heard[k]:
                    chains.append(f"[{k}:a]aresample=48000,aformat=channel_layouts=stereo[a{k}]")
                else:
                    args += ["-f", "lavfi", "-t", f"{b - a:.4f}", "-i", "anullsrc=r=48000:cl=stereo"]
                    chains.append(f"[{extra}:a]anull[a{k}]")
                    extra += 1
            pads = "".join(f"[v{k}][a{k}]" for k in range(len(names)))
            graph, maps = ";".join(chains) + f";{pads}concat=n={len(names)}:v=1:a=1[v][a]", ["-map", "[v]", "-map", "[a]"]
            aud = ["-c:a", "aac", "-b:a", "128k"]
        else:
            pads = "".join(f"[v{k}]" for k in range(len(names)))
            graph, maps, aud = ";".join(chains) + f";{pads}concat=n={len(names)}:v=1:a=0[v]", ["-map", "[v]"], []
        await run("ffmpeg", "-v", "error", "-y", *args, "-filter_complex", graph, *maps, "-c:v", "libx264", "-preset", "veryfast",
                  "-crf", "26", "-pix_fmt", "yuv420p", *aud, "-movflags", "+faststart", str(out), timeout=600, cwd=work)
    for n in names:
        (work / n).unlink(missing_ok=True)
        (work / n.replace(".mp4", ".txt")).unlink(missing_ok=True)
    (work / "pv-subs.ass").unlink(missing_ok=True)
    return {"seconds": round(time.time() - t0, 1), "parts": len(spans), "length": round(sum(b - a for a, b, _ in spans), 1)}


# ---------- a szakasz-gyorsítótáras HLS-előnézet bekötése (10-25, plan20261025_preview_cache.md) ----------

PREVIEW_MODES = ("hls", "mp4")


def preview_mode(v: Any) -> str:
    """preview: (nincs) / "hls" — a lejátszási lista; "mp4" — mellé a kész szakaszok összefűzve egy letölthető MP4-be."""
    x = str(v or "hls").strip().lower()
    if x in ("auto", "playlist", "m3u8", ""):
        x = "hls"
    if x not in PREVIEW_MODES:
        raise ValueError('preview: "mp4" (also a downloadable MP4 of the preview) — or leave it out for the playlist.')
    return x


def hls() -> Optional[tuple[Any, Any]]:
    """(preview_cache, preview_serve), ha a szakasz-gyorsítótáras előnézet be van kötve és be van kapcsolva
    (VIDEO_PREVIEW_HLS, alapból be); különben None — ekkor a PR #40-es MP4-előnézet (render) fut."""
    if os.getenv("VIDEO_PREVIEW_HLS", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    try:
        from app.tools import preview_cache, preview_serve
    except ImportError:
        return None
    try:
        return (preview_cache, preview_serve) if preview_cache.enabled() else None
    except Exception as e:                                   # az előnézet sose állítja meg a szerkesztést
        log.warning("a szakasz-előnézet nem kapcsolható be: %s", e)
        return None


def markers(spans: list[tuple[float, float, str]], total: float) -> list[dict[str, Any]]:
    """A változások helyei (spans_new / spans_edit — pick nélkül, korlát nélkül) ugrópontnak: időrendben, a videón
    belül, ugyanaz egyszer."""
    out, seen = [], set()
    for a, b, lab in sorted(spans, key=lambda s: (s[0], s[1])):
        a, b = round(max(0.0, a), 2), round(min(total, b), 2)
        if b - a < 0.05 or (a, b, lab) in seen:
            continue
        seen.add((a, b, lab))
        out.append({"from": a, "to": b, "what": lab})
    return out


def describe_hls(pv: dict[str, Any]) -> str:
    from app.tools import video
    first = pv.get("first_ready_s")
    mk = pv.get("markers") or []
    where = "; ".join(f"{video._tc(m['from'])}–{video._tc(m['to'])} {m['what']}" for m in mk[:8]) + ("…" if len(mk) > 8 else "")
    return (f"Preview of the whole video (a playlist, from the editor's small copy — the final video is better): "
            f"{pv['segments_dirty']} of {pv['segments_total']} segment(s) made new, {pv['reused']} reused"
            + (f"; the first change plays after {first:g} s" if first is not None else "; the first changed segment is "
               "still being made — the player waits for it")
            + (f". Changes: {where}." if mk else "."))


def describe(spans: list[tuple[float, float, str]], left: int, secs: float) -> str:
    from app.tools import video
    parts = "; ".join(f"{video._tc(a)}–{video._tc(b)} {lab}" for a, b, lab in spans[:8]) + ("…" if len(spans) > 8 else "")
    return (f"Quick preview ({secs:g} s to make, from the editor's small copy — the final video is better): "
            f"{len(spans)} part(s) — {parts}." + (f" {left} more change(s) aren't in it: action=\"render\" with scene=\"L…\" "
                                                  "shows one." if left else ""))


def dumps(spans: list[tuple[float, float, str]]) -> str:
    return json.dumps([[a, b, lab] for a, b, lab in spans], ensure_ascii=False)
