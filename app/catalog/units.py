"""Mértékegység- és időzóna-átváltás — tiszta Python, külső szolgáltatás nélkül (pontos SI-tényezőkkel).

    convert(1, "mi", "km") → 1.609344
    convert(100, "c", "f") → 212.0
    tz_convert("2026-10-15 09:00", "Europe/Budapest", ["America/New_York", "Asia/Tokyo"])
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional


@dataclass(frozen=True)
class Unit:
    key: str
    category: str
    symbol: str
    factor: float = 1.0                       # alapegységre (lineáris)
    to_base: Optional[Callable[[float], float]] = None
    from_base: Optional[Callable[[float], float]] = None


def _u(key, cat, symbol, factor=1.0, to_base=None, from_base=None) -> Unit:
    return Unit(key, cat, symbol, factor, to_base, from_base)


_L = [
    # hossz (m)
    _u("m", "length", "m"), _u("km", "length", "km", 1000), _u("cm", "length", "cm", 0.01), _u("mm", "length", "mm", 0.001),
    _u("um", "length", "µm", 1e-6), _u("nm", "length", "nm", 1e-9), _u("in", "length", "in", 0.0254), _u("ft", "length", "ft", 0.3048),
    _u("yd", "length", "yd", 0.9144), _u("mi", "length", "mi", 1609.344), _u("nmi", "length", "nmi", 1852),
    _u("ly", "length", "ly", 9.4607304725808e15), _u("au", "length", "au", 149597870700),
    # tömeg (kg)
    _u("kg", "mass", "kg"), _u("g", "mass", "g", 0.001), _u("mg", "mass", "mg", 1e-6), _u("t", "mass", "t", 1000),
    _u("lb", "mass", "lb", 0.45359237), _u("oz", "mass", "oz", 0.028349523125), _u("st", "mass", "st", 6.35029318),
    _u("ct", "mass", "ct", 0.0002), _u("dkg", "mass", "dkg", 0.01), _u("q", "mass", "q", 100),
    # hőmérséklet (K)
    _u("k", "temperature", "K"),
    _u("c", "temperature", "°C", to_base=lambda v: v + 273.15, from_base=lambda v: v - 273.15),
    _u("f", "temperature", "°F", to_base=lambda v: (v - 32) * 5 / 9 + 273.15, from_base=lambda v: (v - 273.15) * 9 / 5 + 32),
    _u("r", "temperature", "°R", 5 / 9),
    # terület (m²)
    _u("m2", "area", "m²"), _u("km2", "area", "km²", 1e6), _u("cm2", "area", "cm²", 1e-4), _u("ha", "area", "ha", 1e4),
    _u("a", "area", "a", 100), _u("acre", "area", "ac", 4046.8564224), _u("ft2", "area", "ft²", 0.09290304),
    _u("in2", "area", "in²", 0.00064516), _u("mi2", "area", "mi²", 2589988.110336), _u("negyszogol", "area", "n.-öl", 3.596651),
    # térfogat (m³)
    _u("m3", "volume", "m³"), _u("l", "volume", "L", 0.001), _u("ml", "volume", "mL", 1e-6), _u("dl", "volume", "dL", 1e-4),
    _u("cl", "volume", "cL", 1e-5), _u("cm3", "volume", "cm³", 1e-6), _u("gal", "volume", "gal (US)", 0.003785411784),
    _u("galuk", "volume", "gal (UK)", 0.00454609), _u("qt", "volume", "qt (US)", 0.000946352946), _u("pt", "volume", "pt (US)", 0.000473176473),
    _u("cup", "volume", "cup (US)", 0.0002365882365), _u("floz", "volume", "fl oz (US)", 2.95735295625e-5),
    _u("tbsp", "volume", "tbsp", 1.478676478125e-5), _u("tsp", "volume", "tsp", 4.92892159375e-6), _u("ft3", "volume", "ft³", 0.028316846592),
    # sebesség (m/s)
    _u("mps", "speed", "m/s"), _u("kmh", "speed", "km/h", 1 / 3.6), _u("mph", "speed", "mph", 0.44704), _u("kn", "speed", "kn", 1852 / 3600),
    _u("fps", "speed", "ft/s", 0.3048), _u("mach", "speed", "Mach", 340.29),
    # idő (s)
    _u("s", "time", "s"), _u("ms", "time", "ms", 0.001), _u("min", "time", "min", 60), _u("h", "time", "h", 3600),
    _u("d", "time", "d", 86400), _u("wk", "time", "wk", 604800), _u("mo", "time", "mo", 2629746), _u("yr", "time", "yr", 31556952),
    # adat (bájt)
    _u("b", "data", "B"), _u("bit", "data", "bit", 0.125), _u("kb", "data", "kB", 1e3), _u("mb", "data", "MB", 1e6),
    _u("gb", "data", "GB", 1e9), _u("tb", "data", "TB", 1e12), _u("kib", "data", "KiB", 1024), _u("mib", "data", "MiB", 1024 ** 2),
    _u("gib", "data", "GiB", 1024 ** 3), _u("tib", "data", "TiB", 1024 ** 4),
    # energia (J)
    _u("j", "energy", "J"), _u("kj", "energy", "kJ", 1000), _u("cal", "energy", "cal", 4.184), _u("kcal", "energy", "kcal", 4184),
    _u("wh", "energy", "Wh", 3600), _u("kwh", "energy", "kWh", 3.6e6), _u("btu", "energy", "BTU", 1055.05585262),
    _u("ev", "energy", "eV", 1.602176634e-19),
    # nyomás (Pa)
    _u("pa", "pressure", "Pa"), _u("kpa", "pressure", "kPa", 1000), _u("bar", "pressure", "bar", 1e5), _u("atm", "pressure", "atm", 101325),
    _u("psi", "pressure", "psi", 6894.757293168), _u("mmhg", "pressure", "mmHg", 133.322387415), _u("hpa", "pressure", "hPa", 100),
    # teljesítmény (W)
    _u("w", "power", "W"), _u("kw", "power", "kW", 1000), _u("hp", "power", "hp", 745.69987158227), _u("le", "power", "LE", 735.49875),
    _u("mw", "power", "MW", 1e6),
    # szög (fok)
    _u("deg", "angle", "°"), _u("rad", "angle", "rad", 57.29577951308232), _u("grad", "angle", "gon", 0.9), _u("arcmin", "angle", "′", 1 / 60),
    _u("arcsec", "angle", "″", 1 / 3600), _u("turn", "angle", "turn", 360),
    # frekvencia (Hz)
    _u("hz", "frequency", "Hz"), _u("khz", "frequency", "kHz", 1e3), _u("mhz", "frequency", "MHz", 1e6), _u("ghz", "frequency", "GHz", 1e9),
    _u("rpm", "frequency", "rpm", 1 / 60),
    # fogyasztás (L/100 km; a mérföld/gallon fordított arányú)
    _u("l100km", "fuel", "L/100 km"),
    _u("kml", "fuel", "km/L", to_base=lambda v: 100 / v, from_base=lambda v: 100 / v),
    _u("mpg", "fuel", "mpg (US)", to_base=lambda v: 235.214583 / v, from_base=lambda v: 235.214583 / v),
    _u("mpguk", "fuel", "mpg (UK)", to_base=lambda v: 282.480936 / v, from_base=lambda v: 282.480936 / v),
]

UNITS: dict[str, Unit] = {u.key: u for u in _L}
CATEGORIES = ("length", "mass", "temperature", "area", "volume", "speed", "time", "data", "energy", "pressure", "power",
              "angle", "frequency", "fuel")
_ALIASES = {"meter": "m", "metre": "m", "kilometer": "km", "mile": "mi", "miles": "mi", "inch": "in", "inches": "in",
            "foot": "ft", "feet": "ft", "pound": "lb", "pounds": "lb", "lbs": "lb", "ounce": "oz", "celsius": "c", "°c": "c",
            "fahrenheit": "f", "°f": "f", "kelvin": "k", "liter": "l", "litre": "l", "gallon": "gal", "km/h": "kmh",
            "m/s": "mps", "mi/h": "mph", "knot": "kn", "byte": "b", "kilobyte": "kb", "megabyte": "mb", "gigabyte": "gb",
            "m²": "m2", "km²": "km2", "m³": "m3", "l/100km": "l100km", "µm": "um", "°": "deg"}


class UnitError(ValueError):
    pass


def find(key: str) -> Unit:
    k = (key or "").strip().lower()
    u = UNITS.get(k) or UNITS.get(_ALIASES.get(k, ""))
    if not u:
        raise UnitError(f"unknown unit: {key}")
    return u


def convert(value: float, src: str, dst: str) -> float:
    a, b = find(src), find(dst)
    if a.category != b.category:
        raise UnitError(f"can't convert {a.category} to {b.category}")
    base = a.to_base(value) if a.to_base else value * a.factor
    out = b.from_base(base) if b.from_base else base / b.factor
    return float(f"{out:.12g}")


def units_for(cat: str) -> list[Unit]:
    return [u for u in _L if u.category == cat]


_EXAMPLES = {"length": [(1, "mi", "km"), (1, "in", "cm"), (1, "ft", "m"), (100, "km", "mi")],
             "mass": [(1, "lb", "kg"), (1, "kg", "lb"), (1, "oz", "g"), (1, "st", "kg")],
             "temperature": [(100, "c", "f"), (32, "f", "c"), (0, "c", "k"), (37, "c", "f")],
             "area": [(1, "acre", "m2"), (1, "ha", "acre"), (1, "km2", "mi2"), (100, "m2", "ft2")],
             "volume": [(1, "gal", "l"), (1, "l", "floz"), (1, "cup", "ml"), (1, "m3", "l")],
             "speed": [(100, "kmh", "mph"), (60, "mph", "kmh"), (1, "kn", "kmh"), (10, "mps", "kmh")],
             "time": [(1, "d", "h"), (1, "yr", "d"), (90, "min", "h"), (1, "wk", "min")],
             "data": [(1, "gb", "mb"), (1, "gib", "gb"), (100, "mb", "mib"), (1, "tb", "gb")],
             "energy": [(1, "kwh", "j"), (1, "kcal", "kj"), (1000, "btu", "kwh"), (1, "kcal", "kwh")],
             "pressure": [(1, "bar", "psi"), (1, "atm", "kpa"), (30, "psi", "bar"), (760, "mmhg", "hpa")],
             "power": [(1, "hp", "kw"), (100, "kw", "hp"), (1, "le", "kw"), (1000, "w", "kw")],
             "angle": [(180, "deg", "rad"), (1, "rad", "deg"), (100, "grad", "deg"), (1, "turn", "deg")],
             "frequency": [(1, "ghz", "mhz"), (3000, "rpm", "hz"), (50, "hz", "rpm"), (2.4, "ghz", "mhz")],
             "fuel": [(6, "l100km", "mpg"), (30, "mpg", "l100km"), (15, "kml", "l100km"), (40, "mpguk", "l100km")]}


def examples(cat: str) -> list[tuple[float, str, float, str]]:
    return [(v, a, convert(v, a, b), b) for v, a, b in _EXAMPLES.get(cat, [])]


# ---------- időzónák ----------

_COMMON = ["UTC", "Europe/London", "Europe/Budapest", "Europe/Berlin", "Europe/Paris", "Europe/Madrid", "Europe/Rome",
           "Europe/Warsaw", "Europe/Bucharest", "Europe/Athens", "Europe/Kyiv", "Europe/Istanbul", "Europe/Moscow",
           "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Toronto",
           "America/Mexico_City", "America/Sao_Paulo", "America/Buenos_Aires", "Africa/Cairo", "Africa/Johannesburg",
           "Africa/Lagos", "Asia/Dubai", "Asia/Karachi", "Asia/Kolkata", "Asia/Bangkok", "Asia/Singapore", "Asia/Shanghai",
           "Asia/Hong_Kong", "Asia/Seoul", "Asia/Tokyo", "Australia/Sydney", "Australia/Perth", "Pacific/Auckland",
           "Pacific/Honolulu"]
_ABBR = {"cet": "Europe/Budapest", "cest": "Europe/Budapest", "gmt": "Europe/London", "bst": "Europe/London",
         "est": "America/New_York", "edt": "America/New_York", "cst": "America/Chicago", "pst": "America/Los_Angeles",
         "pdt": "America/Los_Angeles", "mst": "America/Denver", "jst": "Asia/Tokyo", "ist": "Asia/Kolkata",
         "aest": "Australia/Sydney", "utc": "UTC", "z": "UTC"}


def common_zones() -> list[str]:
    return list(_COMMON)


def zone(name: str):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    key = _ABBR.get((name or "").strip().lower(), (name or "").strip())
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError):
        raise UnitError(f"unknown time zone: {name}")


def tz_convert(when: Optional[str], src: str, dsts: list[str]) -> dict:
    """Egy időpont (ISO vagy 'YYYY-MM-DD HH:MM'; üres = most) a forrás-zónából a célzónákba."""
    z = zone(src)
    if when:
        try:
            dt = datetime.fromisoformat(when.strip().replace("Z", "+00:00").replace(" ", "T", 1))
        except ValueError:
            raise UnitError("time: use YYYY-MM-DD HH:MM")
        dt = dt.replace(tzinfo=z) if dt.tzinfo is None else dt.astimezone(z)
    else:
        dt = datetime.now(z)
    out = []
    for d in dsts[:24]:
        zz = zone(d)
        x = dt.astimezone(zz)
        off = x.utcoffset()
        mins = int(off.total_seconds() // 60) if off else 0
        out.append({"zone": str(zz.key), "time": x.isoformat(timespec="minutes"),
                    "offset": f"UTC{'+' if mins >= 0 else '-'}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}",
                    "abbr": x.tzname(), "dst": bool(x.dst())})
    return {"from": {"zone": str(z.key), "time": dt.isoformat(timespec="minutes")}, "to": out}
