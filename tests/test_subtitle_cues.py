"""Feliratsorok a szóidőkből: legfeljebb két sor és ~6,5 s, mondatvégen és vesszőnél vág, kötőjelet nem bont szét.

UI-próba (10-18): egy 65 s-os iPhone-felvétel Whisper-„mondatai” 10–18 s-osak és 120–180 betűsek voltak — a webes
feliratozó és az .srt ezeket kapta egy-egy olvashatatlan blokként; „ilyen -olyan” szóközzel.
"""
from __future__ import annotations

from app.tools.transcript import cues


def _words(text: str, t0: float = 0.0, step: float = 0.35):
    out, t = [], t0
    for w in text.split():
        out.append({"w": w, "start": round(t, 2), "end": round(t + step * 0.9, 2)})
        t += step
    return out


def test_long_sentence_becomes_readable_lines():
    long = ("Szeretnék ide valahova majd magam fölé egy infografikát, ami bemutatja, hogy tizenöt és tizennyolc "
            "másodperc között hihetetlen fontos gondolatok vannak ráadásul, és még ennél is több minden.")
    cs = cues({"words": _words("Jó reggelt kívánok! " + long), "sentences": []})
    assert cs[0]["text"] == "Jó reggelt kívánok!"
    assert all(len(c["text"]) <= 84 and c["end"] - c["start"] <= 6.6 for c in cs)
    assert " ".join(c["text"] for c in cs) == "Jó reggelt kívánok! " + long
    assert any(c["text"].endswith("infografikát,") for c in cs)          # vesszőnél vág, ha már félúton túl


def test_hyphen_tokens_and_pause():
    ws = _words("egyéb ilyen -olyan ábrák")
    ws += [{"w": "Utána", "start": ws[-1]["end"] + 2.0, "end": ws[-1]["end"] + 2.4}]   # hosszú szünet → új sor
    cs = cues({"words": ws})
    assert cs[0]["text"] == "egyéb ilyen-olyan ábrák" and cs[1]["text"] == "Utána"


def test_without_word_times_sentences_stay():
    s = [{"start": 0, "end": 3, "text": "Egy mondat."}]
    assert cues({"words": [], "sentences": s}) == s


def test_ordinal_dot_is_not_a_sentence_end():
    """10-18: „a XV. században” a római sorszámnév pontjánál kettévágódott."""
    cs = cues({"words": _words("és a XV. században itt ekkora volt, a 3. helyen pedig.")})
    assert len(cs) == 1 and "XV. században" in cs[0]["text"] and "3. helyen" in cs[0]["text"]
