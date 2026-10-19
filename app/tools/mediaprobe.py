"""Egy felvétel adatai egy helyen (ffprobe): hossz, MEGJELENÍTÉSI méret (az elforgatás után), elforgatás, fps, kodekek,
hang, színátvitel / HDR, bitráta. A merge, a resize, a renderelő bemenete és a `video info` is ezt olvassa.

Elforgatás: a telefonos felvétel kódolva fekvő (pl. 1920×1080), a lejátszó a jelölés szerint fordítja el. Két helyen
állhat: a stream `side_data_list` kijelzőmátrixában („rotation”: az óramutatóval ELLENTÉTES fok, pl. −90) és a régi
`rotate` címkében (óramutató szerinti fok, pl. 90; az ffmpeg 5+ már nem írja ki). Mindkettőt olvassuk; a `rotation`
mező mindig óramutató szerinti 0 / 90 / 180 / 270. Az ffmpeg dekódoláskor magától elforgat (autorotate), így a
szűrőgráfba már a megjelenítési méretű kocka érkezik — a vászont is ebből kell számolni, nem a kódolt méretből.

    m = await mediaprobe.info(path)
    m["display_width"], m["display_height"], m["rotation"], m["hdr"]      # 1080, 1920, 90, "hlg"
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

HDR_TRANSFERS = {"arib-std-b67": "hlg", "smpte2084": "pq"}


def _fraction(x: Any) -> float:
    a, _, b = str(x or "0/1").partition("/")
    try:
        return float(a) / float(b or 1) if float(b or 1) else 0.0
    except ValueError:
        return 0.0


def _int(x: Any) -> Optional[int]:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def rotation(stream: dict[str, Any]) -> int:
    """Óramutató szerinti elforgatás (0 / 90 / 180 / 270): a régi `rotate` címke, különben a kijelzőmátrix."""
    tag = (stream.get("tags") or {}).get("rotate")
    if tag not in (None, ""):
        r = _int(tag)
        if r is not None:
            return r % 360
    for sd in stream.get("side_data_list") or []:
        if sd.get("rotation") is not None:
            r = _int(sd["rotation"])
            if r is not None:
                return (-r) % 360
    return 0


def hdr_kind(stream: dict[str, Any]) -> Optional[str]:
    """"hlg" | "pq" | None — a color_transfer szerint (az iPhone HDR-videója HLG, a Dolby Vision / HDR10 PQ)."""
    return HDR_TRANSFERS.get(str(stream.get("color_transfer") or "").lower())


def _bit_depth(stream: dict[str, Any]) -> int:
    raw = _int(stream.get("bits_per_raw_sample"))
    if raw:
        return raw
    pix = str(stream.get("pix_fmt") or "")
    for n in (16, 14, 12, 10):
        if f"p{n}" in pix:
            return n
    return 8


def summarize(raw: dict[str, Any]) -> dict[str, Any]:
    """ffprobe-JSON (-show_format -show_streams) → a közös mezők. Kép nélkül a videós mezők None-ok."""
    streams = raw.get("streams") or []
    fmt = raw.get("format") or {}
    vids = [s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")]
    auds = [s for s in streams if s.get("codec_type") == "audio"]
    # az első DEKÓDOLHATÓ hangsáv: az iPhone a sima AAC mellé térhangot (APAC) is ír, amit az ffmpeg nem ismer
    known = [k for k, s in enumerate(auds) if s.get("codec_name") not in (None, "", "unknown", "none")]
    v = vids[0] if vids else {}
    a = auds[known[0]] if known else {}
    w, h = _int(v.get("width")) or 0, _int(v.get("height")) or 0
    rot = rotation(v) if v else 0
    dw, dh = (h, w) if rot % 180 == 90 else (w, h)
    dur = float(fmt.get("duration") or v.get("duration") or a.get("duration") or 0)
    fps = _fraction(v.get("avg_frame_rate")) or _fraction(v.get("r_frame_rate")) if v else 0.0
    return {
        "duration": round(dur, 3),
        "has_video": bool(v),
        "width": w, "height": h,                              # kódolt méret
        "display_width": dw, "display_height": dh,            # ahogy a lejátszó mutatja (elforgatás után)
        "orientation": ("portrait" if dh > dw else "landscape" if dw > dh else "square") if v else None,
        "rotation": rot,
        "fps": round(fps, 3) if fps else None,
        "vcodec": v.get("codec_name"), "pix_fmt": v.get("pix_fmt"), "bit_depth": _bit_depth(v) if v else None,
        "acodec": a.get("codec_name"), "has_audio": bool(a), "audio_stream": known[0] if known else None,
        "audio_channels": _int(a.get("channels")), "sample_rate": _int(a.get("sample_rate")),
        "color_transfer": v.get("color_transfer"), "color_primaries": v.get("color_primaries"),
        "color_space": v.get("color_space"), "color_range": v.get("color_range"),
        "hdr": hdr_kind(v) if v else None,
        "bitrate": _int(fmt.get("bit_rate")), "video_bitrate": _int(v.get("bit_rate")),
        "audio_bitrate": _int(a.get("bit_rate")),
    }


async def probe(path: Path) -> dict[str, Any]:
    """A nyers ffprobe-JSON (formátum + streamek)."""
    from app.convert.engines import run
    out, _ = await run("ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path), timeout=60)
    return json.loads((out or b"{}").decode("utf-8", "replace") or "{}")


async def info(path: Path) -> dict[str, Any]:
    return summarize(await probe(path))


def info_sync(path: Path) -> dict[str, Any]:
    """Ugyanaz szinkron hívóknak (pl. video info) — az ffprobe gyors, a fejlécet olvassa."""
    import subprocess
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                         capture_output=True, timeout=60).stdout
    return summarize(json.loads((out or b"{}").decode("utf-8", "replace") or "{}"))


def public(m: dict[str, Any]) -> dict[str, Any]:
    """A felhasználónak (video info, read-metadata): a lényeg, belső mezők nélkül."""
    keep = ("duration", "display_width", "display_height", "width", "height", "rotation", "orientation", "fps", "vcodec",
            "acodec", "has_audio", "pix_fmt", "bit_depth", "color_transfer", "hdr", "bitrate")
    return {k: m.get(k) for k in keep}
