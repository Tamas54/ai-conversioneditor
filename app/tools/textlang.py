"""Szöveg nyelve — determinisztikusan, a leggyakoribb szavakból (nincs modell, nincs hálózat).

Mire kell: a docx alapértelmezett nyelve (w:lang). Ha az „en-US” marad egy magyar szövegen, a Word / Pages /
WordCraft angolként ellenőrzi a helyesírást → szinte minden szó piros (UI-próba 10-18). Csak biztos esetben
mond nyelvet (elég találat + egyértelmű előny); kétesnél None, és akkor semmihez nem nyúlunk.
A nyelvek = a Tesseract-csomagok (Dockerfile) — amit beolvasni tudunk, azt felismerni is.
"""
from __future__ import annotations

import re
from typing import Optional

# a leggyakoribb funkciószavak (névelő, kötőszó, névmás, segédige) — ezek ritkán közösek két nyelv között
_WORDS = {
    "en-US": "the and of to is in that it for was with as on are be this by not have from or at which you",
    "hu-HU": "a az és hogy nem is egy meg van de ez csak már még mint vagy volt azt ami ha után között kell",
    "de-DE": "der die und das ist nicht ein eine zu den von mit sich auf für dem auch es im werden wird bei",
    "fr-FR": "le la les et des est une un que pour dans pas qui sur au du avec il elle ce sont mais nous",
    "es-ES": "el la los las y de que en un una es por con para del se no al lo como más pero sus",
    "it-IT": "il di che e la le un una per non sono del della con si gli anche nel alla questo ma",
    "pt-PT": "o a os as e de que em um uma para com não do da no na por se mais como mas",
    "nl-NL": "de het een en van is dat niet op te zijn voor met die er ook aan maar bij wordt",
    "pl-PL": "i w na nie się z że do to jest jak o po tak ale od za przez dla są",
    "cs-CZ": "a v na se je že to s z do o jako ale by jsou pro jsem které také",
    "sk-SK": "a v na sa je že to s z do o ako ale by sú pre som ktoré aj alebo",
    "ro-RO": "și de la în a cu o un nu pe care este din pentru mai se sau ca",
}
_SETS = {lang: set(words.split()) for lang, words in _WORDS.items()}
# betű, amely egy nyelvet erősen jelez (a közös szavaknál — cs/sk, es/pt — dönt)
_LETTERS = {"hu-HU": "őű", "de-DE": "ß", "pl-PL": "łąęśźż", "cs-CZ": "ěřů", "sk-SK": "ľĺŕô", "ro-RO": "ășțâ",
            "pt-PT": "ãõ", "es-ES": "ñ¿¡", "fr-FR": "œç", "it-IT": ""}
_TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)


def detect(text: str, min_hits: int = 8, min_words: int = 12) -> Optional[str]:
    """BCP-47 címke (pl. "hu-HU"), vagy None, ha nem biztos. Rövid felirathoz (cím, név) min_hits=2, min_words=3:
    ott főleg a jellegzetes betűk döntenek (ő, ű, ß, ł…)."""
    words = [w.lower() for w in _TOKEN.findall(text[:20000])]
    if len(words) < min_words:
        return None
    score = {lang: sum(w in s for w in words) for lang, s in _SETS.items()}
    low = text.lower()
    for lang, letters in _LETTERS.items():
        if letters:
            score[lang] += 2 * sum(low.count(c) for c in letters)
    ranked = sorted(score.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    return best if top >= min_hits and top >= 1.5 * max(second, 1) else None
