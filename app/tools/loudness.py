"""Hangerő-kiegyenlítés (EBU R128, ffmpeg loudnorm) — Kommandant 10-21: a Wan-klipek generált hangja ~13 dB-lel
hangosabb volt a beszédes felvételnél, az összefűzött videóban a bevágásnál ugrott a hangerő.

Két menet: mérés (integrált hangosság, valódi csúcs, tartomány), aztán LINEÁRIS korrekció a mért értékekkel — egy
erősítés az egész részre, nincs „pumpálás” (a dinamika marad). A közel néma rész (SILENT alatt) nem kap erősítést: a
zaját nem húzzuk fel. TARGET: a webes videók szokásos szintje.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("aice.loudness")

TARGET_LUFS = -16.0
TRUE_PEAK = -1.5
LRA = 11.0
SILENT_LUFS = -50.0


async def measure(src: Path, stream: Optional[int] = None) -> Optional[dict[str, float]]:
    """Az első (vagy a megadott) hangsáv mért értékei: {i, tp, lra, thresh, offset}; None, ha nincs hangja / nem mérhető."""
    from app.convert.engines import run
    m = f"0:a:{stream}" if stream is not None else "0:a:0"
    try:
        _, raw = await run("ffmpeg", "-hide_banner", "-nostats", "-i", str(src), "-map", m, "-vn",
                           "-af", f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK}:LRA={LRA}:print_format=json",
                           "-f", "null", "-", timeout=900)
        err = raw.decode("utf-8", "replace")
    except Exception as e:
        log.info("hangosság nem mérhető (%s): %s", src.name, str(e)[:160])
        return None
    j = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err)
    if not j:
        return None
    d = json.loads(j.group(0))
    try:
        out = {"i": float(d["input_i"]), "tp": float(d["input_tp"]), "lra": float(d["input_lra"]),
               "thresh": float(d["input_thresh"]), "offset": float(d["target_offset"])}
    except (KeyError, ValueError):            # -inf: néma
        return None
    return out if out["i"] > -99 else None


def chain(m: Optional[dict[str, float]], target: float = TARGET_LUFS) -> str:
    """A korrekció szűrőlánca (az aresample 48k-ra vissza — a loudnorm belül 192k-n dolgozik); néma / mérhetetlen: semmi."""
    if not m or m["i"] < SILENT_LUFS:
        return "anull"
    return (f"loudnorm=I={target}:TP={TRUE_PEAK}:LRA={max(LRA, round(m['lra'] + 1, 1))}:measured_I={m['i']}:"
            f"measured_TP={m['tp']}:measured_LRA={m['lra']}:measured_thresh={m['thresh']}:offset={m['offset']}:"
            "linear=true:print_format=none,aresample=48000")


async def level_file(src: Path, dst: Path, target: float = TARGET_LUFS, *, video: bool = True) -> dict[str, Any]:
    """Egy fájl hangja a célszintre (a kép érintetlen: másolva). Vissza: {before, target, changed}."""
    from app.convert.engines import run
    from app.tools import encoding
    m = await measure(src)
    if not m or m["i"] < SILENT_LUFS or abs(m["i"] - target) < 1.0:
        return {"before": m and m["i"], "target": target, "changed": False}
    await run("ffmpeg", "-v", "error", "-y", "-i", str(src), *(["-map", "0:v:0", "-c:v", "copy"] if video else []),
              "-map", "0:a:0", "-af", chain(m, target) + ",aformat=channel_layouts=stereo",   # monó bemenet is (ffmpeg 4.4)
              *(["-c:a", "pcm_s16le"] if dst.suffix.lower() == ".wav" else encoding.aac()),
              *(["-movflags", "+faststart"] if video else []), str(dst), timeout=900)
    return {"before": m["i"], "target": target, "changed": True}
