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


def root_name(source_label: Optional[str], fallback: str = "file") -> str:
    """„stage – background replaced v2.psd" → „stage"."""
    stem = Path(source_label or "").stem if source_label else ""
    stem = stem.split(SEP, 1)[0].strip()
    stem = _VER.sub("", stem).strip()
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


def safe_title(title: str, limit: int = 120) -> str:
    """Felhasználói cím fájlnévnek (a / és vezérlőkarakterek nélkül)."""
    t = re.sub(r"[\x00-\x1f/\\]+", " ", str(title or "")).strip()
    return re.sub(r"\s+", " ", t)[:limit].strip()
