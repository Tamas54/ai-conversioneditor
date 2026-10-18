"""Átlátható eredmény-nevek: „<forrás> – <mi történt>", és ha ugyanabból több lett: „v2, v3".

A ChatGPT-teszt (10-12): „a megadott címek helyett »Untitled« fájlok születtek; több stage.jpg nehezen
különböztethető meg". Eddig az eredmény a forrás nevét kapta (stage.jpg → stage.psd → stage.jpg), a
közvetlen szerkesztők (find_replace, delete_pages…) pedig semmit (→ „3fa8c1e2.docx").

A gyökér: a forrás neve a saját „ – …" utótagja és „ vN" száma nélkül — így egy lánc
(stage → stage – background replaced → stage – edited) nem nő végtelen hosszúra, a sorszám mutatja a sorrendet.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from app import tenancy

SEP = " – "
_VER = re.compile(r"\s+v(\d+)$", re.I)
# a régi (zárójeles) művelet-utótagok is a gyökérhez tartoznak — különben láncban halmozódnak
# („x (fillable) (3 filled) (3 filled).pdf", 10-12)
_LEGACY = re.compile(r"\s*\((?:fillable|filled|\d+ filled|searchable|original|restored(?:, [a-z ]+)?|edited[^)]*|enhanced|layers|book)\)$", re.I)


def root_name(source_label: Optional[str], fallback: str = "file") -> str:
    """„stage – background replaced v2.psd" → „stage"."""
    stem = Path(source_label or "").stem if source_label else ""
    stem = stem.split(SEP, 1)[0].strip()
    stem = _VER.sub("", stem).strip()
    while _LEGACY.search(stem):
        stem = _LEGACY.sub("", stem).strip()
    return stem or fallback


def _taken() -> set[str]:
    """A munkaterület összes címkéjének kisbetűs törzse (kiterjesztés nélkül)."""
    out: set[str] = set()
    root = tenancy.user_root()
    for d in (root / "inputs", root / "outputs"):
        if not d.exists():
            continue
        for m in d.glob("*.meta.json"):
            try:
                label = json.loads(m.read_text("utf-8")).get("label")
            except Exception:
                continue
            if label:
                out.add(Path(str(label)).stem.lower())
    return out


def result_stem(source_file_id: Optional[str], what: str, fallback: str = "file") -> str:
    """Az eredmény neve kiterjesztés nélkül: „<gyökér> – <what>", foglalt név esetén „ v2", „ v3"…"""
    from app.storage import display_label
    root = root_name(display_label(source_file_id) if source_file_id else None, fallback)
    base = f"{root}{SEP}{what}" if what else root
    taken = _taken()
    if base.lower() not in taken:
        return base
    n = 2
    while f"{base} v{n}".lower() in taken:
        n += 1
    return f"{base} v{n}"


def unique_stem(base: str) -> str:
    """Egy adott név (pl. „Oklevél – Kovács Anna”), foglalt esetén „ v2”, „ v3”…"""
    base = safe_title(base) or "file"
    taken = _taken()
    if base.lower() not in taken:
        return base
    n = 2
    while f"{base} v{n}".lower() in taken:
        n += 1
    return f"{base} v{n}"


_KIND_NAMES = {"certificate": ("Oklevél", "Certificate"), "poster": ("Plakát", "Poster"), "flyer": ("Szórólap", "Flyer"),
               "report_cover": ("Borító", "Report cover"), "invitation": ("Meghívó", "Invitation"),
               "social": ("Közösségi poszt", "Social post"), "booklet": ("Füzet", "Booklet"), "card": ("Kártya", "Card"),
               "auto": ("Design", "Design")}


def design_name(template: str, data: dict) -> str:
    """A dizájn neve a sablonból és a tartalomból: „Oklevél – Kovács Anna” (oklevél: a címzett; más: a cím) —
    a sablon neve a tartalom nyelvén (magyar szövegnél magyarul)."""
    from app.tools import textlang
    d = data or {}
    who = d.get("recipient") if template in ("certificate", "invitation") else None
    if isinstance(who, str):
        who = re.sub(r"^(dear|kedves|tisztelt)\s+", "", who.strip(), flags=re.I).rstrip(",!")
    subject = (who or d.get("title") or d.get("name") or "").strip() if isinstance(who or d.get("title") or d.get("name") or "", str) else ""
    sample = " ".join(str(d.get(k) or "") for k in ("title", "recipient", "text", "lead", "subtitle", "body", "goal"))
    hu = (textlang.detect(sample, min_hits=2, min_words=3) or "").startswith("hu")
    hu_name, en_name = _KIND_NAMES.get(template, (template.replace("_", " ").capitalize(),) * 2)
    kind = hu_name if hu else en_name
    title_is_kind = subject and subject.lower() in (hu_name.lower(), en_name.lower())
    return f"{kind}{SEP}{subject}" if subject and not title_is_kind else kind


def safe_title(title: str, limit: int = 120) -> str:
    """Felhasználói cím fájlnévnek (a / és vezérlőkarakterek nélkül)."""
    t = re.sub(r"[\x00-\x1f/\\]+", " ", str(title or "")).strip()
    return re.sub(r"\s+", " ", t)[:limit].strip()
