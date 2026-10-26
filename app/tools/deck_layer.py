"""Deck layer (N2, 10-26): one PowerPoint deck shown over time in a talk video — "pptx a képben", beside the speaker,
in front of the footage, or behind the speaker.

The layer (story.layers[], type "deck"):

    {"id": "L30", "type": "deck", "file_id": "<pptx>", "placement": "window" | "front" | "background",
     "side": "right" | "left" | "top" | "bottom"   (window only; landscape default right, portrait default bottom),
     "at": s, "dur" | "until": s,
     "slides": [{"at": s, "slide": n}, …]          explicit switch times (output time, slide numbers from 1), or
     "follow": [{"slide": n, "said": "phrase"}, …] switch when the phrase is spoken,
     "transition": "fade" | "cut"}                 (fade = the builder's 0.45 s crossfade; cut = hard switch)

`follow` is resolved BEFORE validation (`resolve`, async), deterministically, from the words the Studio already has:
the version's subtitle cues (their word start times when the cues carry them from the transcript, otherwise spread
over the cue by letter count — video._word_times), or, without subtitles, the footage transcript (transcript.transcribe,
cached in Postgres by the audio's SHA-256). Each phrase is looked for after the previous switch (transcript.find:
accent-, punctuation- and mishearing-tolerant). The stored layer always has `slides` (a resolved entry keeps its
`said`), so a later edit of one slide's time is a plain edit.

Slides to pictures (`slide_files`): LibreOffice (Impress → PDF) then pdftoppm at the project size (fitted, aspect
kept); DeckCraft only if LibreOffice fails. Measured on the owner's 20-slide deck (10-26): DeckCraft draws it in ~1 s but
drops several shape fills (the green "170 millió" box, the beige "Nettó" bar, the cyan / pink notes on slide 7) and
narrows some fonts; LibreOffice keeps them (8,8 s cold, once per deck content). The pictures are cached by the pptx's
CONTENT (SHA-256) and the size under <DATA_DIR>/preview-cache/_decks/<sha>-<W>x<H>/ — a re-upload of the same deck,
another version or another project reuses them.

Building (`items`): a deck becomes several builder layers (D.layers), in this order (each one above the previous):
  · the carrier "Layer L30 – deck …" for the whole span: window → a thin dark frame behind the slide, and the
    footage moved aside exactly like a `split` layer (item.split); front / background → a full-frame dark backdrop;
  · one layer per slide SEGMENT, "Layer L30.k – slide n": the slide picture, from its switch until the next switch
    (+ the crossfade, while the next one fades in over it).
Why per segment: the live preview renders one stacked-alpha clip per engine layer, keyed by that layer's own content
and length, never its absolute start (app/tools/live_layers.py). With one clip per segment, retiming one switch
re-renders only the two segments around it; a different slide picture only its own segment; moving the whole deck
nothing. A segment ≥ 30 s is a still after its fade (the live layer's loop probe finds period 1), so a 20-minute deck
costs a few short clips.

Background placement (the speaker in front of the slide): the slides are built full-frame like `front`; the speaker
is put back over them from a person matte. Live preview: the browser segments the footage (MediaPipe Tasks Vision
ImageSegmenter, Apache-2.0) during `matte_part`'s spans and draws the footage again through the mask over the
"behind" layers (app/static/live-player.js). Final render: the same recipe on the server, only over the background
spans (app/tools/deck_matte.py): a cached gray matte clip, and the builder puts the footage again over the deck with that
clip as its luma track matte (video_builder.jsx speakerLayer; fast_render draws that span with the engine).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("aice.deck_layer")

PLACEMENTS = ("window", "front", "background")
SIDES = ("right", "left", "top", "bottom")
TRANSITIONS = ("fade", "cut")
XFADE = 0.45                 # = video_builder.jsx overlayLayers: the fade-in min(0.45, dur/4) — the crossfade length
MIN_GAP = 1.0                # two switches at least this far apart
LAST_HOLD = 20.0             # without dur / until: the last slide stays this long
MAX_SLIDES = 300
DECK_DIR = "_decks"
RENDER_V = 1                 # the slide pictures' recipe (raise when it changes: a new cache folder)
SAID_LEAD = 0.15             # the switch a hair before the phrase (= video.said_to_times)
_BACKDROP = [0.055, 0.06, 0.08]
_FRAME = [0.035, 0.04, 0.055]


def _tc(t: float) -> str:
    t = max(0.0, float(t))
    return f"{int(t // 60):02d}:{t % 60:05.2f}" if t < 3600 else f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _deck_id(r: dict[str, Any]) -> Any:
    return r.get("file_id") or r.get("deck") or r.get("pptx")


# ---------- validation (called from video._layer_from; pure) ----------

def prepare(r: dict[str, Any], raw: dict[str, Any], base: Optional[dict[str, Any]], what: str) -> dict[str, Any]:
    """Before the common at / dur handling: the deck's start from its first slide when at isn't given, and an edit that
    moves the whole deck (a new at, no new slides) moves its slides with it."""
    from app.tools.video import timecode
    r = dict(r)
    if raw.get("follow") not in (None, [], "") and raw.get("slides") in (None, []):
        raise ValueError(f"{what} (deck): follow = [{{\"slide\": 2, \"said\": \"…\"}}] couldn't be matched to the words "
                         "(no subtitles and no transcript) — give slides = [{\"at\": \"02:30\", \"slide\": 1}, …] instead.")
    slides = raw.get("slides") if "slides" in raw else (base or {}).get("slides")
    if base and "at" in raw and "slides" not in raw and base.get("slides"):
        delta = timecode(raw["at"], f"{what} at") - float(base["at"])
        r["slides"] = [{**s, "at": round(float(s["at"]) + delta, 3)} for s in base["slides"]]
    elif r.get("at") is None and isinstance(slides, list) and slides:
        times = []
        for s in slides:
            if isinstance(s, dict) and s.get("at") is not None:
                times.append(timecode(s["at"], f"{what} slides[].at"))
        if times:
            r["at"] = min(times)
    return r


def fields(r: dict[str, Any], raw: dict[str, Any], base: Optional[dict[str, Any]], ly: dict[str, Any], what: str,
           notes: list[str]) -> dict[str, Any]:
    """The deck's own fields, checked: file_id (a .pptx), placement, side, transition, slides (sorted, inside the layer's
    span, at least MIN_GAP apart, numbers ≥ 1). ly: the common part already made (id, type, at, dur)."""
    from app.tools.video import timecode
    fid = _deck_id(r)
    if not fid or not isinstance(fid, str):
        raise ValueError(f"{what} (deck): file_id = the PowerPoint file (.pptx), e.g. {{\"at\": \"02:30\", \"type\": \"deck\", "
                         "\"file_id\": \"<pptx>\", \"placement\": \"window\", \"slides\": [{\"at\": \"02:30\", \"slide\": 1}, "
                         "{\"at\": \"02:52\", \"slide\": 2}]}.")
    if not re.search(r"\.(pptx|ppt|odp)$", fid, re.I):
        raise ValueError(f"{what} (deck): file_id {fid!r} isn't a slide deck — give a .pptx (or .ppt / .odp) file.")
    pl = str(r.get("placement") or "window").strip().lower()
    pl = {"beside": "window", "split": "window", "side": "window", "fullscreen": "front", "full": "front", "over": "front",
          "behind": "background", "back": "background", "backdrop": "background"}.get(pl, pl)
    if pl not in PLACEMENTS:
        raise ValueError(f"{what} (deck): placement {r.get('placement')!r} — window (beside the speaker) | front (over the "
                         "footage, the sound goes on) | background (the slide behind the speaker).")
    out: dict[str, Any] = {"file_id": fid, "placement": pl}
    if pl == "window":
        sd = str(r.get("side") or "").strip().lower()
        if sd:
            if sd not in SIDES:
                raise ValueError(f"{what} (deck): side {r.get('side')!r} — {' | '.join(SIDES)}.")
            out["side"] = sd
    tr = str(r.get("transition") or "fade").strip().lower()
    if tr not in TRANSITIONS:
        raise ValueError(f"{what} (deck): transition {r.get('transition')!r} — fade | cut.")
    out["transition"] = tr
    a, b = float(ly["at"]), float(ly["at"]) + float(ly["dur"])
    given = r.get("slides")
    if given in (None, []):
        given = [{"at": a, "slide": 1}]
    if not isinstance(given, list) or len(given) > MAX_SLIDES:
        raise ValueError(f"{what} (deck): slides = a list of {{\"at\": time, \"slide\": n}} (at most {MAX_SLIDES}).")
    slides = []
    for s in given:
        if not isinstance(s, dict) or s.get("slide") in (None, ""):
            raise ValueError(f"{what} (deck): each slides[] item is {{\"at\": \"02:30\", \"slide\": 3}} (slide numbers from 1).")
        try:
            n = int(s["slide"])
        except (TypeError, ValueError):
            raise ValueError(f"{what} (deck): slide {s.get('slide')!r} — a slide number (1 = the first).") from None
        if n < 1:
            raise ValueError(f"{what} (deck): slide {n} — slide numbers start at 1.")
        t = timecode(s.get("at", a), f"{what} slides[].at")
        e = {"at": round(t, 3), "slide": n}
        if isinstance(s.get("said"), str) and s["said"].strip():
            e["said"] = s["said"].strip()[:200]
        slides.append(e)
    slides.sort(key=lambda s: s["at"])
    kept: list[dict[str, Any]] = []
    for s in slides:
        if s["at"] < a - 0.05:
            raise ValueError(f"{what} (deck): slide {s['slide']} at {_tc(s['at'])} is before the deck starts ({_tc(a)}) — "
                             "give the deck an earlier at, or the slide a later time.")
        if s["at"] >= b - 0.5:
            notes.append(f"⚠ {what}: slide {s['slide']} at {_tc(s['at'])} is after the deck ends ({_tc(b)}) — left out; "
                         "give the deck a later until.")
            continue
        if kept and s["at"] - kept[-1]["at"] < MIN_GAP:
            notes.append(f"⚠ {what}: slide {s['slide']} at {_tc(s['at'])} comes less than {MIN_GAP:g} s after slide "
                         f"{kept[-1]['slide']} — left out.")
            continue
        kept.append(s)
    if not kept:
        raise ValueError(f"{what} (deck): no slide falls inside the deck's time ({_tc(a)}–{_tc(b)}).")
    kept[0] = {**kept[0], "at": round(a, 3)}               # the deck opens on its first listed slide
    out["slides"] = kept
    return out


def default_dur(r: dict[str, Any], at: float) -> Optional[float]:
    """Without dur / until: until the last slide has been shown for LAST_HOLD seconds."""
    from app.tools.video import timecode
    times = [timecode(s["at"]) for s in (r.get("slides") or []) if isinstance(s, dict) and s.get("at") is not None]
    return max(6.0, max(times + [at]) - at + LAST_HOLD) if times else None


def text(ly: dict[str, Any]) -> str:
    n = len(ly.get("slides") or [])
    return f"deck ({n} slide{'s' if n != 1 else ''}, {ly.get('placement', 'window')})"


def recut(ly: dict[str, Any], span: tuple[float, float], remap: Callable[[float], Optional[float]]) -> dict[str, Any]:
    """After a cut: every switch on the new timeline (a switch in a part that was cut out goes to the next kept time,
    the first one to the deck's new start), duplicates and too-close ones merged."""
    out, last = [], None
    for i, s in enumerate(ly.get("slides") or []):
        t = span[0] if i == 0 else remap(float(s["at"]))
        if t is None or t >= span[1] - 0.5:
            continue
        t = max(span[0], t)
        if last is not None and t - last < MIN_GAP:
            out[-1] = {**s, "at": out[-1]["at"]}            # the later slide wins the merged switch
            continue
        out.append({**s, "at": round(t, 4)})
        last = t
    return {**ly, "slides": out or [{**(ly.get("slides") or [{"slide": 1}])[0], "at": round(span[0], 4)}]}


# ---------- "switch when this phrase is spoken" ----------

def has_follow(raw: Any) -> bool:
    return isinstance(raw, list) and any(isinstance(x, dict) and isinstance(x.get("follow"), list) and x["follow"] for x in raw)


def words_from_cues(cues: list[dict[str, Any]]) -> dict[str, Any]:
    """Subtitle cues → a transcript-shaped {words, sentences} (transcript.find reads it): the cue's own word times when
    it carries them (from the transcript), else spread over the cue by letter count (video._word_times)."""
    from app.tools.video import _word_times
    words, sents = [], []
    for c in cues or []:
        try:
            a, b, txt = float(c["start"]), float(c["end"]), str(c.get("text") or "")
        except (KeyError, TypeError, ValueError):
            continue
        toks = txt.split()
        if not toks:
            continue
        ts = _word_times(txt, a, b, c.get("words"))
        for i, w in enumerate(toks):
            words.append({"w": w, "start": float(ts[i]), "end": float(ts[i + 1]) if i + 1 < len(ts) else b})
        sents.append({"start": a, "end": b, "text": txt})
    return {"words": words, "sentences": sents, "exact": all(isinstance(c.get("words"), list) for c in cues or [])}


def follow_to_slides(ly: dict[str, Any], tr: dict[str, Any], notes: list[str], *, after: float = 0.0,
                     source: str = "the subtitles") -> dict[str, Any]:
    """One deck layer's follow → slides: each phrase after the previous switch; a phrase that isn't heard is left out
    (the note says what the machine heard closest). Explicit entries (with at) stay as they are."""
    from app.tools import transcript
    who = f"Layer {ly['id']}" if ly.get("id") else f"The deck at {_tc(after)}"
    out = [dict(s) for s in ly.get("slides") or [] if isinstance(s, dict)]
    cur = after
    for f in ly.get("follow") or []:
        if not isinstance(f, dict):
            continue
        if f.get("at") is not None:
            out.append({"at": f["at"], "slide": f.get("slide")})
            continue
        phrase = str(f.get("said") or f.get("say") or "").strip()
        if not phrase:
            raise ValueError(f"{who}: each follow[] item is {{\"slide\": 3, \"said\": \"the words that bring it\"}}.")
        hit = transcript.find(tr, phrase, after=cur)
        if hit is None:
            close = transcript.nearest(tr, phrase)
            notes.append(f"⚠ {who}: slide {f.get('slide')} — “{phrase}” is not said after {_tc(cur)}"
                         + (f" (closest: “{close[:90]}”)" if close else "") + " — that switch is left out. Give its time "
                         "instead ({\"at\": \"12:34\", \"slide\": n}), or quote what is said.")
            continue
        t = round(max(0.0, hit["start"] - SAID_LEAD), 3)
        out.append({"at": t, "slide": f.get("slide"), "said": phrase})
        heard = f", heard as “{hit['said']}”" if hit.get("said") and transcript._norm(hit["said"]) != transcript._norm(phrase) else ""
        notes.append(f"{who}: slide {f.get('slide')} at {_tc(t)} — when “{phrase}” is said ({source}{heard}).")
        cur = hit["end"]
    new = {k: v for k, v in ly.items() if k != "follow"}
    new["slides"] = out
    return new


async def resolve(raw: Any, notes: list[str], *, cues: Optional[list[dict[str, Any]]] = None,
                  footage: Optional[Path] = None, language: Optional[str] = None, topts: Optional[dict[str, Any]] = None,
                  existing: Optional[list[dict[str, Any]]] = None, step: Callable[..., None] = lambda *a, **k: None) -> Any:
    """The deck layers' follow → slides, before validation. Words: the subtitle cues (output time) when there are any,
    else the footage transcript (cached). existing: the version's layers (an edit by id finds the deck's start)."""
    if not has_follow(raw):
        return raw
    from app.tools.video import timecode
    if cues:
        tr, src = words_from_cues(cues), "timed from the subtitles" if all(c.get("words") for c in cues) else \
            "timed from the subtitles; the word times inside a line are estimated"
    elif footage is not None:
        from app.tools import transcript
        from app.tools.video import transcript_lang
        step(0, 4, "listening to the footage (transcript) for the slide switches")
        tr, src = await transcript.transcribe(footage, transcript_lang(language), **(topts or {})), "from the transcript"
    else:
        tr, src = {"words": [], "sentences": []}, "no words"
    by_id = {x.get("id"): x for x in existing or [] if isinstance(x, dict)}
    out = []
    for x in raw:
        if isinstance(x, dict) and isinstance(x.get("follow"), list) and x["follow"]:
            start = x.get("at")
            if start is None and x.get("id") in by_id:
                start = by_id[x["id"]].get("at")
            try:
                after = timecode(start) if start is not None else 0.0
            except ValueError:
                after = 0.0
            x = follow_to_slides(x, tr, notes, after=after, source=src)
        out.append(x)
    return out


# ---------- slides → pictures (cached by the deck's content) ----------

_LOCKS: dict[str, tuple[Any, asyncio.Lock]] = {}


def _lock(key: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    hit = _LOCKS.get(key)
    if hit is None or hit[0] is not loop:
        hit = (loop, asyncio.Lock())
        _LOCKS[key] = hit
    return hit[1]


def content_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_dir(sha: str, W: int, H: int) -> Path:
    from app.tools import preview_cache
    return preview_cache.cache_root() / DECK_DIR / f"{sha[:32]}-{int(W)}x{int(H)}-v{RENDER_V}"


def _slide_aspect(src: Path) -> Optional[float]:
    try:
        from pptx import Presentation
        p = Presentation(str(src))
        return float(p.slide_width) / float(p.slide_height)
    except Exception:                                           # noqa: BLE001 — .ppt / .odp: the PDF's own size decides
        return None


async def _run(*args: str, cwd: Optional[Path] = None, timeout: float = 240) -> None:
    from app.convert.engines import run
    await run(*args, cwd=cwd, timeout=timeout)


async def _render(src: Path, work: Path, W: int, H: int) -> tuple[list[Path], str]:
    """LibreOffice → PDF → pdftoppm (fitted into W×H, aspect kept); DeckCraft if LibreOffice fails."""
    asp = _slide_aspect(src)
    try:
        from app.tools.anyconvert import lo_convert
        pdf = work / "deck.pdf"
        await lo_convert(src, pdf)
        wide = asp is None or asp >= W / H
        await _run("pdftoppm", "-png", "-scale-to-x", str(W) if wide else "-1", "-scale-to-y", "-1" if wide else str(H),
                   str(pdf), str(work / "s"), cwd=work)
        pages = sorted(work.glob("s-*.png"), key=lambda p: int(re.findall(r"(\d+)\.png$", p.name)[0]))
        if pages:
            return pages, "libreoffice"
        raise RuntimeError("pdftoppm gave no pages")
    except Exception as e:                                       # noqa: BLE001
        log.warning("deck: LibreOffice failed on %s (%s) — DeckCraft", src.name, e)
    from app.tools import crafts
    out = work / "dc"
    await crafts.run_cli(crafts.office_cli("deck"), "deckcraft-cli", "render", str(src), "--all", "--scale",
                         f"{W / 960:.4f}", str(out))
    pages = sorted(out.glob("*.png")) if out.is_dir() else sorted(work.glob("dc*.png"))
    if not pages:
        raise RuntimeError("The slides couldn't be drawn (LibreOffice and DeckCraft both failed).")
    return pages, "deckcraft"


async def slide_files(file_id: str, W: int, H: int) -> dict[str, Any]:
    """The deck's slides as pictures at the project size → {sha, files: [Path] (slide 1 first), engine, secs, cached}."""
    from app.tools import crafts
    src = crafts.existing(file_id)
    sha = await asyncio.to_thread(content_sha, src)
    root = cache_dir(sha, W, H)
    man = root / "slides.json"

    def hit() -> Optional[dict[str, Any]]:
        try:
            m = json.loads(man.read_text("utf-8"))
            files = [root / n for n in m["files"]]
            if all(f.exists() for f in files):
                return {"sha": sha, "files": files, "engine": m.get("engine"), "secs": m.get("secs"), "cached": True}
        except (OSError, ValueError, KeyError):
            pass
        return None

    got = hit()
    if got:
        return got
    async with _lock(sha + f"{W}x{H}"):
        got = hit()
        if got:
            return got
        t0 = time.monotonic()
        tmp = root.with_name(root.name + f".tmp-{uuid.uuid4().hex[:6]}")
        tmp.mkdir(parents=True)
        try:
            pages, engine = await _render(src, tmp, W, H)
            root.mkdir(parents=True, exist_ok=True)
            names = []
            for i, p in enumerate(pages, 1):
                n = f"slide-{i:03d}.png"
                os.replace(p, root / n)
                names.append(n)
            secs = round(time.monotonic() - t0, 2)
            man.write_text(json.dumps({"sha": sha, "W": W, "H": H, "files": names, "engine": engine, "secs": secs,
                                       "src": file_id}), "utf-8")
            log.info("deck %s: %d slide(s) drawn with %s in %.1f s", file_id, len(names), engine, secs)
            return {"sha": sha, "files": [root / n for n in names], "engine": engine, "secs": secs, "cached": False}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def asset_name(sha: str, n: int) -> str:
    return f"deck-{sha[:12]}-{n:03d}.png"


async def materialize(layers: list[dict[str, Any]], work: Path, W: int, H: int, notes: list[str]) -> list[str]:
    """The decks' slide pictures into the build folder (content-named: the same deck → the same asset names), the
    layer's runtime `_files` = {slide n: asset name}, `_aspect`. A slide number past the deck's end is left out (note).
    → the asset names to add to the project."""
    names: list[str] = []
    for ly in layers:
        if ly.get("type") != "deck":
            continue
        try:
            got = await slide_files(ly["file_id"], W, H)
        except FileNotFoundError:
            raise ValueError(f"Layer {ly['id']} (deck): the file {ly['file_id']} isn't stored (any more) — upload the deck again.") from None
        files = got["files"]
        keep = []
        for s in ly["slides"]:
            if s["slide"] > len(files):
                notes.append(f"⚠ Layer {ly['id']}: slide {s['slide']} at {_tc(s['at'])} — the deck has {len(files)} slides; left out.")
                continue
            keep.append(s)
        if not keep:
            keep = [{**ly["slides"][0], "slide": 1}]
        if keep[0]["at"] > ly["at"]:
            keep[0] = {**keep[0], "at": ly["at"]}
        ly["slides"] = keep
        ly["_files"] = {}
        for s in keep:
            nm = asset_name(got["sha"], s["slide"])
            if nm not in names:
                shutil.copyfile(files[s["slide"] - 1], work / nm)
                names.append(nm)
            ly["_files"][s["slide"]] = nm
        try:
            from PIL import Image
            with Image.open(files[0]) as im:
                ly["_aspect"] = im.width / max(1, im.height)
        except Exception:                                       # noqa: BLE001
            ly["_aspect"] = W / H
        ly["_engine"] = got["engine"]
    return names


# ---------- building (video.layer_comps) ----------

def _even(v: float) -> int:
    return max(4, int(round(v / 2)) * 2)


def geometry(ly: dict[str, Any], W: int, H: int) -> dict[str, Any]:
    """The slide's box and the carrier: window → beside the speaker (the footage moved to the other half, as `split`),
    the slide fitted to its half with a thin frame; front / background → the whole frame."""
    asp = float(ly.get("_aspect") or W / H)
    if ly.get("placement", "window") != "window":
        sw, sh = (W, W / asp) if asp >= W / H else (H * asp, H)
        return {"w": W, "h": H, "cx": W / 2, "cy": H / 2, "sw": sw, "sh": sh, "split": None, "frame": 0.0,
                "slide": [0.0, 0.0]}
    m = min(W, H)
    mg = round(m * 0.05)
    side = ly.get("side") or ("bottom" if H > W * 1.2 else "right")
    if side in ("left", "right"):
        rw, rh = W / 2 - mg * 1.5, H - 2 * mg
        cx, cy = (W * 0.75 - mg * 0.25, H / 2) if side == "right" else (W * 0.25 + mg * 0.25, H / 2)
        k = min(rw / W, rh / H)
        to = [W * 0.25 + mg * 0.25, H / 2] if side == "right" else [W * 0.75 - mg * 0.25, H / 2]
        slide = [m * 0.06 if side == "right" else -m * 0.06, 0.0]
    else:
        rw, rh = W - 2 * mg, H / 2 - mg * 1.5
        cx, cy = (W / 2, H * 0.75 - mg * 0.25) if side == "bottom" else (W / 2, H * 0.25 + mg * 0.25)
        k = min(rw / W, rh / H)
        to = [W / 2, H * 0.25 + mg * 0.25] if side == "bottom" else [W / 2, H * 0.75 - mg * 0.25]
        slide = [0.0, m * 0.06 if side == "bottom" else -m * 0.06]
    b = max(3.0, m * 0.006)
    sw = min(rw - 2 * b, (rh - 2 * b) * asp)
    sh = sw / asp
    return {"w": _even(sw + 2 * b), "h": _even(sh + 2 * b), "cx": cx, "cy": cy, "sw": sw, "sh": sh, "frame": b,
            "split": {"scale": round(k, 4), "to": [round(to[0], 1), round(to[1], 1)]}, "slide": slide}


def segments(ly: dict[str, Any], total: Optional[float] = None) -> list[dict[str, Any]]:
    """The slide segments: [{k (1…), slide, a, b (the slide's own span), at, dur (+ the crossfade under the next)}]."""
    A = float(ly["at"])
    B = A + float(ly["dur"])
    if total is not None:
        B = min(B, float(total))
    ss = [s for s in ly.get("slides") or [] if A - 0.05 <= float(s["at"]) < B - 0.5]
    fade = ly.get("transition", "fade") == "fade"
    out = []
    for i, s in enumerate(ss):
        a = A if i == 0 else float(s["at"])
        b = float(ss[i + 1]["at"]) if i + 1 < len(ss) else B
        tail = min(XFADE, max(0.0, B - b)) if fade and i + 1 < len(ss) else 0.0
        out.append({"k": i + 1, "slide": int(s["slide"]), "a": round(a, 3), "b": round(b, 3), "at": round(a, 3),
                    "dur": round(b - a + tail, 3)})
    return out


def items(ly: dict[str, Any], W: int, H: int, total: float) -> list[dict[str, Any]]:
    """The deck's builder layers (D.layers items, in drawing order: carrier, then the slide segments)."""
    g = geometry(ly, W, H)
    at, dur = float(ly["at"]), min(float(ly["dur"]), float(total) - float(ly["at"]))
    fade_in = (ly.get("anim") or {}).get("in") or "fade"
    fade_out = (ly.get("anim") or {}).get("out") or "fade"
    m = min(W, H)
    pl = ly.get("placement", "window")
    if pl == "window":
        els = [{"k": "rect", "name": "Frame", "size": [g["w"], g["h"]], "pos": [g["w"] / 2, g["h"] / 2], "color": _FRAME,
                "opacity": 94, "round": round(m * 0.008, 1)}]
    else:
        els = [{"k": "rect", "name": "Backdrop", "size": [W, H], "pos": [W / 2, H / 2], "color": _BACKDROP, "opacity": 100,
                "round": 0}]
    car: dict[str, Any] = {"id": ly["id"], "name": f"Layer {ly['id']} – deck ({pl})", "at": round(at, 3), "dur": round(dur, 3),
                           "w": int(g["w"]), "h": int(g["h"]), "pos": [round(g["cx"]), round(g["cy"])], "elements": els,
                           "anim": {"in": fade_in, "out": fade_out}, "slide": [0, 0], "deck": {"role": "carrier", "placement": pl}}
    if g["split"]:
        car["split"] = g["split"]
    out = [car]
    segs = segments(ly, total)
    files = ly.get("_files") or {}
    cut = ly.get("transition") == "cut"
    for i, s in enumerate(segs):
        name = files.get(s["slide"]) or files.get(str(s["slide"]))
        if not name:
            continue
        last = i == len(segs) - 1
        el = {"k": "image", "name": f"Slide {s['slide']}", "file": name, "size": [round(g["sw"], 1), round(g["sh"], 1)],
              "pos": [round(g["w"] / 2, 1), round(g["h"] / 2, 1)], "fit": "contain", "t0": 0, "anim": "none"}
        out.append({"id": f"{ly['id']}.{s['k']}", "name": f"Layer {ly['id']}.{s['k']} – slide {s['slide']}",
                    "at": s["at"], "dur": round(min(s["dur"], float(total) - s["at"]), 3), "w": int(g["w"]), "h": int(g["h"]),
                    "pos": [round(g["cx"]), round(g["cy"])], "elements": [el],
                    "anim": {"in": fade_in if i == 0 else ("none" if cut else "fade"), "out": fade_out if last else "none"},
                    "slide": [0, 0], "deck": {"role": "slide", "slide": s["slide"], "placement": pl}})
    return out


def box(ly: dict[str, Any], W: int, H: int) -> dict[str, Any]:
    """The deck's box on the frame (video._overlaps / _cover read w, h, cx, cy)."""
    g = geometry(ly, W, H)
    return {"w": int(g["w"]), "h": int(g["h"]), "cx": round(g["cx"]), "cy": round(g["cy"]), "slide": [0, 0],
            "split": g["split"], "els": [{"name": "Panel"}] if ly.get("placement", "window") == "window" else []}


def final_notes(layers: list[dict[str, Any]]) -> list[str]:
    """What the final render can't do on this server (a finding, so nobody is surprised by the exported video): the
    background deck's speaker is cut out on the server (app/tools/deck_matte.py) — without MediaPipe it isn't."""
    from app.tools import deck_matte
    if deck_matte.available() is None:
        return []
    out = []
    for ly in layers:
        if ly.get("type") == "deck" and ly.get("placement") == "background":
            out.append(f"Layer {ly['id']} (deck, background): the live preview puts you in front of the slides "
                       f"({_tc(ly['at'])}–{_tc(float(ly['at']) + float(ly['dur']))}); this server can't cut you out for the "
                       "final video — there the slides cover the footage like placement \"front\".")
    return out


# ---------- the live preview (app/tools/live_scene.py) ----------

def behind_ids(layers: list[dict[str, Any]]) -> dict[str, list[float]]:
    """The background decks: {deck id: [t0, t1]}."""
    return {str(ly["id"]): [float(ly["at"]), float(ly["at"]) + float(ly["dur"])] for ly in layers or []
            if isinstance(ly, dict) and ly.get("type") == "deck" and ly.get("placement") == "background"}


def matte_part(spec: dict[str, Any], scene_layers: list[dict[str, Any]], total: float) -> Optional[dict[str, Any]]:
    """The scene JSON's `matte`: the spans where the speaker is drawn again over the "behind" layers (the background
    decks' carrier and slide clips, flagged `behind: true` here). None without a background deck."""
    decks = behind_ids(((spec or {}).get("story") or {}).get("layers") or [])
    if not decks:
        return None
    spans = []
    for did, (a, b) in decks.items():
        a, b = max(0.0, a), min(float(total), b)
        if b > a:
            spans.append([round(a, 3), round(b, 3)])
    for e in scene_layers or []:
        lid = str(e.get("id") or "")
        if lid in decks or lid.split(".")[0] in decks:
            e["behind"] = True
    spans.sort()
    from app.tools.deck_matte import RECIPE           # the same recipe as the final render's matte
    return {"spans": spans, "model": RECIPE["model"], "levels": RECIPE["levels"], "ema": RECIPE["ema"],
            "motion": RECIPE["motion"], "size": RECIPE["size"]} if spans else None
