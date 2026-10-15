"""Egységes mezőséma — minden formátumra ugyanaz, így a kitöltő, a validátor, az ügynök és a felület is egyet ismer.

    {"schema": "aice.form/1", "format": "pdf", "kind": "acroform|flat_pdf|docx|xlsx|image",
     "pages": [{"page": 1, "width": 595, "height": 842}],
     "fields": [{"name": "szuletesi_datum", "label": "Születési dátum", "type": "date", "page": 1,
                 "box": [x0, y0, x1, y1], "options": [], "required": false, "value": null,
                 "check": "date", "anchor": {...formátumfüggő hely...}}]}

`box` a lap/kép saját egységében (PDF: pont, fotó: pixel), bal felső origóval. `anchor` mondja meg a kitöltőnek,
hová írjon (PDF-mező neve, DOCX bekezdés + kitöltendő minta, XLSX cella…). A felhasználó a sémát átírhatja
(címke, típus, doboz), új mezőt vehet fel — a kitöltő a kapott sémával dolgozik.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional

SCHEMA = "aice.form/1"
TYPES = ("text", "multiline", "checkbox", "date", "signature", "choice", "number", "email", "phone", "iban", "hu_tax_id",
         "taj", "hu_vat", "hu_bank", "postcode")
_CHECK_OF = {"date": "date", "email": "email", "phone": "phone", "iban": "iban", "hu_tax_id": "hu_tax_id", "taj": "taj",
             "hu_vat": "hu_vat", "hu_bank": "hu_bank", "postcode": "postcode", "number": "number"}


def ascii_fold(s: str) -> str:
    return unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()


def slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", ascii_fold(label)).strip("_")
    return s[:48] or "field"


def clean_label(text: str) -> str:
    t = re.sub(r"[_.…]{3,}|\[\s*\]|☐|□|\s+", " ", str(text)).strip()
    return t.rstrip(":：*").strip()


# címke (ékezet nélkül, kisbetűvel) → típus; a sorrend számít (az első találat nyer)
_TYPE_RULES: list[tuple[str, str]] = [
    (r"\b(alairas|signature|sign here|kezjegy|initials|szignal)", "signature"),
    (r"adoazonosito|tax ?id|tin\b", "hu_tax_id"),
    (r"\badoszam|vat (no|number)|vat\b", "hu_vat"),
    (r"\btaj\b|tb ?szam|social security", "taj"),
    (r"\biban\b", "iban"),
    (r"bankszamla|szamlaszam|bank account|account (no|number)", "hu_bank"),
    (r"e-?mail", "email"),
    (r"telefon|phone|mobil|tel\.?\b", "phone"),
    (r"iranyitoszam|postcode|post code|zip", "postcode"),
    (r"datum|date|\bkelt\b|szuletesi ido|date of birth|\bdob\b|ervenyes", "date"),
    (r"megjegyzes|comment|remarks|indoklas|leiras|description|reason", "multiline"),
    (r"osszeg|amount|\bdb\b|quantity|darabszam", "number"),
    (r"hozzajarul|elfogad|agree|consent|igen/nem|yes/no", "checkbox"),
]


def guess_type(label: str, default: str = "text") -> str:
    a = ascii_fold(label)
    for pat, typ in _TYPE_RULES:
        if re.search(pat, a):
            return typ
    return default


def field(name: str, label: str, typ: str, *, page: Optional[int] = None, box: Optional[list[float]] = None,
          anchor: Optional[dict[str, Any]] = None, options: Optional[list[str]] = None, value: Any = None,
          required: bool = False) -> dict[str, Any]:
    typ = typ if typ in TYPES else "text"
    return {"name": name, "label": label, "type": typ, "page": page,
            "box": [round(float(v), 2) for v in box] if box else None, "options": options or [], "required": required,
            "value": value, "check": _CHECK_OF.get(typ), "anchor": anchor or {}}


def unique_names(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, int] = {}
    for f in fields:
        base = f["name"] or "field"
        if base in seen:
            seen[base] += 1
            f["name"] = f"{base}_{seen[base]}"
        else:
            seen[base] = 1
    return fields


def make(fmt: str, kind: str, pages: list[dict[str, Any]], fields: list[dict[str, Any]], **extra) -> dict[str, Any]:
    return {"schema": SCHEMA, "format": fmt, "kind": kind, "pages": pages, "fields": unique_names(fields), **extra}


def merge_manual(detected: dict[str, Any], manual: Optional[list[dict[str, Any]]]) -> dict[str, Any]:
    """A felhasználó kézi igazítása: azonos nevű mező felülírja a felismertet (címke, típus, doboz, kötelező…),
    új név új mezőt ad, `"remove": true` törli. A horgony (anchor) a felismertből marad, ha nem adtak újat."""
    if not manual:
        return detected
    by = {f["name"]: f for f in detected["fields"]}
    order = [f["name"] for f in detected["fields"]]
    for m in manual:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        name = str(m["name"])
        if m.get("remove"):
            by.pop(name, None)
            continue
        base = by.get(name) or field(name, m.get("label") or name, m.get("type") or guess_type(m.get("label") or name))
        upd = {k: m[k] for k in ("label", "type", "page", "box", "options", "required", "anchor") if k in m}
        if "type" in upd:
            upd["check"] = _CHECK_OF.get(upd["type"])
        base = {**base, **upd}
        if name not in by:
            order.append(name)
        by[name] = base
    detected = dict(detected)
    detected["fields"] = [by[n] for n in order if n in by]
    return detected
