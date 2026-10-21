"""Időbélyeges átirat egy felvételhez (beszéd → szavak időbélyeggel), és egy elhangzó mondat megkeresése benne.

Mire: a saját felvételre tett grafikai réteg mondathoz köthető („amikor elhangzik, hogy az ügyfél fellebbez, jelenjen
meg a nyíl”) — ne másodpercre kelljen megadni (a ChatGPT-értékelés 10-17). Plusz feliratfájl (SRT) a felvételhez.

Motor: faster-whisper (MIT), Whisper large-v3-turbo, CPU-n int8 — helyben, kulcs és díj nélkül. Mérve 10-17
(magyar jogi mondatok, 14 s, 8 szál): large-v3-turbo 21,5 s, a mondatok jók („a másodfokú bíróság 6 hónapon belül
dönt”); a small 7,5 s, de „ügyférfellerbez”, „belődönt” — ezért a turbo. A kiadott FilmCraft CLI Whisper nélkül
készült (a `whisper` feature hiányzik), ezért nem az. A modell első használatkor töltődik le (~1,6 GB, a kötetre).
AMIT EGYSZER ÁTÍRTUNK, NEM ÍRJUK ÁT ÚJRA: a hang SHA256-ja szerint az ocr_cache táblában (Postgres) marad.

Magyar átirat (Omlett 3.5, a 10-19-i félrehallások: „sonkával” → „sokkával”, „szép” → „kép”, „Voilà” → „Ala”):
    prompt      a felvétel témája / stílusa → Whisper initial_prompt
    vocabulary  a várható szavak (nevek, ételek, helyek) → Whisper hotwords; és utána a javítás alapja
    model       large-v3-turbo (alap, gyors) | large-v3 (lassabb, pontosabb)
    force       a gyorsítótár megkerülése (újrahallgatás; az új eredmény felülírja a régit)
    correct     None: a szókincs szerinti (determinisztikus) javítás, ha van vocabulary · False: semmi javítás ·
                True: utána a Studio AI is átnézi (AI-lépés: billing.ai_step a _call_model-en át, MCP-n kredites)
Sorrend (a tulajdonos elve): adat → determinisztikus → AI. A gyorsítótárban a NYERS átirat van (kulcs: hang + nyelv +
prompt/szókincs + modell); a javítások listája (corrections: heard → fixed, idő, ki javította) mindig látszik.
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
MODELS = ("large-v3-turbo", "large-v3")   # választható modellek (az env-beli MODEL az alap)
_MODEL_ALIASES = {"turbo": "large-v3-turbo", "large-v3turbo": "large-v3-turbo", "v3": "large-v3", "large": "large-v3"}
VER = "asr-v1"
FIX_VER = "asr-fix-v1"            # az AI-javítás gyorsítótár-kulcsának verziója (content_checks)
_model = None
_model_name: Optional[str] = None
_lock = asyncio.Lock()            # egyszerre egy átírás (CPU-igényes; a többi sorban vár)

# a video tool action="schema" referenciájába (a video sáv köti be): a transcript paraméterei
SCHEMA_REF = {"transcript": (
    'video(action="transcript", file_id, language?, prompt?, vocabulary?, model?, force?, correct?) → a job; its result: '
    "sentences [{at, until, text}], subtitles, an .srt file_id, corrections [{at, heard, fixed, by}], check. "
    "prompt: a sentence about the recording (topic, names, style) — Whisper's initial prompt. "
    'vocabulary: the words you expect ("sonkával, fakanállal, Voilà" or a list) — passed to Whisper as hotwords, then '
    "close mishearings of them are fixed without AI (same first letter, similar sound, not just another form of the word). "
    f"model: {' | '.join(MODELS)} (default {MODEL}; large-v3 is slower and a little more accurate). "
    "force: true = listen again instead of the cached transcript. "
    "correct: true = the Studio AI proofreads what is still misheard (a Studio AI step: monthly allowance, then credits); "
    "false = no corrections at all; left out = only the vocabulary fixes.")}


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


def model_name(model: Optional[str] = None) -> str:
    """A kért modell (None → az alap); ismeretlennél érthető hiba a választékkal."""
    m = str(model or "").strip().lower()
    if not m:
        return MODEL
    m = _MODEL_ALIASES.get(m, m)
    if m in MODELS or m == MODEL:
        return m
    raise ValueError(f"model: {model!r} — choose {' or '.join(MODELS)} (default {MODEL}).")


def vocab_list(vocabulary: Any) -> list[str]:
    """A várható szavak listája: lista, vagy vesszővel / pontosvesszővel / sortöréssel elválasztott szöveg."""
    if not vocabulary:
        return []
    items = vocabulary if isinstance(vocabulary, (list, tuple)) else re.split(r"[,;\n]+", str(vocabulary))
    out: list[str] = []
    for it in items:
        t = " ".join(str(it or "").split())[:60]
        if t and t not in out:
            out.append(t)
    return out[:100]


def _load(name: Optional[str] = None):
    """A modell (egyszerre egy van a memóriában: másik modell kérésekor az előzőt elengedjük — int8-ban ~1,5 GB)."""
    global _model, _model_name
    name = name or MODEL
    if _model is None or _model_name != name:
        from faster_whisper import WhisperModel
        _model = None
        threads = max(2, min(16, (os.cpu_count() or 4) // 2))
        _model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads, download_root=str(_models_dir()))
        _model_name = name
    return _model


async def _pcm16k(src: Path) -> bytes:
    """A felvétel hangja 16 kHz mono 16 bites nyers PCM-ként (ffmpeg; a PyAV-verziótól független)."""
    proc = await asyncio.create_subprocess_exec("ffmpeg", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                                                "-f", "s16le", "-", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise ValueError(f"No sound could be read from this file ({err.decode('utf-8', 'replace').strip()[-160:]}).")
    return out


def _run(pcm: bytes, language: Optional[str], prompt: Optional[str] = None, hotwords: Optional[str] = None,
         model: Optional[str] = None) -> dict[str, Any]:
    import numpy as np
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    name = model or MODEL
    segs, info = _load(name).transcribe(audio, language=language or None, word_timestamps=True, vad_filter=True,
                                        initial_prompt=prompt or None, hotwords=hotwords or None)
    words, sentences = [], []
    for s in segs:
        sentences.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()})
        for w in s.words or []:
            if w.word.strip():
                words.append({"w": w.word.strip(), "start": round(w.start, 2), "end": round(w.end, 2)})
    return {"language": info.language, "model": name, "duration": round(len(audio) / 16000, 2),
            "sentences": sentences, "words": words}


def cache_ver(language: Optional[str] = None, prompt: Optional[str] = None, vocabulary: Optional[list[str]] = None) -> str:
    """A gyorsítótár „verzió”-kulcsa: nyelv + (ha van) a prompt és a szókincs lenyomata. Paraméterek nélkül a régi kulcs
    (a már átírt felvételek ugyanúgy találatok)."""
    ver = VER + (f":{language}" if language else "")
    if prompt or vocabulary:
        h = hashlib.sha256(json.dumps([prompt or "", vocabulary or []], ensure_ascii=False).encode()).hexdigest()[:16]
        ver += f":h{h}"
    return ver


async def transcribe(src: Path, language: Optional[str] = None, *, prompt: Optional[str] = None, vocabulary: Any = None,
                     model: Optional[str] = None, force: bool = False, correct: Optional[bool] = None) -> dict[str, Any]:
    """→ {language, model, duration, sentences [{start, end, text}], words [{w, start, end}]} — gyorsítótárból, ha volt
    (force: újrahallgatás). vocabulary / correct esetén a javítás is: + corrections [{at, heard, fixed, by}] és, ha a
    Studio AI dolgozott, studio_ai {steps, credits}."""
    from app import db
    name = model_name(model)
    vocab = vocab_list(vocabulary)
    prompt = " ".join(str(prompt or "").split())[:600] or None
    if not available():
        raise RuntimeError("Speech recognition isn't installed on this server (faster-whisper).")
    pcm = await _pcm16k(src)
    if len(pcm) < 16000 * 2 * 0.3:
        return {"language": None, "model": name, "duration": round(len(pcm) / 32000, 2), "sentences": [], "words": []}
    sha = hashlib.sha256(pcm).hexdigest()
    ver = cache_ver(language, prompt, vocab)
    hit = None
    if not force:
        try:
            hit = await db.ocr_cache_get(sha, ver, name)     # a közös tartós gyorsítótár (sha256, verzió, modell → szöveg)
        except Exception:
            hit = None
    if hit:
        out = json.loads(hit)
    else:
        async with _lock:
            out = await asyncio.to_thread(_run, pcm, language, prompt, ", ".join(vocab) or None, name)
        try:
            await db.ocr_cache_put(sha, ver, name, json.dumps(out, ensure_ascii=False), replace=force)
        except Exception:
            log.warning("átirat-gyorsítótár írása nem sikerült", exc_info=True)
    if correct is False or (not vocab and not correct):
        return out
    return await fix(out, vocab, prompt=prompt, use_ai=bool(correct), force=force)


# ---------- javítás: adat (szókincs) → determinisztikus → AI ----------

_PUNCT_HEAD = re.compile(r"^[^\w]+")
_PUNCT_TAIL = re.compile(r"[^\w]+$")


def _stem(a: str, b: str) -> bool:
    """Az egyik a másik eleje vagy vége (legalább 4 betűvel): ugyanannak a szónak másik alakja / összetétele, vagy egy
    kifejezés darabja („ajánlom” az „azt ajánlom”-ban) — nem félrehallás."""
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 4 and (long_.startswith(short) or long_.endswith(short))


def _close(parts: list[str], want: str) -> Optional[float]:
    """A hallott szó(k) (normalizálva) a várt szó félrehallása-e? → hasonlóság (egybeírt alakban), vagy None.
    Óvatos: legalább 5 betű, azonos kezdőbetű, nem ragozott alak / összetétel („fakanál” a „fakanállal” mellett nem hiba,
    és a „fakanál jó” sem „fakanállal”); rövid szónál (< 8 betű) szigorúbb küszöb. A rövid szavakat („kép” / „szép”) és a
    nagyon eltérő hallást („Ala” / „Voilà”) az AI-javítás kapja (correct=true)."""
    heard = "".join(parts)
    if len(want) < 5 or not heard or heard == want or heard[0] != want[0] or _stem(heard, want):
        return None
    if len(parts) > 1 and any(x == want or _stem(x, want) for x in ("".join(parts[:-1]), "".join(parts[1:]))):
        return None
    r = difflib.SequenceMatcher(None, heard, want, autojunk=False).ratio()
    return r if r >= (0.75 if len(want) >= 8 else 0.8) else None


def _sentence_of(tr: dict[str, Any], t: float) -> Optional[dict[str, Any]]:
    return next((s for s in tr.get("sentences") or [] if s["start"] - 0.05 <= t <= s["end"] + 0.05), None)


def _replace(tr: dict[str, Any], i: int, size: int, fixed: str, by: str) -> dict[str, Any]:
    """A words[i:i+size] helyére a javított szó(k): az írásjel a hallottról marad, az idő a betűk arányában oszlik el,
    a mondat szövege is javul. → a javítás bejegyzése."""
    words = tr["words"]
    chunk = words[i:i + size]
    heard = " ".join(w["w"] for w in chunk)
    toks = [_PUNCT_TAIL.sub("", _PUNCT_HEAD.sub("", t)) or t for t in fixed.split()]
    if chunk[0]["w"][:1].isupper() and toks[0][:1].islower():
        toks[0] = toks[0][:1].upper() + toks[0][1:]
    head = (_PUNCT_HEAD.search(chunk[0]["w"]) or [""])[0]
    tail = (_PUNCT_TAIL.search(chunk[-1]["w"]) or [""])[0]
    toks[0], toks[-1] = head + toks[0], toks[-1] + tail
    t0, t1 = chunk[0]["start"], chunk[-1]["end"]
    total = sum(len(t) for t in toks) or 1
    new, at = [], t0
    for k, t in enumerate(toks):
        end = t1 if k == len(toks) - 1 else at + (t1 - t0) * len(t) / total
        new.append({"w": t, "start": round(at, 2), "end": round(end, 2)})
        at = end
    words[i:i + size] = new
    s = _sentence_of(tr, t0)
    if s is not None:
        fixed_txt = " ".join(t["w"] for t in new)
        if heard in s["text"]:
            s["text"] = s["text"].replace(heard, fixed_txt, 1)
        else:                         # más a szóköz/írásjel: a mondat a szavaiból
            s["text"] = _join([w for w in words if s["start"] - 0.05 <= w["start"] <= s["end"] + 0.05])
    return {"at": round(t0, 2), "heard": heard, "fixed": " ".join(t["w"] for t in new), "by": by}


def fix_with_vocabulary(tr: dict[str, Any], vocab: list[str]) -> list[dict[str, Any]]:
    """Determinisztikus javítás a várt szavakból (AI nélkül, ingyen): minden szókincs-elemre a hozzá hasonló hosszú
    szóablakok (n−1…n+1 szó) közül a legjobb, egymást nem átfedő találatok cserélődnek. Helyben módosít; → a javítások."""
    words = tr.get("words") or []
    if not vocab or not words:
        return []
    norm = [_norm(w["w"]) for w in words]
    known = {_norm(v.replace(" ", "")) for v in vocab}
    taken: set[int] = set()            # a szókincs pontosan hallott előfordulásai: érintetlenek (10-19, valódi mérés:
    for v in vocab:                    # a jól hallott „azt ajánlom” darabjából „azt azt ajánlom” lett)
        want, n = "".join(_norm(t) for t in v.split()), len(v.split())
        for i in range(0, len(words) - n + 1):
            if "".join(norm[i:i + n]) == want:
                taken |= set(range(i, i + n))
    cands = []
    for v in vocab:
        want = "".join(_norm(t) for t in v.split())
        n = len(v.split())
        for size in sorted({max(1, n - 1), n, n + 1}):
            for i in range(0, len(words) - size + 1):
                if not norm[i] or norm[i][0] != (want[:1] or "?"):
                    continue
                if "".join(norm[i:i + size]) in known:
                    continue
                r = _close(norm[i:i + size], want)
                if r is not None:
                    cands.append((r, i, size, v))
    chosen = []
    for r, i, size, v in sorted(cands, key=lambda c: (-c[0], c[1])):
        span = set(range(i, i + size))
        if not span & taken:
            taken |= span
            chosen.append((i, size, v, r))
    out = []
    for i, size, v, r in sorted(chosen, key=lambda c: -c[0]):        # hátulról: az indexek nem csúsznak el
        out.append({**_replace(tr, i, size, v, "vocabulary"), "score": round(r, 2)})
    return sorted(out, key=lambda c: c["at"])


_FIX_SYSTEM = """You correct speech-recognition mistakes in a transcript. You get the sentences as the machine heard them
(numbered), the words the speaker is expected to use (vocabulary) and a note about the recording.
Fix ONLY words that were clearly misheard: a sound-alike that makes no sense here, especially where a vocabulary word
(in any grammatical form) or a well-known phrase fits. Keep everything else exactly as it is: never rephrase, shorten,
translate, add or reorder words, never fix grammar or style, keep the language.
Reply with ONE JSON object: {"fixes": [{"n": sentence number, "heard": "the misheard words, copied EXACTLY from that
sentence", "fixed": "what was really said"}]}. Nothing to fix → {"fixes": []}."""


async def _ai_fixes(batch: list[tuple[int, str]], vocab: list[str], prompt: Optional[str], force: bool) -> list[dict[str, Any]]:
    """Egy köteg mondat a Studio AI-nak (egy AI-lépés: billing.ai_step a _call_model-ben) → [{n, heard, fixed}].
    AMIT EGYSZER MEGCSINÁLTUNK, NE ÚJRA: ugyanarra a szövegre + szókincsre a döntés a content_checks táblából jön."""
    from app import db
    from app.llm.siliconflow import _call_model, _strip_json_fences
    key = hashlib.sha256(json.dumps([batch, vocab, prompt or ""], ensure_ascii=False).encode()).hexdigest()
    if not force:
        try:
            hit = await db.check_get("asr_fix", key, FIX_VER)
        except Exception:
            hit = None
        if hit is not None:
            return list(hit.get("fixes") or []) if isinstance(hit, dict) else []
    user = ("Recording: " + (prompt or "(no note)") + "\nVocabulary: " + (", ".join(vocab) or "(none)")
            + "\n\nSentences:\n" + "\n".join(f"{n}. {t}" for n, t in batch))
    last: Exception = RuntimeError("no model")
    for m in (settings.LLM_MODEL_PRIMARY, settings.LLM_MODEL_FALLBACK):
        try:
            raw = await _call_model(m, [{"role": "system", "content": _FIX_SYSTEM}, {"role": "user", "content": user}],
                                    thinking=False)
            data = json.loads(_strip_json_fences(raw))
            break
        except Exception as e:
            from app import billing
            if isinstance(e, billing.AIStepsExhausted):
                raise
            last = e
            log.warning("átirat-javítás %s modellel nem sikerült: %s", m, e)
    else:
        raise last
    fixes = [f for f in (data.get("fixes") or []) if isinstance(f, dict)] if isinstance(data, dict) else []
    try:
        await db.check_put("asr_fix", key, FIX_VER, {"fixes": fixes})
    except Exception:
        log.warning("átirat-javítás gyorsítótár írása nem sikerült", exc_info=True)
    return fixes


def _apply_ai_fix(tr: dict[str, Any], f: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Egy AI-javítás, ha a hallott rész SZÓ SZERINT ott van a mondatban (különben kimarad — nem írunk át mást)."""
    sents = tr.get("sentences") or []
    try:
        s = sents[int(f.get("n")) - 1]
    except (TypeError, ValueError, IndexError):
        return None
    heard, fixed = " ".join(str(f.get("heard") or "").split()), " ".join(str(f.get("fixed") or "").split())
    target = [_norm(t) for t in heard.split() if _norm(t)]
    if not target or not fixed or len(target) > 8 or len(fixed.split()) > 2 * len(target) + 2 \
            or "".join(target) == "".join(_norm(t) for t in fixed.split()):
        return None
    words = tr.get("words") or []
    idx = [k for k, w in enumerate(words) if s["start"] - 0.05 <= w["start"] <= s["end"] + 0.05]
    n = len(target)
    for a in range(len(idx) - n + 1):
        i = idx[a]
        if [_norm(w["w"]) for w in words[i:i + n]] == target:
            return _replace(tr, i, n, fixed, "ai")
    if not words and heard in s["text"]:             # szóidők nélkül: csak a mondat szövege
        s["text"] = s["text"].replace(heard, fixed, 1)
        return {"at": s["start"], "heard": heard, "fixed": fixed, "by": "ai"}
    return None


async def fix(tr: dict[str, Any], vocab: list[str], *, prompt: Optional[str] = None, use_ai: bool = False,
              force: bool = False) -> dict[str, Any]:
    """Javítás a NYERS átirat másolatán: előbb a szókincsből (determinisztikus), aztán — ha kérték — a Studio AI a
    maradékra (kötegenként egy AI-lépés). → az átirat + corrections (+ studio_ai, ha az AI dolgozott)."""
    from app import billing
    out = json.loads(json.dumps(tr))
    corrections = fix_with_vocabulary(out, vocab)
    notes: list[str] = []
    if use_ai and out.get("sentences"):
        batches, cur, size = [], [], 0
        for n, s in enumerate(out["sentences"], 1):
            if cur and size + len(s["text"]) > 6000:
                batches.append(cur)
                cur, size = [], 0
            cur.append((n, s["text"]))
            size += len(s["text"])
        if cur:
            batches.append(cur)
        outer = billing._steps.get()                 # a hívó (MCP-burkoló) számlálója is lássa
        with billing.count_steps() as used:
            try:
                for b in batches:
                    for f in await _ai_fixes(b, vocab, prompt, force):
                        c = _apply_ai_fix(out, f)
                        if c:
                            corrections.append(c)
            except billing.AIStepsExhausted as e:
                notes.append(f"The AI proofreading didn't run: {e}")
            except Exception as e:
                from app.llm.siliconflow import LLMServiceError
                log.warning("átirat AI-javítás kimaradt: %s", e)
                why = str(e) if isinstance(e, LLMServiceError) else "the AI service isn't available right now"
                notes.append(f"The AI proofreading didn't run ({why}); the transcript is as heard"
                             + (" with the vocabulary fixes." if corrections else "."))
        if used[0]:
            out["studio_ai"] = {"steps": used[0], "credits": used[1]}
            if outer is not None:
                outer[0] += used[0]
                outer[1] += used[1]
    out["corrections"] = sorted(corrections, key=lambda c: c["at"])
    if notes:
        out["correction_notes"] = notes
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


def _glued(out: str, t: str) -> bool:
    """A token az előzőhöz tapad-e (szóköz nélkül): kötőjellel / írásjellel kezdődik, vagy nyitó zárójel / idézőjel után jön."""
    return bool(out) and (bool(re.match(r"[-‐–,.;:!?…)\]”»]", t)) or out.endswith(("(", "„", "«")))


def _join(words: list[dict[str, Any]]) -> str:
    """Szavak szöveggé: a kötőjellel/írásjellel kezdődő token az előzőhöz tapad („ilyen-olyan”, nem „ilyen -olyan”)."""
    out = ""
    for w in words:
        t = w["w"]
        out += t if not out or _glued(out, t) else " " + t
    return out


def _token_starts(words: list[dict[str, Any]]) -> list[float]:
    """A _join szövegének szóközzel elválasztott tokenjei mikor hangzanak el (a tapadó írásjel nem új token) — a
    szavankénti feliratnak (karaoke, kiemelés)."""
    out, starts = "", []
    for w in words:
        if not out or not _glued(out, w["w"]):
            starts.append(round(float(w["start"]), 2))
        out += w["w"] if not out or _glued(out, w["w"]) else " " + w["w"]
    return starts


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
            out.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": _join(cur), "words": _token_starts(cur)})
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
        # mondatvég — de a sorszámnév pontja („a XV. században”, „3. helyen”) nem az (10-18: „XV.” / „században” kettévágva)
        if re.search(r"[.!?…][\"”»)]*$", w["w"]) and not re.fullmatch(r"(?:\d+|[IVXLCDM]+)\.", w["w"]):
            flush()
    flush()
    for a, b in zip(out, out[1:]):              # a rövid felirat kicsit tovább marad, de a következőbe nem lóg bele
        a["end"] = round(min(b["start"], max(a["end"], a["start"] + 1.0)), 2)
    return out
