"""Live preview, captions part (lane L3, plan20261026_live_preview.md).

The browser draws the captions itself, with libass compiled to WASM (JASSUB, app/static/live-captions.js), from the SAME
ASS file the segment preview burns in with ffmpeg's ass filter (preview_cache._build → pc-subs.ass): the text of
fast_render.ass_subtitles(subs, style, W, H, total, accent, fps=fps), with the frame-snapped times (_ass_time). Same
file, same libass, same Inter files → it looks and times like the server.

    captions_part(pid, spec) → {"v", "hash", "ass_url", "fonts": [{"name", "url", "file"}], "W", "H", "frame", "fps",
                                "style", "events", "bytes", "hand_edited"}   or None (no captions)

- W×H is the ASS's PlayRes = the video's own size (spec.base width × height), exactly as the segment preview writes it;
  libass scales it to whatever canvas it draws on. "frame" is the size of the server's preview frames (the proxy,
  pw × ph): a LiveCaptions canvas of that size gives the server's pixels.
- The ASS is cached in memory by its content hash (it is text: ~0.1–1 MB, gzip ~5 %), and the hash is in the URL, so a
  served ASS is immutable. An input memo (subs, style, accent, geometry) skips the ~0.2 s regeneration on repeated
  scene requests. After a restart (or eviction) the route rebuilds it from the project's current spec; if the hash
  no longer matches, the project changed and the scene has to be fetched again (404).
- The URL tokens use preview_serve's HMAC (FILE_SIGNING_KEY), with the expiry rounded up to a 6-hour bucket: the same
  captions give the same URL for hours, so the player's diff (unchanged URL → no reload) and the browser cache hold.
- The fonts are the files fontconfig picks on the server for the ASS's families (fc-list in the prod image): "Inter Semi
  Bold" → Inter-SemiBold.otf, "Inter" → Inter-Regular.otf, "Inter" + \\b1 → Inter-Bold.otf. Their URLs are signed
  without the project (the same font is the same URL for every project: one download).
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

ASS_MEDIA = "text/x-ssa; charset=utf-8"
FONT_MEDIA = "font/otf"
IMMUTABLE = "private, max-age=31536000, immutable"
BUCKET_S = 6 * 3600                # the URL tokens' expiry is rounded up to this (stable URLs, see above)
FONT_PID = "live-fonts"            # the pseudo-project the font tokens are signed for
_MAX = 48                          # ASS texts kept in memory (a 30-minute karaoke track is ~1 MB)

_ASS: "OrderedDict[str, tuple[str, bytes]]" = OrderedDict()     # hash → (text, gzip)
_MEMO: "OrderedDict[str, str]" = OrderedDict()                  # input key → hash
_LOCK = threading.Lock()

# family (lower case), bold → the Inter file fontconfig picks on the server for it (fc-list in aice-local)
_WEIGHTS = {"thin": "Thin", "extra light": "ExtraLight", "extralight": "ExtraLight", "light": "Light", "regular": "Regular",
            "medium": "Medium", "semi bold": "SemiBold", "semibold": "SemiBold", "bold": "Bold",
            "extra bold": "ExtraBold", "extrabold": "ExtraBold", "black": "Black"}


def font_dir() -> Path:
    from app.tools import fast_render
    return Path(fast_render.INTER_DIR)


def _font_file(family: str, bold: bool) -> Optional[str]:
    fam = re.sub(r"\s+", " ", family.strip().lower())
    if fam == "inter":
        name = "Inter-Bold.otf" if bold else "Inter-Regular.otf"
    elif fam.startswith("inter ") and fam[6:] in _WEIGHTS:
        name = f"Inter-{_WEIGHTS[fam[6:]]}.otf"
    else:
        return None
    return name if (font_dir() / name).is_file() else None


def ass_fonts(ass: str) -> list[tuple[str, str]]:
    """The fonts the ASS asks libass for → [(name, file)]: each style's Fontname (+ its Bold flag), and each \\fn / \\b
    override inside a Dialogue line (fast_render.ass_subtitles: Cap = "Inter Semi Bold", Bar = "Inter", the bold style
    = \\fnInter\\b1). Only the families with an Inter file on this server are listed."""
    styles: dict[str, tuple[str, bool]] = {}
    want: list[tuple[str, bool]] = []                    # (family, bold), in the order libass first meets them

    def add(fam: str, bold: bool) -> None:
        if fam and (fam, bold) not in want:
            want.append((fam, bold))

    for ln in ass.splitlines():
        if ln.startswith("Style:"):
            f = [x.strip() for x in ln[6:].split(",")]
            if len(f) > 7:
                styles[f[0]] = (f[1], f[7] not in ("0", ""))
        elif ln.startswith("Dialogue:"):
            f = ln[9:].split(",", 9)
            if len(f) < 10:
                continue
            fam, bold = styles.get(f[3].strip(), ("", False))
            add(fam, bold)                                   # (libass opens the style's font before the overrides)
            for block in re.findall(r"\{([^}]*)\}", f[9]):
                m = re.search(r"\\fn([^\\}]+)", block)
                b = re.search(r"\\b(\d+)", block)
                if m or b:
                    fam = m.group(1).strip() if m else fam
                    bold = (int(b.group(1)) == 1 or int(b.group(1)) >= 600) if b else bold
                    add(fam, bold)
    out: list[tuple[str, str]] = []
    for fam, bold in want:
        name = _font_file(fam, bold)
        if name and name not in [n for _, n in out]:
            out.append((f"{fam} Bold" if bold and fam.lower() == "inter" else fam, name))
    return out


def geometry(spec: dict[str, Any]) -> Optional[dict[str, Any]]:
    """W, H, fps, total and the preview frame (pw, ph) — the same numbers preview_cache.plan_ctx gives the segment
    preview's ASS (a test holds them equal). None: not a video on the owner's own footage (no segment preview)."""
    from app.tools import footage
    base = spec.get("base") or {}
    if spec.get("kind") != "overlay" or not base:
        return None
    W, H = int(base["width"]), int(base["height"])
    fps = float(base.get("fps") or 30)
    fps = fps if 1 <= fps <= 120 else 30.0
    total = round(float(base["duration"]), 3)
    pw, ph = (int(base["proxy"]["w"]), int(base["proxy"]["h"])) if base.get("proxy") else footage.size(W, H)
    return {"W": W, "H": H, "fps": fps, "total": total, "pw": pw, "ph": ph}


def inputs(spec: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Everything the ASS depends on (as preview_cache._build reads it), or None when there are no captions."""
    from app.tools import video
    g = geometry(spec)
    if g is None:
        return None
    opts, story = spec.get("opts") or {}, spec.get("story") or {}
    subs = opts.get("subtitles") or story.get("subtitles") or []
    if not subs:
        return None
    return {**g, "subs": subs, "style": opts.get("caption_style") or "box", "accent": video._rgb(opts.get("accent"))}


def _key(inp: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(inp, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def build(inp: dict[str, Any]) -> str:
    """The ASS text — byte for byte what preview_cache._build writes into pc-subs.ass."""
    from app.tools import fast_render
    return fast_render.ass_subtitles(inp["subs"], inp["style"], inp["W"], inp["H"], inp["total"], inp["accent"],
                                     fps=inp["fps"])


def _store(text: str) -> str:
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    with _LOCK:
        if h in _ASS:
            _ASS.move_to_end(h)
        else:
            _ASS[h] = (text, gzip.compress(text.encode("utf-8"), 6, mtime=0))
            while len(_ASS) > _MAX:
                _ASS.popitem(last=False)
    return h


def ass_for(spec: dict[str, Any]) -> Optional[tuple[str, dict[str, Any]]]:
    """→ (hash, inputs) for the spec's captions (built once per distinct input), None without captions."""
    inp = inputs(spec)
    if inp is None:
        return None
    k = _key(inp)
    with _LOCK:
        h = _MEMO.get(k)
        if h and h in _ASS:
            _MEMO.move_to_end(k)
            _ASS.move_to_end(h)
            return h, inp
    h = _store(build(inp))
    with _LOCK:
        _MEMO[k] = h
        while len(_MEMO) > _MAX * 2:
            _MEMO.popitem(last=False)
    return h, inp


def cached(h: str) -> Optional[tuple[str, bytes]]:
    with _LOCK:
        v = _ASS.get(h)
        if v:
            _ASS.move_to_end(h)
        return v


def rebuild(pid: str, h: str) -> Optional[tuple[str, bytes]]:
    """A served URL whose ASS is not in memory (restart, eviction): rebuild it from the project's current spec. None:
    the project changed since (another hash) or has no captions any more."""
    from app.storage import read_meta
    spec = (read_meta(pid) or {}).get("spec") or {}
    got = ass_for(spec)
    if not got or got[0] != h:
        return None
    return cached(h)


# ---------- signed URLs (preview_serve's HMAC, the expiry rounded up to a bucket) ----------

def _exp(now: Optional[float] = None) -> int:
    from app.config import settings
    now = time.time() if now is None else now
    return (int(now) // BUCKET_S + 1) * BUCKET_S + settings.FILE_TTL_HOURS * 3600


def ass_token_h(h: str) -> str:
    return f"live-ass:{h}"


def font_token_h(key: str) -> str:
    return f"live-font:{key}"


def _signed(pid: str, h: str, uid: str, exp: int) -> str:
    from app import tenancy
    from app.tools import preview_serve as ps
    q = f"?p={pid}&t={exp}.{ps._sig(pid, h, exp, uid)}"
    return q + ("" if uid == tenancy.ADMIN_ID else f"&u={uid}")


def font_key(name: str) -> str:
    """Inter-SemiBold.otf → Inter-SemiBold-<sha12>.otf (the content in the URL: immutable, cacheable)."""
    data = (font_dir() / name).read_bytes()
    return f"{Path(name).stem}-{hashlib.sha256(data).hexdigest()[:12]}.otf"


_FONT_KEYS: dict[str, str] = {}


def font_file_of(key: str) -> Optional[Path]:
    """The served key → the font file (only the Inter files of this server, and only with the current content)."""
    m = re.fullmatch(r"(Inter-[A-Za-z]+)-([0-9a-f]{12})\.otf", key or "")
    if not m:
        return None
    name = m.group(1) + ".otf"
    p = font_dir() / name
    if not p.is_file():
        return None
    if _FONT_KEYS.get(name) is None:
        _FONT_KEYS[name] = font_key(name)
    return p if _FONT_KEYS[name] == key else None


def _font_url(base: str, name: str, exp: int) -> str:
    from app import tenancy
    from app.tools import preview_serve as ps
    if _FONT_KEYS.get(name) is None:
        _FONT_KEYS[name] = font_key(name)
    key = _FONT_KEYS[name]
    return f"{base}/api/video/live/font/{key}?t={exp}.{ps._sig(FONT_PID, font_token_h(key), exp, tenancy.ADMIN_ID)}"


def captions_part(pid: str, spec: Optional[dict[str, Any]] = None, *, base: Optional[str] = None,
                  now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """The scene's "captions" (live.json): None when the version has no captions. spec None: read from the project's
    meta. base: the URL prefix (default PUBLIC_BASE_URL, as preview_serve's URLs). Cheap when nothing changed (memo);
    a changed input costs ~0.2 s on a 30-minute show (use captions_part_async from a request handler)."""
    from app.storage import read_meta
    from app.tools import preview_serve as ps
    meta = None
    if spec is None:
        meta = read_meta(pid) or {}
        spec = meta.get("spec") or {}
    got = ass_for(spec)
    if not got:
        return None
    h, inp = got
    text, _gz = cached(h) or (build(inp), b"")
    base = (ps._base() if base is None else base).rstrip("/")
    exp, uid = _exp(now), ps._workspace(pid)
    if meta is None:
        meta = read_meta(pid) or {}
    return {"v": 1, "hash": h, "ass_url": f"{base}/api/video/live/captions/{h}.ass{_signed(pid, ass_token_h(h), uid, exp)}",
            "fonts": [{"name": n, "file": f, "url": _font_url(base, f, exp)} for n, f in ass_fonts(text)],
            "W": inp["W"], "H": inp["H"], "frame": [inp["pw"], inp["ph"]], "fps": inp["fps"], "style": inp["style"],
            "events": text.count("\nDialogue:"), "bytes": len(text.encode("utf-8")),
            "hand_edited": bool(meta.get("hand_edited"))}


async def captions_part_async(pid: str, spec: Optional[dict[str, Any]] = None, **kw: Any) -> Optional[dict[str, Any]]:
    """captions_part off the event loop (a changed caption track is ~0.2 s of PIL text measuring)."""
    return await asyncio.to_thread(captions_part, pid, spec, **kw)
