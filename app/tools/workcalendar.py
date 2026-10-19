"""Magyar munkanaptár: melyik nap munkanap, pihenőnap, munkaszüneti nap — becslés nélkül.

  munkaszüneti nap  az Mt. 102. § (1) szerint (2012. évi I. törvény, ma hatályos szöveg): január 1., március 15.,
                    nagypéntek, húsvéthétfő, május 1., pünkösdhétfő, augusztus 20., október 23., november 1.,
                    december 25–26. — a húsvéthoz kötötteket a Gergely-naptári húsvétból számoljuk
  áthelyezés        az adott évi munkarend-rendelet áthelyezett munkanapjai (szombat) és pihenőnapjai, évenként egy
                    adatfájlban (app/tools/workcalendar_data/hu/ÉÉÉÉ.json) a forrással (NJT-hivatkozás, lekérés napja)

Amelyik évre nincs adatfájl (a rendelet még nem jelent meg, vagy nem töltöttük be), arra UnknownYear jön — a naptár
nem találgat. Új év: az NJT-ről a „…évi munkaszüneti napok körüli munkarendről” rendelet 2. §-a egy új fájlba.
"""
from __future__ import annotations

import datetime as dt
import functools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

DATA = Path(__file__).resolve().parent / "workcalendar_data"
LABOUR_CODE = {"title": "2012. évi I. törvény a munka törvénykönyvéről (Mt.) 102. § (1)",
               "url": "https://njt.jog.gov.hu/eli/TV/2012/1"}
_FIXED = {(1, 1): "Újév", (3, 15): "Nemzeti ünnep", (5, 1): "A munka ünnepe", (8, 20): "Az államalapítás ünnepe",
          (10, 23): "Nemzeti ünnep", (11, 1): "Mindenszentek", (12, 25): "Karácsony", (12, 26): "Karácsony"}
_WEEKDAYS = ("hétfő", "kedd", "szerda", "csütörtök", "péntek", "szombat", "vasárnap")
MONTHS = ("január", "február", "március", "április", "május", "június", "július", "augusztus", "szeptember",
          "október", "november", "december")


class UnknownYear(ValueError):
    pass


def easter(year: int) -> dt.date:
    """Húsvétvasárnap (Gergely-naptár, anonim algoritmus)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = (h + l_ - 7 * m + 114) % 31 + 1
    return dt.date(year, month, day)


def holidays(year: int) -> dict[dt.date, str]:
    """Az Mt. 102. § (1) munkaszüneti napjai az adott évben."""
    out = {dt.date(year, m, d): name for (m, d), name in _FIXED.items()}
    e = easter(year)
    out[e - dt.timedelta(days=2)] = "Nagypéntek"
    out[e + dt.timedelta(days=1)] = "Húsvéthétfő"
    out[e + dt.timedelta(days=50)] = "Pünkösdhétfő"
    return out


@functools.lru_cache(maxsize=32)
def year_data(year: int, country: str = "HU") -> dict[str, Any]:
    p = DATA / country.lower() / f"{year}.json"
    if not p.exists():
        known = sorted(int(f.stem) for f in (DATA / country.lower()).glob("*.json"))
        raise UnknownYear(f"The {country} work calendar for {year} isn't loaded (no decree data) — known years: "
                          f"{', '.join(map(str, known)) or 'none'}. It isn't guessed.")
    d = json.loads(p.read_text("utf-8"))
    return {"workdays": {dt.date.fromisoformat(x) for x in d.get("workdays", [])},
            "restdays": {dt.date.fromisoformat(x) for x in d.get("restdays", [])}, "source": d.get("source") or {}}


@dataclass
class Day:
    date: dt.date
    kind: str                        # workday | transferred_workday | weekend | holiday | restday
    name: Optional[str] = None       # a munkaszüneti nap neve
    weekday: str = ""

    @property
    def is_workday(self) -> bool:
        return self.kind in ("workday", "transferred_workday")


def day(d: dt.date, country: str = "HU") -> Day:
    yd = year_data(d.year, country)
    wd = _WEEKDAYS[d.weekday()]
    hol = holidays(d.year)
    if d in hol:
        return Day(d, "holiday", hol[d], wd)
    if d in yd["workdays"]:
        return Day(d, "transferred_workday", None, wd)
    if d in yd["restdays"]:
        return Day(d, "restday", None, wd)
    return Day(d, "weekend" if d.weekday() >= 5 else "workday", None, wd)


def month(year: int, mon: int, country: str = "HU") -> list[Day]:
    first = dt.date(year, mon, 1)
    n = ((first.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - first).days
    return [day(first + dt.timedelta(days=i), country) for i in range(n)]


def sources(year: int, country: str = "HU") -> list[dict[str, str]]:
    return [LABOUR_CODE, year_data(year, country)["source"]]
