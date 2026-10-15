"""Mezőértékek ellenőrzése és egységesítése. Vissza: (rendben?, egységesített érték, hibaüzenet).

    date          2026-10-15, 2026.10.15., 15.10.2026, 15/10/2026 → ISO; a mező kérhet magyar alakot (2026. 10. 15.)
    iban          ISO 13616 (mod 97), országonkénti hossz; csoportosítva írjuk ki
    hu_tax_id     adóazonosító jel: 10 jegy, 8-cal kezdődik, ellenőrzőszám (súlyok 1..9, mod 11)
    taj           TAJ-szám: 9 jegy, súlyok 3,7,… az első 8 jegyre, mod 10
    hu_vat        adószám: 8-1-2 jegy, törzsszám ellenőrzőszáma (9,7,3,1 súly, mod 10)
    hu_bank       belföldi bankszámlaszám: 2×8 vagy 3×8 jegy, 8-as csoportonként 9,7,3,1 súly, mod 10
    email, phone, postcode (HU: 4 jegy), number
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional

IBAN_LEN = {"AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22, "BR": 29, "CH": 21,
            "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24, "FI": 18, "FO": 18,
            "FR": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28, "HR": 21, "HU": 28, "IE": 22, "IL": 23,
            "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27,
            "MD": 24, "ME": 22, "MK": 19, "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15, "PK": 24, "PL": 28, "PS": 29,
            "PT": 25, "QA": 29, "RO": 24, "RS": 22, "SA": 24, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "TN": 24, "TR": 26,
            "UA": 29, "VG": 24, "XK": 20}

Result = tuple[bool, Any, Optional[str]]


def _digits(v: Any) -> str:
    return re.sub(r"\D", "", str(v or ""))


def parse_date(v: Any) -> Optional[date]:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip().rstrip(".")
    for pat, order in ((r"(\d{4})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})", "ymd"),
                       (r"(\d{1,2})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{4})", "dmy"),
                       (r"(\d{4})(\d{2})(\d{2})", "ymd")):
        m = re.fullmatch(pat, s)
        if m:
            a, b, c = (int(x) for x in m.groups())
            y, mo, d = (a, b, c) if order == "ymd" else (c, b, a)
            try:
                return date(y, mo, d)
            except ValueError:
                return None
    months = {"január": 1, "február": 2, "március": 3, "április": 4, "május": 5, "június": 6, "július": 7,
              "augusztus": 8, "szeptember": 9, "október": 10, "november": 11, "december": 12}
    m = re.fullmatch(r"(\d{4})\.?\s*([a-záéíóöőúüű]+)\s*(\d{1,2})\.?", s.lower())
    if m and m.group(2) in months:
        try:
            return date(int(m.group(1)), months[m.group(2)], int(m.group(3)))
        except ValueError:
            return None
    return None


def check_date(v: Any, style: str = "iso") -> Result:
    d = parse_date(v)
    if not d:
        return False, v, "not a valid date (e.g. 2026-10-15 or 2026.10.15.)"
    return True, (d.strftime("%Y. %m. %d.") if style == "hu" else d.isoformat()), None


def check_iban(v: Any) -> Result:
    s = re.sub(r"\s|-", "", str(v or "")).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{8,30}", s):
        return False, v, "IBAN: country code, 2 check digits, then the account number"
    if IBAN_LEN.get(s[:2]) and len(s) != IBAN_LEN[s[:2]]:
        return False, v, f"IBAN: a {s[:2]} IBAN has {IBAN_LEN[s[:2]]} characters, this has {len(s)}"
    num = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    if int(num) % 97 != 1:
        return False, v, "IBAN: the check digits don't match — probably a typo"
    return True, " ".join(s[i:i + 4] for i in range(0, len(s), 4)), None


def check_hu_tax_id(v: Any) -> Result:
    d = _digits(v)
    if len(d) != 10 or d[0] != "8":
        return False, v, "adóazonosító jel: 10 digits starting with 8"
    if sum(int(d[i]) * (i + 1) for i in range(9)) % 11 != int(d[9]):
        return False, v, "adóazonosító jel: the check digit doesn't match"
    return True, d, None


def hu_tax_id_birth(v: Any) -> Optional[date]:
    """Az adóazonosító jel 2–6. jegye: a születés napja 1867.01.01-től számolva."""
    from datetime import timedelta
    d = _digits(v)
    return date(1867, 1, 1) + timedelta(days=int(d[1:6])) if len(d) == 10 else None


def check_taj(v: Any) -> Result:
    d = _digits(v)
    if len(d) != 9:
        return False, v, "TAJ: 9 digits"
    if sum(int(d[i]) * (3 if i % 2 == 0 else 7) for i in range(8)) % 10 != int(d[8]):
        return False, v, "TAJ: the check digit doesn't match"
    return True, f"{d[:3]} {d[3:6]} {d[6:]}", None


def check_hu_vat(v: Any) -> Result:
    d = _digits(v)
    if len(d) != 11:
        return False, v, "adószám: 11 digits (12345678-1-23)"
    if sum(int(d[i]) * w for i, w in enumerate((9, 7, 3, 1, 9, 7, 3, 1))) % 10 != 0:
        return False, v, "adószám: the check digit of the first 8 digits doesn't match"
    if d[8] not in "12345":
        return False, v, "adószám: the 9th digit (VAT code) must be 1–5"
    return True, f"{d[:8]}-{d[8]}-{d[9:]}", None


def check_hu_bank(v: Any) -> Result:
    d = _digits(v)
    if len(d) not in (16, 24):
        return False, v, "bankszámlaszám: 2×8 or 3×8 digits"
    for k in range(0, len(d), 8):
        g = d[k:k + 8]
        if sum(int(g[i]) * (9, 7, 3, 1)[i % 4] for i in range(8)) % 10 != 0:
            return False, v, f"bankszámlaszám: group {k // 8 + 1} has a wrong check digit"
    return True, "-".join(d[i:i + 8] for i in range(0, len(d), 8)), None


def check_email(v: Any) -> Result:
    s = str(v or "").strip()
    ok = bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", s))
    return ok, s, None if ok else "not an email address"


def check_phone(v: Any) -> Result:
    s = str(v or "").strip()
    d = _digits(s)
    if not 8 <= len(d) <= 15:
        return False, v, "phone: 8–15 digits"
    if d.startswith("36") and len(d) in (10, 11) and s.lstrip().startswith("+"):
        d = "06" + d[2:]
    if d.startswith("06") and len(d) in (10, 11):         # magyar szám: +36 1 234 5678 / +36 30 123 4567
        n = d[2:]
        s = f"+36 1 {n[1:4]} {n[4:]}" if n[0] == "1" else f"+36 {n[:2]} {n[2:5]} {n[5:]}"
    return True, s, None


def check_postcode(v: Any, country: str = "HU") -> Result:
    d = _digits(v)
    if country == "HU" and not re.fullmatch(r"[1-9]\d{3}", d):
        return False, v, "irányítószám: 4 digits"
    return True, d or str(v), None


def check_number(v: Any) -> Result:
    s = str(v or "").strip().replace(" ", "").replace(",", ".")
    try:
        float(s)
    except ValueError:
        return False, v, "not a number"
    return True, str(v).strip(), None


CHECKS = {"date": check_date, "iban": check_iban, "hu_tax_id": check_hu_tax_id, "taj": check_taj, "hu_vat": check_hu_vat,
          "hu_bank": check_hu_bank, "email": check_email, "phone": check_phone, "postcode": check_postcode,
          "number": check_number}


def check(kind: Optional[str], value: Any) -> Result:
    if value in (None, "") or not kind or kind not in CHECKS:
        return True, value, None
    return CHECKS[kind](value)
