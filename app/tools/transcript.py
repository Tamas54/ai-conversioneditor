"""Időbélyeges átirat egy felvételhez (beszéd → szavak időbélyeggel), és egy elhangzó mondat megkeresése benne.

Mire: a saját felvételre tett grafikai réteg mondathoz köthető („amikor elhangzik, hogy az ügyfél fellebbez, jelenjen
meg a nyíl”) — ne másodpercre kelljen megadni (a ChatGPT-értékelés 10-17). Plusz feliratfájl (SRT) a felvételhez.

Motor: faster-whisper (MIT), Whisper large-v3-turbo, CPU-n int8 — helyben, kulcs és díj nélkül. Mérve 10-17
(magyar jogi mondatok, 14 s, 8 szál): large-v3-turbo 21,5 s, a mondatok jók („a másodfokú bíróság 6 hónapon belül
dönt”); a small 7,5 s, de „ügyférfellerbez”, „belődönt” — ezért a turbo. A kiadott FilmCraft CLI Whisper nélkül
készült (a `whisper` feature hiányzik), ezért nem az. A modell első használatkor töltődik le (~1,6 GB, a kötetre).
AMIT EGYSZER ÁTÍRTUNK, NEM ÍRJUK ÁT ÚJRA: a hang SHA256-ja szerint az ocr_cache táblában (Postgres) marad.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Optional

from app.config import settings

log = logging.getLogger("aice.transcript")

MODEL = os.getenv("ASR_MODEL", "large-v3-turbo")
VER = "asr-v1"
_model = None
_lock = asyncio.Lock()            # egyszerre egy átírás (CPU-igényes; a többi sorban vár)


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _models_dir() -> Path:
    d = Path(os.getenv("ASR_MODELS_DIR") or settings.DATA_DIR / "models" / "whisper")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        threads = max(2, min(16, (os.cpu_count() or 4) // 2))
        _model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=threads, download_root=str(_models_dir()))
    return _model


async def _pcm16k(src: Path) -> bytes:
    """A felvétel hangja 16 kHz mono 16 bites nyers PCM-ként (ffmpeg; a PyAV-verziótól független)."""
    proc = await asyncio.create_subprocess_exec("ffmpeg", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                                                "-f", "s16le", "-", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise ValueError(f"No sound could be read from this file ({err.decode('utf-8', 'replace').strip()[-160:]}).")
    return out


def _run(pcm: bytes, language: Optional[str]) -> dict[str, Any]:
    import numpy as np
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    segs, info = _load().transcribe(audio, language=language or None, word_timestamps=True, vad_filter=True)
    words, sentences = [], []
    for s in segs:
        sentences.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()})
        for w in s.words or []:
            if w.word.strip():
                words.append({"w": w.word.strip(), "start": round(w.start, 2), "end": round(w.end, 2)})
    return {"language": info.language, "model": MODEL, "duration": round(len(audio) / 16000, 2),
            "sentences": sentences, "words": words}


async def transcribe(src: Path, language: Optional[str] = None) -> dict[str, Any]:
    """→ {language, model, duration, sentences [{start, end, text}], words [{w, start, end}]} — gyorsítótárból, ha volt."""
    from app import db
    if not available():
        raise RuntimeError("Speech recognition isn't installed on this server (faster-whisper).")
    pcm = await _pcm16k(src)
    if len(pcm) < 16000 * 2 * 0.3:
        return {"language": None, "model": MODEL, "duration": round(len(pcm) / 32000, 2), "sentences": [], "words": []}
    sha = hashlib.sha256(pcm).hexdigest()
    ver = VER + (f":{language}" if language else "")
    try:
        hit = await db.ocr_cache_get(sha, ver, MODEL)        # a közös tartós gyorsítótár (sha256, verzió, modell → szöveg)
    except Exception:
        hit = None
    if hit:
        return json.loads(hit)
    async with _lock:
        out = await asyncio.to_thread(_run, pcm, language)
    try:
        await db.ocr_cache_put(sha, ver, MODEL, json.dumps(out, ensure_ascii=False))
    except Exception:
        log.warning("átirat-gyorsítótár írása nem sikerült", exc_info=True)
    return out


# ---------- egy elhangzó mondat helye ----------

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", "", s)


def find(tr: dict[str, Any], phrase: str, after: float = 0.0) -> Optional[dict[str, Any]]:
    """A kifejezés (pl. „az ügyfél fellebbez”) helye az átiratban: {start, end, said, score, sentence_end} — ékezet-,
    írásjel- és kisebb félrehallás-tűrő (a modell „ügyfér felletbez”-t hallhat). after: csak ennél későbbi előfordulás."""
    target = [_norm(t) for t in phrase.split() if _norm(t)]
    words = [w for w in tr.get("words") or [] if w["start"] >= after - 0.01]
    if not target or not words:
        return None
    joined_t = "".join(target)
    best = None
    n = len(target)
    for size in {max(1, n - 1), n, n + 1}:
        for i in range(0, max(0, len(words) - size) + 1):
            chunk = words[i:i + size]
            score = difflib.SequenceMatcher(None, "".join(_norm(w["w"]) for w in chunk), joined_t, autojunk=False).ratio()
            if best is None or score > best[0] + 1e-9:
                best = (score, chunk)
    if best is None or best[0] < 0.72:
        return None
    score, chunk = best
    start, end = chunk[0]["start"], chunk[-1]["end"]
    sent_end = next((s["end"] for s in tr.get("sentences") or [] if s["start"] - 0.05 <= start <= s["end"] + 0.05), end)
    return {"start": start, "end": end, "said": " ".join(w["w"] for w in chunk), "score": round(score, 2),
            "sentence_end": max(sent_end, end)}


def nearest(tr: dict[str, Any], phrase: str) -> Optional[str]:
    """Hiba esetére: a legjobban hasonlító mondat (hogy az ügynök/felhasználó tudja, mit hallott a gép)."""
    sents = [s["text"] for s in tr.get("sentences") or []]
    hit = difflib.get_close_matches(phrase, sents, n=1, cutoff=0.0)
    return hit[0] if hit else None


def srt(tr: dict[str, Any]) -> str:
    def ts(t: float) -> str:
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return "".join(f"{i}\n{ts(s['start'])} --> {ts(s['end'])}\n{s['text']}\n\n"
                   for i, s in enumerate(tr.get("sentences") or [], 1))


def _join(words: list[dict[str, Any]]) -> str:
    """Szavak szöveggé: a kötőjellel/írásjellel kezdődő token az előzőhöz tapad („ilyen-olyan”, nem „ilyen -olyan”)."""
    out = ""
    for w in words:
        t = w["w"]
        out += t if not out or re.match(r"[-‐–,.;:!?…)\]”»]", t) or out.endswith(("(", "„", "«")) else " " + t
    return out


def cues(tr: dict[str, Any], max_chars: int = 84, max_dur: float = 6.5, gap: float = 0.8) -> list[dict[str, Any]]:
    """Feliratsorok a szavak SAJÁT időbélyegeiből: legfeljebb két sor (~2×42 betű) és ~6,5 s; mondatvégen mindig, a
    félúton túl vesszőnél/gondolatjelnél és hosszabb szünetnél is vág. (UI-próba 10-18: a Whisper-„mondatok” 10–18 s-osak
    és 120–180 betűsek voltak — feliratnak, .srt-nek olvashatatlan blokk.) Szóidők nélkül a mondatok maradnak."""
    words = tr.get("words") or []
    if not words:
        return [dict(s) for s in tr.get("sentences") or []]
    out: list[dict[str, Any]] = []
    cur: list[dict[str, Any]] = []

    def flush() -> None:
        if cur:
            out.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": _join(cur)})
            cur.clear()

    for w in words:
        if cur:
            joined = _join(cur)
            soft = len(joined) >= max_chars * 0.55 and re.search(r"[,;:–—]$", cur[-1]["w"])
            hard = (len(joined) + 1 + len(w["w"]) > max_chars or w["end"] - cur[0]["start"] > max_dur
                    or w["start"] - cur[-1]["end"] > gap)
            if soft or hard:
                flush()
        cur.append(w)
        if re.search(r"[.!?…][\"”»)]*$", w["w"]):
            flush()
    flush()
    for a, b in zip(out, out[1:]):              # a rövid felirat kicsit tovább marad, de a következőbe nem lóg bele
        a["end"] = round(min(b["start"], max(a["end"], a["start"] + 1.0)), 2)
    return out
