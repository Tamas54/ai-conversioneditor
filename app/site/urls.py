"""A nyilvános oldalak URL-jei nyelvenként, beszédes slugokkal (a slugok a nyelvi fájlban vannak).

    en (alap):  /convert/documents, /convert/docx-to-pdf, /formats/docx, /tools/compress-pdf, /developers/docs
    hu:         /hu/atalakitas/dokumentumok, /hu/atalakitas/docx-bol-pdf, /hu/formatumok/docx, /hu/eszkozok/pdf-tomorites

Minden oldalnak minden nyelven van párja (hreflang); a mélység legfeljebb 3 kattintás a kezdőlaptól:
kezdőlap → kategória-hub → konverziós/eszköz-oldal.
"""
from __future__ import annotations

import functools
import re
from typing import Optional

from app.catalog.formats import FORMATS, Format
from app.site import i18n


def P(lang: str) -> str:
    return i18n.prefix(lang)


def S(lang: str, key: str) -> str:
    return i18n.slug(lang, key)


def cat_slug(lang: str, cat: str) -> str:
    return i18n.raw(lang, f"cat.{cat}.slug", cat)


def tool_slug(lang: str, key: str) -> str:
    return i18n.raw(lang, f"tool.{key}.slug", key)


def cmp_slug(lang: str, key: str) -> str:
    return i18n.raw(lang, f"cmp.{key}.slug", key)


def unit_slug(lang: str, key: str) -> str:
    return i18n.raw(lang, f"unitcat.{key}.slug", key)


def pair_slug(lang: str, src: str, dst: str) -> str:
    pat = i18n.raw(lang, "_meta.pair", "{src}-to-{dst}")
    return pat.format(src=FORMATS[src].slug, dst=FORMATS[dst].slug)


def home(lang: str) -> str:
    """A kezdőlap a meglévő app-címlap (/app) — a nyilvános oldalak annak a részei, nem külön honlap."""
    return "/app"


def agents(lang: str) -> str:
    return f"{P(lang)}/{S(lang, 'agents')}"


def convert_hub(lang: str) -> str:
    return f"{P(lang)}/{S(lang, 'convert')}"


def category(lang: str, cat: str) -> str:
    return f"{convert_hub(lang)}/{cat_slug(lang, cat)}"


def pair(lang: str, src: str, dst: str) -> str:
    return f"{convert_hub(lang)}/{pair_slug(lang, src, dst)}"


def fmt(lang: str, key: str) -> str:
    return f"{P(lang)}/{S(lang, 'formats')}/{FORMATS[key].slug}"


def tools_hub(lang: str) -> str:
    return f"{P(lang)}/{S(lang, 'tools')}"


def tool(lang: str, key: str) -> str:
    return f"{tools_hub(lang)}/{tool_slug(lang, key)}"


def compare_hub(lang: str) -> str:
    return f"{P(lang)}/{S(lang, 'compare')}"


def compare(lang: str, key: str) -> str:
    return f"{compare_hub(lang)}/{cmp_slug(lang, key)}"


def developers(lang: str) -> str:
    return f"{P(lang)}/{S(lang, 'developers')}"


def docs(lang: str) -> str:
    return f"{developers(lang)}/{S(lang, 'docs')}"


def job_builder(lang: str) -> str:
    return f"{developers(lang)}/{S(lang, 'job_builder')}"


def api_pair(lang: str, src: str, dst: str) -> str:
    return f"{developers(lang)}/{S(lang, 'convert')}/{pair_slug(lang, src, dst)}"


def connect_ai(lang: str) -> str:
    return f"{P(lang)}/{S(lang, 'connect_ai')}"


def units(lang: str) -> str:
    return f"{P(lang)}/{S(lang, 'units')}"


def unit_cat(lang: str, key: str) -> str:
    return f"{units(lang)}/{unit_slug(lang, key)}"


def timezones(lang: str) -> str:
    return f"{P(lang)}/{S(lang, 'timezones')}"


# ---------- visszafejtés ----------

def parse_pair(lang: str, slug: str) -> Optional[tuple[str, str]]:
    pat = i18n.raw(lang, "_meta.pair", "{src}-to-{dst}")
    rx = "^" + re.escape(pat).replace(r"\{src\}", "(?P<src>.+?)").replace(r"\{dst\}", "(?P<dst>.+)") + "$"
    m = re.match(rx, slug)
    if not m:
        return None
    by_slug = {f.slug: f.key for f in FORMATS.values()}
    s, d = by_slug.get(m.group("src")), by_slug.get(m.group("dst"))
    return (s, d) if s and d else None


def find_by_slug(lang: str, section: str, slug: str) -> Optional[str]:
    """Egy nyelvi slug → a belső kulcs (kategória / eszköz / összevetés / mértékegység-kategória)."""
    table = i18n.raw(lang, section, {}) or {}
    for k, v in table.items():
        if isinstance(v, dict) and v.get("slug", k) == slug:
            return k
    base = i18n.raw(i18n.DEFAULT, section, {}) or {}
    if slug in base:
        return slug
    return None


@functools.lru_cache(maxsize=1)
def first_segments() -> frozenset[str]:
    """Az auth-middleware számára: ezek az első URL-szegmensek nyilvános oldalak (nyelvenként)."""
    segs: set[str] = set()
    for lang in i18n.langs():
        if lang == i18n.DEFAULT:
            for k in ("convert", "formats", "tools", "compare", "developers", "connect_ai", "units", "timezones"):
                segs.add(S(lang, k))
        else:
            segs.add(lang)
    return frozenset(segs)


def is_public(path: str) -> bool:
    if path == "/":
        return True
    seg = path.strip("/").split("/", 1)[0]
    return seg in first_segments()


def absolute(path: str) -> str:
    from app.config import settings
    return settings.PUBLIC_BASE_URL.rstrip("/") + path
