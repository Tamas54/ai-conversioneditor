"""Nyelvek: egy nyelv = egy JSON-fájl az app/site/i18n/ mappában (pl. de.json). Új fájl → új nyelv, kódmódosítás nélkül.

Az alapnyelv (SITE_DEFAULT_LANG, alapból "en") a gyökérben él (/convert/…), a többi a saját előtagja alatt
(/hu/atalakitas/…). Hiányzó kulcs → az alapnyelv szövege → végső esetben maga a kulcs (így egy félig fordított
nyelv is működik). A slugok (URL-részek) is a nyelvi fájlban vannak: a magyar URL magyar szavakból áll.
"""
from __future__ import annotations

import functools
import json
import os
import re
import string
from pathlib import Path
from typing import Any

DIR = Path(__file__).parent / "i18n"
DEFAULT = os.getenv("SITE_DEFAULT_LANG", "en")


@functools.lru_cache(maxsize=1)
def _all() -> dict[str, dict[str, Any]]:
    out = {}
    for p in sorted(DIR.glob("*.json")):
        code = p.stem
        if not re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", code):
            continue
        out[code] = json.loads(p.read_text("utf-8"))
    return out


def langs() -> list[str]:
    """A bekapcsolt nyelvek (SITE_LANGS, vesszővel; alapból csak az alapnyelv — nemzetközi, angol termék),
    az alapnyelv elöl. Egy további nyelv: a fájl a mappába + a kódja a SITE_LANGS-be (pl. "en,de")."""
    on = {c.strip() for c in os.getenv("SITE_LANGS", DEFAULT).split(",") if c.strip()} | {DEFAULT}
    codes = [c for c in _all() if c in on]
    return sorted(codes, key=lambda c: (c != DEFAULT, c))


def data(lang: str) -> dict[str, Any]:
    return _all().get(lang) or _all()[DEFAULT]


def _dig(d: Any, key: str) -> Any:
    for part in key.split("."):
        if not isinstance(d, dict) or part not in d:
            return None
        d = d[part]
    return d


def raw(lang: str, key: str, default: Any = None) -> Any:
    v = _dig(data(lang), key)
    if v is None and lang != DEFAULT:
        v = _dig(data(DEFAULT), key)
    return default if v is None else v


class _Safe(dict):
    def __missing__(self, k: str) -> str:
        return "{" + k + "}"


_FMT = string.Formatter()


def t(lang: str, key: str, **kw: Any) -> str:
    v = raw(lang, key)
    if v is None:
        return key
    if not isinstance(v, str):
        return str(v)
    return _FMT.vformat(v, (), _Safe(kw)) if kw else v


def meta(lang: str) -> dict[str, Any]:
    return {"code": lang, **raw(lang, "_meta", {})}


def slug(lang: str, key: str) -> str:
    return raw(lang, f"slug.{key}", key)


def prefix(lang: str) -> str:
    return "" if lang == DEFAULT else f"/{lang}"


def reload() -> None:
    _all.cache_clear()
