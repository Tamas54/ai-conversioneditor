"""AI-narráció a videókhoz: offline, ingyenes Piper-hangok (58 nyelv, magyarul is), új szolgáltató nélkül.

A videókészítő a jelenetek felolvasandó szövegét (caption) mondatja fel; a jelenet hossza a narrációhoz igazodik,
a hang a jelenet elején indul, a zene alatta halkabban szól. A hangmodell (~63 MB) első használatkor töltődik le a
Hugging Face-ről (rhasspy/piper-voices) a DATA_DIR/models/piper alá, utána gépen fut (CPU, ~10× valós idő).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import unicodedata
import wave
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("aice.narration")

BASE_URL = os.getenv("PIPER_BASE_URL", "https://huggingface.co/rhasspy/piper-voices/resolve/main").rstrip("/")
QUALITY = {"medium": 0, "high": 1, "low": 2, "x_low": 3}

# nyelv → (alapértelmezett, női, férfi) — kézzel válogatott, jól érthető hangok; a többi nyelvhez a voices.json-ből
VOICES: dict[str, tuple[str, Optional[str], Optional[str]]] = {
    "hu": ("hu_HU-anna-medium", "hu_HU-anna-medium", "hu_HU-imre-medium"),
    "en": ("en_US-lessac-medium", "en_US-lessac-medium", "en_US-ryan-medium"),
    "de": ("de_DE-thorsten-medium", "de_DE-kerstin-low", "de_DE-thorsten-medium"),
    "fr": ("fr_FR-siwis-medium", "fr_FR-siwis-medium", "fr_FR-tom-medium"),
    "es": ("es_ES-davefx-medium", "es_AR-daniela-high", "es_ES-davefx-medium"),
    "it": ("it_IT-paola-medium", "it_IT-paola-medium", "it_IT-riccardo-x_low"),
    "pt": ("pt_BR-faber-medium", None, "pt_BR-faber-medium"),
    "nl": ("nl_BE-nathalie-medium", "nl_BE-nathalie-medium", "nl_NL-pim-medium"),
    "pl": ("pl_PL-gosia-medium", "pl_PL-gosia-medium", "pl_PL-darkman-medium"),
    "cs": ("cs_CZ-jirka-medium", None, "cs_CZ-jirka-medium"),
    "sk": ("sk_SK-lili-medium", "sk_SK-lili-medium", None),
    "ro": ("ro_RO-mihai-medium", None, "ro_RO-mihai-medium"),
    "ru": ("ru_RU-irina-medium", "ru_RU-irina-medium", "ru_RU-dmitri-medium"),
    "uk": ("uk_UA-tetiana-high", "uk_UA-tetiana-high", "uk_UA-mykyta-high"),
}

# latin betűs nyelvek leggyakoribb szavai (a nyelv felismeréséhez — a videó szövege rövid, ez elég)
_STOP = {
    "hu": "a az és hogy nem is egy van meg már csak de ez azt mint vagy volt lesz kell ami aki el sem pedig",
    "en": "the and of to in is that for it with as was on be are this by not or from at an have has",
    "de": "der die und das ist nicht zu den von mit sich des auf für ein eine dem im auch es als wird",
    "fr": "le la les et des est un une du que pour dans qui en au pas sur ce avec par plus sont",
    "es": "el la los las de que y en un una es por con para no se del al lo como más pero",
    "it": "il la di che e un una è per non in con del della sono le si da al dei come",
    "pt": "o a os as de que e do da em um uma é para com não por mais se no na dos",
    "nl": "de het een en van in is dat op te zijn met voor niet aan er ook als door",
    "pl": "i w nie na się z do że to jest jak o po ale od za co tak przez",
    "cs": "a v se na je to že s z do o jako by k ale pro jsou tak už",
    "sk": "a v sa na je to že s z do o ako by k ale pre sú tak už som",
    "ro": "și de la în a cu pe un o este nu că se din care pentru mai sunt",
    "sv": "och att det som en på är av för med till den har inte om ett",
    "da": "og at det er en til på af med for som den ikke de har et",
    "fi": "ja on ei se että oli ovat kun mutta myös tai kuin hän tämä",
    "tr": "ve bir bu da de için ile ne çok olarak daha gibi ama sonra",
}
_STOPSETS = {k: set(v.split()) for k, v in _STOP.items()}
_SPECIAL = {"hu": "őűŐŰ", "ro": "ășțĂȘȚ", "pl": "łńśźżŁŃŚŹŻ", "cs": "řůěŘŮĚ", "sk": "ľĺŕôĽĹŔ", "de": "ßäÄ",
            "tr": "ğışĞİŞ", "pt": "ãõÃÕ", "es": "ñ¿¡Ñ", "fr": "œçèêÇÈÊ", "da": "æøÆØ", "sv": "åÅ"}

_lock = threading.Lock()
_loaded: dict[str, Any] = {}


def available() -> bool:
    try:
        import piper  # noqa: F401
        return True
    except Exception:
        return False


def models_dir() -> Path:
    from app.config import settings
    d = Path(os.getenv("PIPER_VOICES_DIR") or settings.DATA_DIR / "models" / "piper")
    d.mkdir(parents=True, exist_ok=True)
    return d


def detect(text: str) -> str:
    """Kétbetűs nyelvkód a szövegből: írásrendszer, majd a gyakori szavak (latin betűs nyelveknél)."""
    t = text or ""
    scripts = {"CYRILLIC": 0, "GREEK": 0, "ARABIC": 0, "HEBREW": 0, "HIRAGANA": 0, "KATAKANA": 0, "HANGUL": 0,
               "CJK": 0, "DEVANAGARI": 0, "THAI": 0, "GEORGIAN": 0, "ARMENIAN": 0}
    for ch in t[:4000]:
        if ch.isalpha() and ord(ch) > 0x24F:
            name = unicodedata.name(ch, "")
            for s in scripts:
                if name.startswith(s):
                    scripts[s] += 1
                    break
    top = max(scripts, key=scripts.get)
    if scripts[top] > len(t) * 0.15:
        if top == "CYRILLIC":
            return "uk" if re.search(r"[іїєґІЇЄҐ]", t) else "bg" if re.search(r"\bи\b.*\bна\b.*\bсе\b", t) else "ru"
        return {"GREEK": "el", "ARABIC": "ar", "HEBREW": "he", "HIRAGANA": "ja", "KATAKANA": "ja", "HANGUL": "ko",
                "CJK": "zh", "DEVANAGARI": "hi", "THAI": "th", "GEORGIAN": "ka", "ARMENIAN": "hy"}[top]
    words = re.findall(r"[^\W\d_]+", t.lower())
    score = {k: sum(1 for w in words if w in s) for k, s in _STOPSETS.items()}
    for k, chars in _SPECIAL.items():
        score[k] = score.get(k, 0) + 2 * sum(t.count(c) for c in chars)
    best = max(score, key=score.get)
    return best if score[best] > 0 else "en"


_NAMES = {"hungarian": "hu", "magyar": "hu", "english": "en", "angol": "en", "german": "de", "deutsch": "de",
          "német": "de", "french": "fr", "français": "fr", "francia": "fr", "spanish": "es", "español": "es",
          "spanyol": "es", "italian": "it", "italiano": "it", "olasz": "it", "portuguese": "pt", "português": "pt",
          "dutch": "nl", "polish": "pl", "polski": "pl", "lengyel": "pl", "czech": "cs", "čeština": "cs", "cseh": "cs",
          "slovak": "sk", "szlovák": "sk", "romanian": "ro", "română": "ro", "román": "ro", "russian": "ru", "orosz": "ru",
          "ukrainian": "uk", "ukrán": "uk", "swedish": "sv", "danish": "da", "finnish": "fi", "turkish": "tr",
          "greek": "el", "croatian": "hr", "serbian": "sr", "slovenian": "sl", "bulgarian": "bg"}


def lang_code(language: Optional[str]) -> Optional[str]:
    """„Hungarian” / „magyar” / „hu” / „hu-HU” → „hu”; ismeretlen → None (akkor a szövegből)."""
    s = (language or "").strip().lower()
    if not s or s == "auto":
        return None
    if re.fullmatch(r"[a-z]{2,3}([-_][a-z]{2})?", s):
        return s[:2]
    return _NAMES.get(s)


def _catalog() -> dict[str, Any]:
    p = models_dir() / "voices.json"
    if not p.exists():
        _download("voices.json", p)
    return json.loads(p.read_text("utf-8"))


def _download(rel: str, dst: Path) -> None:
    import httpx
    tmp = dst.with_suffix(dst.suffix + ".part")
    with httpx.stream("GET", f"{BASE_URL}/{rel}", follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)
    os.replace(tmp, dst)


def pick(language: Optional[str], want: Optional[str] = None) -> str:
    """Hangmodell kulcsa: want = "female" | "male" | egy Piper-hang (pl. hu_HU-imre-medium) | None."""
    cat = _catalog()
    if want and want in cat:
        return want
    lang = (language or "en").lower().replace("-", "_").split("_")[0][:3]
    pref = VOICES.get(lang)
    if pref:
        key = {"female": pref[1], "male": pref[2]}.get((want or "").lower()) or pref[0]
        if key in cat:
            return key
    cands = [(QUALITY.get(v.get("quality"), 9), v.get("num_speakers", 1) > 1, k) for k, v in cat.items()
             if v.get("language", {}).get("family") == lang]
    if not cands:
        raise ValueError(f"No AI voice for language '{lang}'.")
    return sorted(cands)[0][2]


def _model(key: str) -> Path:
    cat = _catalog()
    files = [f for f in cat[key]["files"] if f.endswith((".onnx", ".onnx.json"))]
    out = models_dir()
    for f in files:
        dst = out / Path(f).name
        if not dst.exists():
            _download(f, dst)
    return out / f"{key}.onnx"


def _voice(key: str):
    from piper import PiperVoice
    with _lock:
        v = _loaded.get(key)
        if v is None:
            v = PiperVoice.load(str(_model(key)))
            if len(_loaded) >= 3:
                _loaded.pop(next(iter(_loaded)))
            _loaded[key] = v
        return v


# szünet a felolvasásban: [pause: 1.5s] · [pause 2] · [pause: 500ms] · [szünet: 1 mp] — a hang ennyit hallgat (≤ 10 s),
# a képernyőn nem látszik (strip_pauses). Az ötlet a MoneyPrinterTurbo-ból (10-22).
PAUSE = re.compile(r"\[\s*(?:pause|break|sz[üu]net|pausa)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*(ms|s|sec|secs|seconds?|mp)?\s*\]",
                   re.IGNORECASE)
PAUSE_MAX = 10.0


def pauses(text: str) -> list[tuple[str, float]]:
    """A szöveg darabjai a szünetek mentén: [(kimondandó szöveg, utána ennyi mp csend)]."""
    out, at = [], 0
    for m in PAUSE.finditer(text or ""):
        v = float(m.group(1).replace(",", "."))
        secs = v / 1000 if (m.group(2) or "").lower() == "ms" else v
        out.append((text[at:m.start()], min(PAUSE_MAX, max(0.0, secs))))
        at = m.end()
    out.append(((text or "")[at:], 0.0))
    return out


def strip_pauses(text: str) -> str:
    """A felirat / a képernyő szövege a szünetjelek nélkül."""
    return re.sub(r"\s{2,}", " ", PAUSE.sub(" ", text or "")).strip() if text and "[" in text else (text or "")


def clean(text: str) -> str:
    """Felolvasható szöveg: linkek, jelölések, emojik nélkül."""
    t = re.sub(r"https?://\S+", "", text or "")
    t = re.sub(r"[*_#`>|~\[\]{}]", " ", t)
    t = "".join(ch for ch in t if unicodedata.category(ch)[0] != "S" or ch in "%$€£+=°")
    return re.sub(r"\s+", " ", t).strip()


def synth(text: str, key: str, dst: Path, speed: float = 1.0) -> float:
    """Egy szöveg → WAV (a hang saját mintavételezésével), a [pause: Xs] jeleknél csenddel. → hossz másodpercben (0, ha
    nincs mit mondani)."""
    from piper import SynthesisConfig
    parts = [(clean(t), gap) for t, gap in pauses(text)]
    if not any(t for t, _ in parts):
        return 0.0
    v = _voice(key)
    cfg = SynthesisConfig(length_scale=1.0 / max(0.5, min(2.0, speed)))
    if len(parts) == 1:
        with wave.open(str(dst), "wb") as wf:
            v.synthesize_wav(parts[0][0], wf, syn_config=cfg)
    else:
        while parts and not parts[-1][0]:             # a végén álló szünet nem nyújtja a hangot (az avatar ára is másodperc)
            parts.pop()
        parts[-1] = (parts[-1][0], 0.0)
        params, chunks = None, []                      # bájtok, illetve a csend másodpercei
        for i, (t, gap) in enumerate(parts):
            if t:
                part = dst.with_name(f"{dst.stem}.p{i}.wav")
                try:
                    with wave.open(str(part), "wb") as wf:
                        v.synthesize_wav(t, wf, syn_config=cfg)
                    with wave.open(str(part)) as w:
                        params = params or w.getparams()
                        chunks.append(w.readframes(w.getnframes()))
                finally:
                    part.unlink(missing_ok=True)
            if gap:
                chunks.append(gap)
        frame = params.sampwidth * params.nchannels
        with wave.open(str(dst), "wb") as wf:
            wf.setparams(params)
            wf.writeframes(b"".join(c if isinstance(c, bytes) else b"\0" * int(c * params.framerate) * frame for c in chunks))
    with wave.open(str(dst)) as w:
        return w.getnframes() / float(w.getframerate() or 1)


async def speak_all(texts: list[str], key: str, work: Path, speed: float = 1.0) -> list[tuple[Optional[Path], float]]:
    """Több szöveg egymás után (egy hangmodell, egy szál) → [(wav, hossz)] — üres szövegnél (None, 0)."""
    def run() -> list[tuple[Optional[Path], float]]:
        out: list[tuple[Optional[Path], float]] = []
        for i, t in enumerate(texts):
            p = work / f"narr-{i:02d}.wav"
            secs = synth(t, key, p, speed)
            out.append((p if secs > 0 else None, secs))
        return out
    return await asyncio.to_thread(run)


async def track(segments: list[tuple[float, Path]], total: float, dst: Path) -> Path:
    """A darabok a saját kezdőidejükön (másodperc) egy sávban, a videó teljes hosszára → 48 kHz sztereó WAV."""
    from app.convert.engines import run
    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for _, p in segments:
        args += ["-i", str(p)]
    n = len(segments)
    parts = [f"[{i}:a]aresample=48000,adelay={int(max(0.0, t) * 1000)}:all=1[a{i}]" for i, (t, _) in enumerate(segments)]
    mix = "".join(f"[a{i}]" for i in range(n)) + f"amix=inputs={n}:normalize=0:dropout_transition=0,apad,atrim=0:{total:.3f}[out]"
    args += ["-filter_complex", ";".join(parts + [mix]), "-map", "[out]", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(dst)]
    await run(*args, timeout=300)
    return dst
