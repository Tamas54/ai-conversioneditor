"""Segédek fájl nélkül: mértékegység- és időzóna-átváltás (app/catalog/units.py)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.catalog import units as U
from app.convert.options import OptionError


async def units(inputs: list[Path], o: dict[str, Any], out: Path) -> dict[str, Any]:
    if o.get("value") is None or not o.get("from_unit") or not o.get("to_unit"):
        raise OptionError("units: give value, from_unit and to_unit")
    try:
        a, b = U.find(o["from_unit"]), U.find(o["to_unit"])
        r = U.convert(o["value"], o["from_unit"], o["to_unit"])
    except U.UnitError as e:
        raise OptionError(str(e))
    return {"files": [], "data": {"value": o["value"], "from": a.key, "to": b.key, "result": r, "category": a.category}}


async def timezones(inputs: list[Path], o: dict[str, Any], out: Path) -> dict[str, Any]:
    zones = [str(z) for z in (o.get("to_zones") or ["UTC"])]
    try:
        return {"files": [], "data": U.tz_convert(o.get("time"), o.get("from_zone") or "UTC", zones)}
    except U.UnitError as e:
        raise OptionError(str(e))
