"""Külső tényforrások a tartalmi ellenőrzéshez és az élő adatokhoz — mindkettő saját szolgáltatás.

    StatData (REST /api/call): makró-mutató országra a legfrissebb (vagy kért) időszakkal, forrással és URL-lel;
                               MNB-árfolyam (1 egység deviza = X forint)
    Echolot ENGINE (/v2/search, kulccsal): webes találatok kivonattal — a nem statisztikai állításokhoz

A hívó mindig szabványos alakot kap: {value, unit, period, source_name, source_url} ill. [{title, url, snippet, date}].
Hiba = kivétel (a hívó dönti el, hogy „nem tudtam ellenőrizni”-ként jelzi) — kitalált érték soha.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.config import settings

log = logging.getLogger("aice.datasources")

# a StatData get_macro_indicator mutatói (a többi = nem kötünk hozzá, webes ellenőrzés marad)
INDICATORS = {
    "cpi": "inflation (annual %)", "core_cpi": "core inflation (annual %)", "services_cpi": "services inflation (annual %)",
    "energy_cpi": "energy inflation (annual %)", "food_cpi": "food inflation (annual %)",
    "policy_rate": "central bank policy rate (%)", "unemployment": "unemployment rate (%)",
    "gdp_growth": "GDP growth, quarter on quarter (%)", "ppi": "producer prices (annual %)",
    "gov_debt": "government debt (% of GDP)", "house_prices": "house prices (annual %)",
    "bond_yield_10y": "10-year government bond yield (%)", "industrial_production": "industrial production index",
    "retail_trade": "retail trade volume index",
}
CURRENCIES = {"EUR", "USD", "GBP", "CHF", "JPY", "CZK", "PLN", "RON", "SEK", "NOK", "DKK", "AUD", "CAD", "CNY", "TRY"}


class SourceError(RuntimeError):
    pass


async def _statdata(tool: str, args: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{settings.STATDATA_URL.rstrip('/')}/api/call", json={"tool": tool, "args": args})
    try:
        d = r.json()
    except ValueError:
        raise SourceError(f"StatData answered HTTP {r.status_code}") from None
    if not d.get("ok"):
        raise SourceError(f"StatData: {str(d.get('error'))[:200]}")
    res = d.get("result")
    if isinstance(res, str):
        try:
            return json.loads(res)
        except ValueError:
            return {"text": res}
    return res


async def indicator(country: str, name: str, period: Optional[str] = None) -> dict[str, Any]:
    """Egy makró-mutató: {value, unit, period, source_name, source_url, status}. period: "YYYY-MM" | "YYYY-Qn" | None."""
    if name not in INDICATORS:
        raise SourceError(f"unknown indicator {name!r}")
    args: dict[str, Any] = {"country": (country or "").upper()[:2], "indicator": name}
    if period:
        args["period"] = period
    d = await _statdata("get_macro_indicator", args)
    if not isinstance(d, dict) or d.get("value") is None or d.get("status") in ("missing", "error"):
        if period:            # rossz/hiányzó időszak: a StatData szerint a kérdés újra, időszak nélkül
            raise SourceError(f"no {name} value for {args['country']} in {period}")
        raise SourceError(f"no {name} value for {args['country']}")
    return {"value": float(d["value"]), "unit": d.get("unit") or INDICATORS[name], "period": d.get("period"),
            "source_name": _source_name(d.get("source_used"), d.get("source_url")), "source_url": d.get("source_url"),
            "status": d.get("status")}


def _source_name(used: Optional[str], url: Optional[str]) -> str:
    """A StatData `source_used`-ja néha belső jelölés („scrape”, „brave_search”) — akkor a forrás-URL hosztja."""
    used = (used or "").strip()
    m = re.search(r"https?://\S+", used)
    if m:                                    # „scrape https://…” → a hoszt
        url, used = url or m.group(0), ""
    elif used and (any(c.isupper() for c in used) or " " in used) and "_" not in used.split()[0]:
        return used
    host = re.sub(r"^www\.", "", urlparse(url or "").netloc)
    return host or used or "StatData"


async def fx(currency: str) -> dict[str, Any]:
    """MNB hivatalos árfolyam: 1 egység deviza forintban."""
    cur = (currency or "").upper()
    d = await _statdata("mnb_rates", {"currencies": cur})
    rate = next((x.get("rate") for x in (d or {}).get("rates", []) if x.get("currency") == cur), None)
    if rate is None:
        raise SourceError(f"no MNB rate for {cur}")
    return {"value": float(rate), "unit": f"HUF per 1 {cur}", "period": d.get("date"),
            "source_name": "Magyar Nemzeti Bank (MNB)", "source_url": "https://www.mnb.hu/arfolyamok", "status": "fresh"}


async def fetch(spec: dict[str, Any]) -> dict[str, Any]:
    """Egy kötés forrása: {"kind": "indicator", country, indicator, period?} | {"kind": "fx", currency}."""
    kind = spec.get("kind")
    if kind == "indicator":
        return await indicator(spec.get("country", ""), spec.get("indicator", ""), spec.get("period") or None)
    if kind == "fx":
        return await fx(spec.get("currency", ""))
    raise SourceError(f"unknown source kind {kind!r}")


def describe(spec: dict[str, Any]) -> str:
    if spec.get("kind") == "indicator":
        return f"{spec.get('country', '').upper()} {INDICATORS.get(spec.get('indicator', ''), spec.get('indicator'))}" + (
            f", {spec['period']}" if spec.get("period") else ", latest")
    if spec.get("kind") == "fx":
        return f"{spec.get('currency', '').upper()}/HUF, MNB official rate"
    return str(spec)


def search_enabled() -> bool:
    return bool(settings.ECHOLOT_ENGINE_URL and settings.ECHOLOT_ENGINE_KEY)


# az ENGINE kulcsonként 2 egyidejű hívást enged (ENGINE_KEY_MAX_INFLIGHT), fölötte 429 — mérve 10-14: öt párhuzamos
# keresésből három 429 volt, a tényellenőrzés „nincs forrás”-t mondott. Ezért egyszerre kettő, 429-re várakozás.
_SEARCH_SEM = asyncio.Semaphore(2)


async def web_search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Webes találatok: [{title, url, snippet, date}] (Echolot ENGINE, Firecrawl v2 alak)."""
    if not search_enabled():
        raise SourceError("web search isn't set up on this server")
    async with _SEARCH_SEM:
        for attempt in range(4):
            try:
                async with httpx.AsyncClient(timeout=45) as c:
                    r = await c.post(f"{settings.ECHOLOT_ENGINE_URL}/v2/search", json={"query": query[:300], "limit": limit},
                                     headers={"Authorization": f"Bearer {settings.ECHOLOT_ENGINE_KEY}"})
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt == 3:
                    raise SourceError(f"web search didn't answer ({type(e).__name__})") from None
                continue
            if r.status_code != 429:
                break
            try:
                wait = float((r.json() or {}).get("retry_after") or r.headers.get("retry-after") or 2)
            except ValueError:
                wait = 2.0
            await asyncio.sleep(min(max(wait, 0.5), 15))
    if r.status_code >= 400:
        raise SourceError(f"web search answered HTTP {r.status_code}")
    data = (r.json() or {}).get("data") or {}
    web = data.get("web") if isinstance(data, dict) else data
    return [{"title": (x.get("title") or "")[:200], "url": x.get("url"), "snippet": (x.get("description") or x.get("snippet") or "")[:500],
             "date": x.get("date")} for x in (web or [])[:limit] if x.get("url")]
