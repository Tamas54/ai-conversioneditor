"""Forrás-súly és bizonyíték-részlet a tényellenőrzéshez (10-22, szerkesztői próba).

A tapasztalat: „supported” egy kérdés-válasz oldalból és ismeretlen blogokból is lett (math.stackexchange, gcn.com,
industrialsage.com, embodiedhub.ai), a PDF-ben és a 403-at adó oldalon álló hét helyes állítás „unverified” maradt
(a keresés kivonatában nem volt benne a szám), és egy OECD-adatot a modell egy másik országhoz kötött.

    tier(url, named)        a forrás súlya: named (az állítás saját, megnevezett forrásának oldala), official (állami,
                            nemzetközi, statisztikai, egyetemi), secondary (sajtó, blog, cég), weak (fórum, kérdés-válasz,
                            közösségi oldal) — általános szabályokkal, nem egyedi oldal-listával
    key_numbers(quote)      az állítás számai (az évszámok nélkül, ha más szám is van), írásmód-változatokkal
    passages(text, quote)   a forrásszöveg részletei a számok körül (oldal, PDF) — ezt látja az ítélet
    quoted(quote, text)     a modell idézete tényleg ott van-e a forrásban (a kitalált idézet nem bizonyíték)
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional
from urllib.parse import urlparse

TIERS = ("named", "official", "secondary", "weak")

# állami, nemzetközi, egyetemi végződés
_OFFICIAL_HOST = re.compile(r"(^|\.)(gov|gob|gouv|go|govt|gv|admin|bund|mil|edu|ac|int)(\.[a-z]{2,3})?$|(^|\.)europa\.eu$")
# a .org/.hu végű nemzetközi szervezetek, statisztikai hivatalok, jegybankok és tudományos kiadók
_OFFICIAL_ORG = {"oecd.org", "oecd-ilibrary.org", "imf.org", "worldbank.org", "ilo.org", "un.org", "unctad.org",
                 "bis.org", "wto.org", "iea.org", "ksh.hu", "mnb.hu", "kormany.hu", "njt.hu", "magyarkozlony.hu",
                 "parlament.hu", "stlouisfed.org", "nber.org", "doi.org", "arxiv.org", "ssrn.com", "nature.com",
                 "science.org", "sciencedirect.com", "springer.com", "link.springer.com", "wiley.com", "jstor.org"}
# kérdés-válasz, fórum, közösségi és feltöltött tartalom: senki nem felel érte, ami ott áll
_WEAK_HOST = re.compile(r"(^|\.)(stackexchange\.com|stackoverflow\.com|quora\.com|reddit\.com|answers\.[a-z.]+|"
                        r"medium\.com|substack\.com|linkedin\.com|facebook\.com|x\.com|twitter\.com|youtube\.com|"
                        r"tiktok\.com|instagram\.com|pinterest\.[a-z.]+|scribd\.com|slideshare\.net|coursehero\.com|"
                        r"studocu\.com|brainly\.[a-z.]+|chegg\.com|fandom\.com|blogspot\.com|wordpress\.com)$")


def host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower().removeprefix("www.")


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def names_source(h: str, named: Optional[str]) -> bool:
    """A webhely az állítás megnevezett forrásáé-e: „IFR” → ifr.org, „PwC Magyarország” → pwc.com, „International
    Federation of Robotics” → ifr.org (betűszó)."""
    if not h or not named:
        return False
    labels = h.split(".")[:-1] or h.split(".")
    for tok in re.findall(r"[a-z0-9]+", _fold(named)):
        if len(tok) >= 3 and any(lb == tok or (len(tok) >= 4 and lb.startswith(tok)) for lb in labels):
            return True
    caps = [w for w in re.findall(r"[^\W\d_]+", named) if w[0].isupper()]
    acro = "".join(w[0] for w in caps).lower()
    return len(acro) >= 3 and _fold(acro) in labels


def tier(url: str, named: Optional[str] = None) -> str:
    h = host(url)
    if names_source(h, named):
        return "named"
    if _OFFICIAL_HOST.search(h) or h in _OFFICIAL_ORG or any(h.endswith("." + o) for o in _OFFICIAL_ORG):
        return "official"
    if _WEAK_HOST.search(h):
        return "weak"
    return "secondary"


_NUM = re.compile(r"(?<![\w.,])\d{1,3}(?:[ .  ]\d{3})+(?:[.,]\d+)?(?![\w])|(?<![\w.,])\d+(?:[.,]\d+)?(?![\w])")


def key_numbers(quote: str) -> list[str]:
    """Az idézet számai írásmód-változatokkal (27,1 → 27,1 | 27.1; 922 200 → 922 200 | 922,200 | 922200). Az évszám
    csak akkor kulcs, ha más szám nincs."""
    found = [m.group(0) for m in _NUM.finditer(quote or "")]
    plain = [x for x in found if not re.fullmatch(r"(19|20)\d\d", x)]
    out: list[str] = []
    for x in plain or found:
        digits = re.sub(r"[ .  ](?=\d{3}\b)", "", x)
        for v in (x, x.replace(",", "."), x.replace(".", ","), digits, digits.replace(",", "."), digits.replace(".", ",")):
            if v not in out:
                out.append(v)
    return out


def _num_hits(text: str, nums: list[str]) -> list[int]:
    hits = []
    for n in nums:
        for m in re.finditer(r"(?<![\d.,])" + re.escape(n) + r"(?![\d])", text):
            hits.append(m.start())
    return sorted(set(hits))


def passages(text: str, quote: str, n: int = 2, width: int = 320) -> list[str]:
    """A forrásszöveg (oldal, PDF) legfeljebb n részlete az állítás számai körül — üres, ha a szám nincs benne."""
    text = re.sub(r"\s+", " ", text or "")
    out: list[tuple[int, int]] = []
    for at in _num_hits(text, key_numbers(quote)):
        a, b = max(0, at - width), min(len(text), at + width)
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
        if len(out) >= n:
            break
    return [("…" if a else "") + text[a:b].strip() + ("…" if b < len(text) else "") for a, b in out]


def has_numbers(text: str, quote: str) -> bool:
    nums = key_numbers(quote)
    return bool(nums) and bool(_num_hits(re.sub(r"\s+", " ", text or ""), nums))


def quoted(quote: Optional[str], text: str) -> bool:
    """A modell idézete ott van-e a forrásszövegben: szó szerint (írásjel és kis-nagybetű nélkül), vagy legalább a
    szavai négyötöde sorrendben, a számai mind."""
    if not quote:
        return False
    q = re.findall(r"\w+", _fold(quote))
    t = re.findall(r"\w+", _fold(text))
    if not q:
        return False
    if " ".join(q) in " ".join(t):
        return True
    nums = [w for w in q if w[0].isdigit()]
    if any(w not in t for w in nums):
        return False
    k, hit = 0, 0
    for w in q:
        try:
            k = t.index(w, k) + 1
            hit += 1
        except ValueError:
            continue
    return hit >= 0.8 * len(q)
