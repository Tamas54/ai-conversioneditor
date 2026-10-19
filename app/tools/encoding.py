"""Egységes videó-kódolási profil (OMLETT 1.2): minden lépés ugyanazzal a minőséggel kódol, hogy a lánc
(átméretezés → fűzés → végső render) ne veszítsen lépésenként.

10-19, éles teszt: ~14 Mbps forrás → resize után ~4,9 → merge után ~4,3 → végső render ~8,3 Mbps — minden lépés
CRF 20-szal (a végső a renderelő saját, rögzített 8 Mbps-os H.264-ével) kódolt újra, és a közbenső veszteséget a
végső render nem hozza vissza. Most:
  közbenső (eszközök: resize, merge, trim, crop, rotate, vízjel…)  x264 CRF 17   (VIDEO_CRF_INTERMEDIATE)
  végső (a videó-renderelő MP4-e)                                  x264 CRF 19   (VIDEO_CRF_FINAL)
  preset                                                           medium        (VIDEO_X264_PRESET)
  hang                                                             AAC 192k      (VIDEO_AUDIO_BITRATE)
  MP4/MOV                                                          +faststart
A végső kódolás: a renderelő (EffectCraft) közel veszteségmentes köztes fájlt ír (ProRes 422 LT, ~100 Mbps a
munkamappában, a végén törölve), abból x264 CRF 19 — a renderelő saját H.264-kódolója csak rögzített bitrátát tud
(a „quality” mód csak HEVC/AV1-nél él). VIDEO_FINAL_ENCODER=engine visszaállítja a régit (8 Mbps). A vázlat-render
(draft) marad a renderelő gyors H.264-énél.
Az értékek hívásonként olvasódnak a környezetből (a tesztek és az üzemeltetés is felülírhatja újraindítás nélkül).
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.getenv(name, "") or default)))
    except ValueError:
        return default


def intermediate_crf() -> int:
    return _int_env("VIDEO_CRF_INTERMEDIATE", 17, 0, 51)


def final_crf() -> int:
    return _int_env("VIDEO_CRF_FINAL", 19, 0, 51)


def preset() -> str:
    p = os.getenv("VIDEO_X264_PRESET", "medium").strip()
    return p if p in ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow") else "medium"


def audio_bitrate() -> str:
    b = os.getenv("VIDEO_AUDIO_BITRATE", "192k").strip()
    return b if re.fullmatch(r"\d{2,3}k", b) else "192k"


def final_encoder() -> str:
    """x264 (alap): a renderelő köztes ProRes-éből x264 CRF; engine: a renderelő saját H.264-e (a régi út)."""
    return "engine" if os.getenv("VIDEO_FINAL_ENCODER", "x264").strip().lower() == "engine" else "x264"


def x264(stage: str = "intermediate") -> list[str]:
    """H.264 4:2:0 a profil szerint; stage = intermediate | final."""
    crf = final_crf() if stage == "final" else intermediate_crf()
    return ["-c:v", "libx264", "-preset", preset(), "-crf", str(crf), "-pix_fmt", "yuv420p"]


def aac() -> list[str]:
    return ["-c:a", "aac", "-b:a", audio_bitrate()]


def faststart(fmt: str = "mp4") -> list[str]:
    return ["-movflags", "+faststart"] if fmt in ("mp4", "m4v", "mov") else []


def describe() -> dict:
    """A ténylegesen érvényes profil (a riportoknak, teszteknek)."""
    return {"intermediate_crf": intermediate_crf(), "final_crf": final_crf(), "preset": preset(),
            "audio": f"aac {audio_bitrate()}", "final_encoder": final_encoder(), "faststart": True}


def bt709_tags() -> list[str]:
    """A végső (SDR) kimenet színjelölése: a renderelő sRGB/BT.709-ben dolgozik."""
    return ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]


async def final_from_master(master: Path, dst: Path, timeout: float = 3600) -> Path:
    """A renderelő közel veszteségmentes köztes fájlja (ProRes) → a végső MP4: x264 CRF (final), AAC, +faststart."""
    from app.convert.engines import run
    await run("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(master), "-map", "0:v:0", "-map", "0:a:0?",
              *x264("final"), *bt709_tags(), *aac(), *faststart(dst.suffix.lstrip(".")), str(dst), timeout=timeout)
    return dst
