"""Háttérzene a videókhoz: a beépített CC0 zenetár (music_library.json → MUSIC_DIR), hangulat szerinti választás,
a videó hosszára igazított zenei alap, és halkítás a beszéd alatt (ducking).

A zenetár: 24 hurkolható darab, 8 hangulat (upbeat, calm, cinematic, lofi, corporate, playful, romantic, dramatic),
mind −16 LUFS-re hangolva, CC0 (a doory projekt eredeti művei; a kódjából semmi). A Docker-kép a
scripts/fetch_music.py-jal tölti le, fájlonként SHA256-tal.

A halkítás a ChatGPT-értékelés (10-17) „háttérzene, beszéd alatt automatikusan lehalkulva” kérése: eddig a zene
egyenletesen −20 dB-en szólt a narráció alatt. Itt a beszéd (narráció vagy a felvétel saját hangja) vezérli: ahol
beszéd van, a zene lejjebb megy, a szünetekben visszajön (ffmpeg sidechaincompress).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

_LIB = Path(__file__).with_name("music_library.json")


def music_dir() -> Path:
    return Path(os.getenv("MUSIC_DIR", "/opt/music"))


@lru_cache(maxsize=1)
def _manifest() -> dict[str, Any]:
    return json.loads(_LIB.read_text("utf-8"))


def tracks(mood: Optional[str] = None) -> list[dict[str, Any]]:
    """A telepített darabok (amelyiknek a fájlja megvan), hangulat szerint szűrve."""
    out = []
    for t in _manifest()["tracks"]:
        if mood and t["mood"] != mood:
            continue
        if (music_dir() / t["file"]).exists():
            out.append(t)
    return out


def moods() -> list[str]:
    return sorted({t["mood"] for t in _manifest()["tracks"]})


def find(track_id: str) -> Optional[dict[str, Any]]:
    return next((t for t in _manifest()["tracks"] if t["id"] == track_id), None)


def pick(choice: Optional[str], seed: str = "") -> Optional[dict[str, Any]]:
    """choice: egy darab azonosítója (pl. "still-water") vagy egy hangulat ("calm") → a darab. Hangulaton belül a
    seed (pl. a videó címe) dönt, determinisztikusan — ugyanaz a videó újrarenderelve ugyanazt a zenét kapja."""
    if not choice:
        return None
    t = find(choice)
    if t is not None:
        return t if (music_dir() / t["file"]).exists() else None
    pool = tracks(choice)
    if not pool:
        if choice not in moods():
            raise ValueError(f"music: {choice!r} — choose a mood ({', '.join(moods())}), a track id, or \"none\"")
        return None
    k = int(hashlib.sha1(seed.encode("utf-8")).hexdigest(), 16) % len(pool)
    return pool[k]


def public(t: dict[str, Any]) -> dict[str, Any]:
    return {"id": t["id"], "title": t["title"], "mood": t["mood"], "genre": t.get("genre"), "bpm": t.get("bpm"),
            "seconds": round(t["durationMs"] / 1000, 1), "license": "CC0-1.0 (no attribution needed)"}


async def _ffmpeg(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg: {err.decode('utf-8', 'replace').strip()[-300:]}")


async def bed(t: dict[str, Any], seconds: float, out: Path, fade_in: float = 1.0, fade_out: float = 2.5,
              gain_db: float = 0.0) -> Path:
    """A darab a kért hosszra: hurkolva (a darabok hurkolhatók), elején be-, végén kiúsztatva."""
    src = music_dir() / t["file"]
    seconds = max(1.0, float(seconds))
    fo = min(fade_out, seconds / 3)
    await _ffmpeg("-stream_loop", "-1", "-i", str(src), "-t", f"{seconds:.3f}",
                  "-af", f"afade=t=in:d={min(fade_in, seconds / 4):.2f},afade=t=out:st={seconds - fo:.3f}:d={fo:.2f},"
                         f"volume={gain_db:.1f}dB",
                  "-ac", "2", "-ar", "48000", str(out))
    return out


async def loudness(path: Path) -> Optional[float]:
    """Integrált hangosság (LUFS, EBU R128) — None, ha néma vagy nem mérhető."""
    proc = await asyncio.create_subprocess_exec("ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
                                                "loudnorm=print_format=json", "-f", "null", "-",
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    try:
        v = float(json.loads(err.decode("utf-8", "replace")[err.decode("utf-8", "replace").rindex("{"):])["input_i"])
    except (ValueError, KeyError):
        return None
    return v if v > -70 else None


async def level_below(speech: Path, music: Path, below: float = 14.0, fallback: float = -12.0) -> float:
    """A zene erősítése (dB) úgy, hogy a beszéd mért hangossága alatt `below` dB-lel szóljon — halk és hangos felvételen
    is ugyanannyival; ha valamelyik nem mérhető, a fallback."""
    s, m = await asyncio.gather(loudness(speech), loudness(music))
    if s is None or m is None:
        return fallback
    return max(-40.0, min(6.0, (s - below) - m))


async def duck(speech: Path, music: Path, out: Path, music_db: Optional[float] = None, depth: float = 4.0) -> Path:
    """A zene a beszéd alatt halkul (sidechain): music_db = a zene erősítése (alapból: a beszéd alatt 14 dB-lel,
    mérve), depth = mennyit enged a beszéd alatt (arány). → csak a halkított zene (külön sáv: szerkeszthető marad)."""
    if music_db is None:
        music_db = await level_below(speech, music)
    await _ffmpeg("-i", str(music), "-i", str(speech), "-filter_complex",
                  f"[0:a]volume={music_db:.1f}dB[m];[1:a]aformat=channel_layouts=stereo,aresample=48000[s];"
                  f"[m][s]sidechaincompress=threshold=0.015:ratio={depth:.1f}:attack=15:release=450:makeup=1[d]",
                  "-map", "[d]", "-ac", "2", "-ar", "48000", str(out))
    return out


async def mix(speech: Path, music: Path, out: Path, music_db: Optional[float] = None, depth: float = 4.0) -> Path:
    """Beszéd + alatta halkuló zene egy sávba (pl. a felvétel saját hangja + a választott zene)."""
    if music_db is None:
        music_db = await level_below(speech, music)
    await _ffmpeg("-i", str(music), "-i", str(speech), "-filter_complex",
                  f"[0:a]volume={music_db:.1f}dB[m];[1:a]aformat=channel_layouts=stereo,aresample=48000,asplit=2[s][k];"
                  f"[m][k]sidechaincompress=threshold=0.015:ratio={depth:.1f}:attack=15:release=450:makeup=1[d];"
                  "[s][d]amix=inputs=2:normalize=0:duration=first[o]",
                  "-map", "[o]", "-ac", "2", "-ar", "48000", str(out))
    return out
