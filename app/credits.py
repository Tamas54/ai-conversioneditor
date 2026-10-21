"""Kreditkönyv és árazás (Postgres; OMLETT 2.3).

    credit_ledger   minden egyenlegváltozás egy sor: kind = grant | purchase | hold | capture | release | refund | adjust |
                    admin_usage; operation, provider, provider_task_id, provider_cost, idempotency_key (UNIQUE), meta
    users.credits   a gyorsítótárazott egyenleg (mindenki ezt olvassa) — UGYANABBAN a tranzakcióban írva, a felhasználó
                    sorának zárolásával (SELECT … FOR UPDATE); így SUM(delta) = users.credits (a napi egyeztetés ellenőrzi)
    pricing_rules   operation + model + tier → unit (second | job | image), provider_cost, multiplier, min_charge;
                    ár = ceil(provider_cost × egység × multiplier), legalább min_charge; adminból szerkeszthető

Külső (fizetős) művelet: becslés → hold a maximumra (−H) → szolgáltatói feladat → siker: release (+H) és capture a
tényleges költséggel (−A, A ≤ H), hiba: csak a release. A kulcsok (hold:/release:/capture:<op>) miatt a webhook és a
lekérdezés versenye sem számol kétszer. Az admin mentes, de a használata naplózva (admin_usage, delta 0).
Helyi CPU-művelet (render, merge, effekt) alapból 0 — egy pricing_rules-sor bármelyiket árazhatja (MCP: mcp:<tool>[.<action>]).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, Optional

from app import db, tenancy

log = logging.getLogger("aice.credits")

KINDS = ("grant", "purchase", "hold", "capture", "release", "refund", "adjust", "admin_usage")
UNITS = ("second", "job", "image")
InsufficientCredits = db.InsufficientCredits

# egy MCP-hívás (vagy webes kérés) alatt a könyvbe írt mozgások: a burkoló az eredménybe teszi (studio_ai)
_box: ContextVar[Optional[dict[str, Any]]] = ContextVar("aice_credit_box", default=None)


def _note(uid: str, delta: int, kind: str, balance: Optional[int]) -> None:
    box = _box.get()
    if box is None or box.get("uid") != uid:
        return
    if kind in ("hold",):
        box["held"] += -delta
    elif kind == "release":
        box["held"] -= delta
    elif kind != "admin_usage":
        box["used"] += -delta
    box["balance"] = balance


# ---------- a könyv ----------

async def apply(uid: str, delta: int, kind: str, reason: str, *, ref: Optional[str] = None, operation: Optional[str] = None,
                provider: Optional[str] = None, provider_task_id: Optional[str] = None, provider_cost: Any = None,
                idempotency_key: Optional[str] = None, meta: Optional[dict[str, Any]] = None,
                workspace_id: Optional[str] = None, top_up_to: Optional[int] = None, paid_only: bool = False) -> dict[str, Any]:
    """Egy könyvsor + az egyenleg, egy tranzakcióban, a felhasználó sorát zárolva. Ugyanaz az idempotency_key másodszor
    nem ír (duplicate=True). Levonás fedezet nélkül → InsufficientCredits (needed, available). top_up_to: a delta a sorzár
    alatt számolva „legalább ennyire” (ingyenes havi feltöltés). paid_only: a levonás csak fizetett kreditből (PaidCreditsOnly).
    Vissza: {balance, delta, id, duplicate}."""
    res = await apply_many(uid, [dict(delta=delta, kind=kind, reason=reason, ref=ref, operation=operation, provider=provider,
                                      provider_task_id=provider_task_id, provider_cost=provider_cost,
                                      idempotency_key=idempotency_key, meta=meta, workspace_id=workspace_id,
                                      top_up_to=top_up_to, paid_only=paid_only)])
    return res[0]


async def apply_many(uid: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Több könyvsor ugyanazon a zároláson belül (pl. release + capture): vagy mind, vagy egyik sem."""
    if kind_bad := [e["kind"] for e in entries if e["kind"] not in KINDS]:
        raise ValueError(f"unknown ledger kind: {kind_bad}")
    from psycopg.rows import dict_row
    out: list[dict[str, Any]] = []
    async with db._pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT credits FROM users WHERE id = %s FOR UPDATE", (uid,))
                row = await cur.fetchone()
                if not row:                    # vendég / ismeretlen fiók: levonásnál „nincs fedezet” (mint régen)
                    if any(int(e.get("delta") or 0) < 0 for e in entries):
                        raise InsufficientCredits(needed=-min(int(e.get("delta") or 0) for e in entries), available=0)
                    raise ValueError(f"no such user: {uid}")
                bal = start = int(row["credits"])
                for e in entries:
                    bal = await _book(cur, uid, bal, e, out)
                if bal != start:
                    await cur.execute("UPDATE users SET credits = %s WHERE id = %s", (bal, uid))
    for r in out:
        if not r["duplicate"]:
            _note(uid, r["delta"], r["kind"], r["balance"])
    return out


async def _book(cur, uid: str, bal: int, e: dict[str, Any], out: list[dict[str, Any]]) -> int:
    """Egy könyvsor a zárolt egyenlegre (idempotencia-kulcs, fedezet) → az új egyenleg."""
    from psycopg.types.json import Jsonb
    key = e.get("idempotency_key")
    if key:
        await cur.execute("SELECT id FROM credit_ledger WHERE idempotency_key = %s", (key,))
        dup = await cur.fetchone()
        if dup:
            out.append({"balance": bal, "delta": 0, "id": dup["id"], "duplicate": True, "kind": e["kind"]})
            return bal
    delta = 0 if e["kind"] == "admin_usage" else int(e["delta"])
    if e.get("top_up_to") is not None:
        delta = max(0, int(e["top_up_to"]) - bal)
        if not delta:
            out.append({"balance": bal, "delta": 0, "id": None, "duplicate": False, "kind": e["kind"]})
            return bal
    if delta < 0 and bal + delta < 0:
        raise InsufficientCredits(needed=-delta, available=bal)
    if delta < 0 and e.get("paid_only"):
        left = await _paid_left(cur, uid)
        if -delta > left:
            raise PaidCreditsOnly(needed=-delta, available=max(0, min(left, bal)))
    bal += delta
    await cur.execute(
        """INSERT INTO credit_ledger (user_id, workspace_id, delta, balance_after, reason, ref, kind, operation,
                                      provider, provider_task_id, provider_cost, idempotency_key, meta)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (uid, e.get("workspace_id"), delta, bal, e.get("reason") or e["kind"], e.get("ref"), e["kind"],
         e.get("operation"), e.get("provider"), e.get("provider_task_id"), e.get("provider_cost"), key,
         Jsonb(e["meta"]) if e.get("meta") else None))
    out.append({"balance": bal, "delta": delta, "id": (await cur.fetchone())["id"], "duplicate": False, "kind": e["kind"]})
    return bal


# A külső AI-videó (kie / fal) csak FIZETETT kreditből (Kommandant 10-21: „semmilyen veszteség”): az ingyenes havi és a
# kezdő/ajándék kredit mögött nincs bevétel. Fizetett = a Stripe-ból jött jóváírás (kreditcsomag, előfizetés havi
# kreditje: idempotency_key „stripe:…”); ebből vonódik le minden eddigi PAID_ONLY_OPS-foglalás és -levonás.
PAID_ONLY_OPS = ("ai_clip", "enhance")


class PaidCreditsOnly(InsufficientCredits):
    def __init__(self, *, needed: int, available: int):
        super().__init__(f"paid_credits_only: AI video runs on paid credits only (free and welcome credits can't pay for it): "
                         f"this needs {needed} credits, you have {available} paid credits left.",
                         needed=needed, available=available)


async def _paid_left(cur, uid: str) -> int:
    await cur.execute(
        """SELECT COALESCE(sum(delta) FILTER (WHERE delta > 0 AND idempotency_key LIKE 'stripe:%%'), 0)
                  + COALESCE(sum(delta) FILTER (WHERE kind IN ('hold', 'release', 'capture') AND operation = ANY(%s)), 0) AS left
           FROM credit_ledger WHERE user_id = %s""", (list(PAID_ONLY_OPS), uid))
    return int((await cur.fetchone())["left"])


async def free_topup(uid: str, month: str, amount: int) -> int:
    """Ingyenes csomag: hónaponként egyszer feltöltés legalább `amount` kreditre — a hónap jelölése és a jóváírás EGY
    tranzakcióban, a sorzár alatt (közben levonás is jöhet). Vissza: a jóváírt összeg."""
    from psycopg.rows import dict_row
    out: list[dict[str, Any]] = []
    async with db._pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT credits FROM users WHERE id = %s AND plan = 'free' AND topup_month IS DISTINCT FROM %s "
                                  "FOR UPDATE", (uid, month))
                row = await cur.fetchone()
                if not row:
                    return 0
                bal = await _book(cur, uid, int(row["credits"]), dict(
                    delta=0, kind="grant", reason="free monthly credits", ref=month, top_up_to=amount,
                    idempotency_key=f"topup:{uid}:{month}"), out)
                await cur.execute("UPDATE users SET credits = %s, topup_month = %s WHERE id = %s", (bal, month, uid))
    for r in out:
        if not r["duplicate"]:
            _note(uid, r["delta"], r["kind"], r["balance"])
    return out[0]["delta"] if out else 0


async def balance(uid: str) -> Optional[int]:
    """None = korlátlan (admin) vagy adatbázis nélkül."""
    if uid == tenancy.ADMIN_ID or not db.enabled() or tenancy.is_guest(uid):
        return None
    u = await db.user_get(uid)
    return int((u or {}).get("credits") or 0)


async def history(uid: str, limit: int = 50, before: Optional[int] = None) -> list[dict[str, Any]]:
    """A felhasználó saját tranzakciói — a szolgáltatói költség, a szolgáltató feladat-azonosítója és a meta (becslés,
    szorzó) NEM: az a mi költségalapunk (az admin a /api/admin/credits/status-ban látja)."""
    return await db._rows(
        """SELECT id, created_at, kind, delta, balance_after, reason, operation
           FROM credit_ledger WHERE user_id = %s AND (%s::bigint IS NULL OR id < %s::bigint)
           ORDER BY id DESC LIMIT %s""", (uid, before, before, max(1, min(200, limit))))


PRIVATE_ESTIMATE_KEYS = ("provider_cost", "provider", "multiplier")


def public_estimate(est: dict[str, Any], uid: Optional[str] = None) -> dict[str, Any]:
    """Becslés a felhasználónak: a szolgáltatói költség (a mi költségalapunk) csak az adminnak."""
    if (uid or tenancy.current_user_id()) == tenancy.ADMIN_ID:
        return est
    return {k: v for k, v in est.items() if k not in PRIVATE_ESTIMATE_KEYS}


# ---------- fizetős művelet: hold → capture / release ----------

def _who(uid: str) -> str:
    if tenancy.is_guest(uid):
        from app.auth import ACCOUNT_NEEDED
        raise PermissionError(ACCOUNT_NEEDED)
    return "admin" if uid == tenancy.ADMIN_ID else "user"


async def hold(uid: str, amount: int, op_id: str, *, operation: str, provider: Optional[str] = None,
               meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """A becsült maximum lefoglalása (−amount). Admin: nem foglal (a használata a settle-nél kerül a könyvbe). A PAID_ONLY_OPS
    műveletei csak fizetett kreditből foglalhatnak (PaidCreditsOnly)."""
    if _who(uid) == "admin" or not db.enabled() or amount <= 0:
        return {"balance": await balance(uid), "held": 0}
    r = await apply(uid, -int(amount), "hold", f"{operation} (reserved)", operation=operation, provider=provider,
                    idempotency_key=f"hold:{op_id}", meta=meta, paid_only=operation in PAID_ONLY_OPS)
    return {"balance": r["balance"], "held": int(amount) if not r["duplicate"] else 0}


async def settle(uid: str, op_id: str, held: int, charge: int, *, operation: str, provider: Optional[str] = None,
                 provider_task_id: Optional[str] = None, provider_cost: Any = None,
                 meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Siker: a foglalás feloldása (+held) és a tényleges ár levonása (−charge, legfeljebb a foglalt összeg) EGY
    tranzakcióban. Hiba: charge=0 → csak a feloldás. Admin: egy admin_usage sor (delta 0) a „kiszámlázandó” összeggel."""
    if _who(uid) == "admin":
        if db.enabled():
            await apply(uid, 0, "admin_usage", operation, operation=operation, provider=provider,
                        provider_task_id=provider_task_id, provider_cost=provider_cost, idempotency_key=f"admin:{op_id}",
                        meta={**(meta or {}), "would_charge": max(0, int(charge))})
        return {"balance": None, "charged": 0}
    charge = max(0, min(int(charge), int(held)))      # sosem több, mint amit foglaltunk (foglalás nélkül: semmi)
    if not db.enabled():
        return {"balance": None, "charged": 0}
    entries = []
    if held:
        entries.append(dict(delta=int(held), kind="release", reason=f"{operation} (reservation released)", operation=operation,
                            provider=provider, provider_task_id=provider_task_id, idempotency_key=f"release:{op_id}"))
    if charge:
        entries.append(dict(delta=-charge, kind="capture", reason=operation, operation=operation, provider=provider,
                            provider_task_id=provider_task_id, provider_cost=provider_cost,
                            idempotency_key=f"capture:{op_id}", meta=meta))
    elif provider_cost is not None and entries:
        entries[0]["provider_cost"] = provider_cost          # hibás feladat: a szolgáltató költsége (0) a feloldáson
    if not entries:
        return {"balance": await balance(uid), "charged": 0}
    res = await apply_many(uid, entries)
    return {"balance": res[-1]["balance"], "charged": charge if not res[-1]["duplicate"] else 0}


async def release(uid: str, op_id: str, held: int, *, operation: str, provider: Optional[str] = None,
                  provider_task_id: Optional[str] = None, provider_cost: Any = None,
                  meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return await settle(uid, op_id, held, 0, operation=operation, provider=provider, provider_task_id=provider_task_id,
                        provider_cost=provider_cost, meta=meta)


# ---------- árazás ----------

_rules_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])
_RULES_TTL_S = 30

# Veszteség soha (Kommandant 10-21): minden külső (kie / fal) ár = a szolgáltató költsége × MARKUP ÷ a legkevesebb, ami egy
# eladott krediten nálunk marad (credit_floor_eur: minden csomag és kreditcsomag közül a legrosszabb, a legmagasabb ÁFÁ-val
# és a legdrágább kártyadíjjal). Így a legolcsóbban eladott kreditből vett AI-n is legalább MARKUP-szoros a fedezet. A sor
# szorzóját az admin emelheti, ez alá nem mehet (price() is ide emel); a gépi invariáns: tests/test_pricing_floor.py.
MARKUP = 2.0
USD_EUR = 0.95                      # tartalék az euró gyengülésére (EKB 2026-10-21: 0,89)
VAT_MAX = 0.27                      # a legmagasabb uniós ÁFA (HU) — az áraink bruttók
CARD_FEE = (0.0325, 0.25)           # Stripe, a legdrágább (nem EGT-s) kártya: 3,25% + €0,25 terhelésenként
USD_PER_UNIT = {"kie": 0.005, "fal": 1.0}       # a szolgáltató egysége dollárban (1 kie-kredit = $0.005; a fal dollárban)


def credit_floor_eur() -> float:
    """A legkevesebb euró, ami egy eladott kreditből ÁFA és kártyadíj után nálunk marad: a havi és az éves csomagok (az
    éves 12 havi kerettel) és a kreditcsomagok közül a legrosszabb — a csomagárakból számolva, hogy kövesse a változást."""
    from app import billing

    def net(gross: float) -> float:
        return gross / (1 + VAT_MAX) - (gross * CARD_FEE[0] + CARD_FEE[1])
    per = [net(p["month"]) / p["monthly_credits"] for p in billing.PLANS.values() if p.get("month")]
    per += [net(p["year"]) / (12 * p["monthly_credits"]) for p in billing.PLANS.values() if p.get("year")]
    per += [net(p["price"]) / p["credits"] for p in billing.PACKS.values()]
    return min(per)


def provider_of(rule: dict[str, Any]) -> Optional[str]:
    """A szabály szolgáltatója — ha a sorból hiányzik (kézzel / adminból mentve), a modell nevéből (kie / fal árlap):
    a padló így sem kerülhető meg."""
    if rule.get("provider"):
        return rule["provider"]
    from app.providers import fal, kie
    model = rule.get("model") or ""
    return "kie" if model in kie.MODELS else "fal" if model in fal.MODELS else None


def floor_multiplier(provider: Optional[str]) -> float:
    """Studio-kredit / szolgáltatói egység, ami alá egy külső ár nem mehet (0: nem külső szolgáltató)."""
    usd = USD_PER_UNIT.get(provider or "")
    return usd * USD_EUR * MARKUP / credit_floor_eur() if usd else 0.0


def default_rules() -> list[dict[str, Any]]:
    """A szolgáltatói modellek induló árai (a kie és a fal ellenőrzött árlapjából: app/providers/kie.py és fal.py PRICES),
    a szorzó a padló (floor_multiplier)."""
    from app.providers import fal, kie
    return [{"operation": op, "model": model, "tier": tier, "unit": unit, "provider": name, "provider_cost": cost,
             "multiplier": floor_multiplier(name), "min_charge": 1, "note": note}
            for name, prices in (("kie", kie.PRICES), ("fal", fal.PRICES))
            for op, model, tier, unit, cost, note in prices]


def list_price(provider: str, cost: float, units: float = 1) -> int:
    """Egy rögzített (nem adminból szerkesztett) külső ár, pl. egy AI-kép: ugyanaz a képlet, mint a szabályoké."""
    return price({"provider": provider, "provider_cost": cost, "multiplier": floor_multiplier(provider), "min_charge": 1}, units)


def _num(v: Any) -> float:
    return float(v or 0)


async def seed_rules(rules: Optional[list[dict[str, Any]]] = None) -> int:
    """Induló árak: ami még nincs, létrejön; a meglévő sor szorzója és szolgáltatói költsége legalább a kódbeli lesz (az admin
    emelése megmarad, a padló alatti ár nem). A kódból kikerült kie / fal modellek sorai sírkövet kapnak (pl. a Seedance).
    Vissza: hány sor jött létre."""
    if not db.enabled():
        return 0
    n = 0
    defaults = rules is None
    for r in default_rules() if defaults else rules:
        got = await db._rows(
            """INSERT INTO pricing_rules (operation, model, tier, unit, provider, provider_cost, multiplier, min_charge, note)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (operation, model, tier) DO UPDATE
                 SET multiplier = GREATEST(pricing_rules.multiplier, EXCLUDED.multiplier),
                     provider_cost = GREATEST(pricing_rules.provider_cost, EXCLUDED.provider_cost), updated_at = now()
                 WHERE pricing_rules.multiplier < EXCLUDED.multiplier OR pricing_rules.provider_cost < EXCLUDED.provider_cost
               RETURNING (xmax = 0) AS inserted""",
            (r["operation"], r.get("model") or "", r.get("tier") or "", r.get("unit") or "job", r.get("provider"),
             r.get("provider_cost") or 0, r.get("multiplier") if r.get("multiplier") is not None else 1,
             int(r.get("min_charge") or 0), r.get("note")))
        n += sum(1 for g in got if g["inserted"])
    if defaults:
        from app.providers import fal, kie
        await db._rows("""UPDATE pricing_rules SET deleted_at = now(), updated_at = now()
                          WHERE provider IN ('kie', 'fal') AND deleted_at IS NULL AND NOT (model = ANY(%s)) RETURNING id""",
                       ([m for prices in (kie.PRICES, fal.PRICES) for _, m, *_ in prices],))
    _forget_rules()
    return n


def _forget_rules() -> None:
    global _rules_cache
    _rules_cache = (0.0, [])


_seeded: set[int] = set()


async def rules() -> list[dict[str, Any]]:
    global _rules_cache
    if not db.enabled():
        return []
    if time.monotonic() - _rules_cache[0] < _RULES_TTL_S:
        return _rules_cache[1]
    if id(db._pool) not in _seeded:                  # az induló árak az első olvasáskor (a lifespan is meghívja)
        _seeded.add(id(db._pool))
        await seed_rules()
    rows = await db._rows("SELECT * FROM pricing_rules WHERE deleted_at IS NULL ORDER BY operation, model, tier")
    _rules_cache = (time.monotonic(), rows)
    return rows


async def rule_for(operation: str, model: str = "", tier: str = "") -> Optional[dict[str, Any]]:
    """A legpontosabb szabály: (művelet, modell, szint) → (művelet, modell) → (művelet)."""
    rs = await rules()
    for m, t in ((model, tier), (model, ""), ("", "")):
        hit = next((r for r in rs if r["operation"] == operation and r["model"] == (m or "") and r["tier"] == (t or "")), None)
        if hit:
            return hit
    return None


def price(rule: Optional[dict[str, Any]], units: float = 1, provider_units: Optional[float] = None) -> int:
    """Studio-kredit: ceil(provider_cost × egység × multiplier), legalább min_charge (0 ár: 0, a min_charge sem számít); külső
    szolgáltatónál a szorzó legalább a padló (floor_multiplier) — az adminban elállított sor sem mehet veszteségbe.
    provider_units: a szolgáltató TÉNYLEGES költsége a saját egységében (pl. kie-kredit) — a capture ebből számol."""
    if not rule:
        return 0
    cost = _num(provider_units) if provider_units is not None else _num(rule["provider_cost"]) * max(0.0, float(units))
    raw = cost * max(_num(rule["multiplier"]), floor_multiplier(provider_of(rule)))
    if raw <= 0:
        return 0
    return max(int(rule.get("min_charge") or 0), math.ceil(raw - 1e-9))


async def estimate(operation: str, model: str = "", tier: str = "", units: float = 1) -> dict[str, Any]:
    r = await rule_for(operation, model, tier)
    return {"operation": operation, "model": model or None, "tier": tier or None, "unit": (r or {}).get("unit") or "job",
            "units": units, "credits": price(r, units),
            "provider_cost": round(_num((r or {}).get("provider_cost")) * units, 3) if r else 0,
            "provider": (r or {}).get("provider")}


async def rule_save(r: dict[str, Any]) -> dict[str, Any]:
    if r.get("unit") and r["unit"] not in UNITS:
        raise ValueError(f"unit: {' | '.join(UNITS)}")
    for k in ("provider_cost", "multiplier"):
        if r.get(k) is not None and float(r[k]) < 0:
            raise ValueError(f"{k} can't be negative")
    if not r.get("provider") and provider_of(r):
        r = {**r, "provider": provider_of(r)}            # egy kie / fal modell sora szolgáltató nélkül sem menthető
    fl = floor_multiplier(r.get("provider"))
    if fl and float(r["multiplier"] if r.get("multiplier") is not None else 1) < fl * (1 - 1e-9):   # numeric-kerekítés
        raise ValueError(f"multiplier: at least {fl:.4f} for {r['provider']} — below that the cheapest credit we sell "
                         f"(€{credit_floor_eur():.4f} net) wouldn't cover {MARKUP:g}× what the provider charges")
    rows = await db._rows(
        """INSERT INTO pricing_rules (operation, model, tier, unit, provider, provider_cost, multiplier, min_charge, note)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (operation, model, tier) DO UPDATE SET unit = EXCLUDED.unit, provider = EXCLUDED.provider,
             provider_cost = EXCLUDED.provider_cost, multiplier = EXCLUDED.multiplier, min_charge = EXCLUDED.min_charge,
             note = EXCLUDED.note, deleted_at = NULL, updated_at = now() RETURNING *""",
        (str(r["operation"]).strip(), (r.get("model") or "").strip(), (r.get("tier") or "").strip(), r.get("unit") or "job",
         r.get("provider") or None, r.get("provider_cost") or 0, r.get("multiplier") if r.get("multiplier") is not None else 1,
         int(r.get("min_charge") or 0), r.get("note")))
    _forget_rules()
    return rows[0]


async def rule_delete(rule_id: int) -> bool:
    """Sírkő (deleted_at), nem DELETE: az induló árak újra-seedelése így nem hozza vissza a törölt sort."""
    ok = bool(await db._rows("UPDATE pricing_rules SET deleted_at = now(), updated_at = now() WHERE id = %s AND deleted_at IS NULL "
                             "RETURNING id", (rule_id,)))
    _forget_rules()
    return ok


# ---------- MCP: minden eszközhívás ugyanazon az úton, mint a webes felület ----------

def call_operation(tool: str, args: dict[str, Any]) -> str:
    act = args.get("action")
    return f"mcp:{tool}" + (f".{str(act).lower()}" if isinstance(act, str) and act else "")


async def _call_price(op: str) -> int:
    try:
        r = await rule_for(op) or await rule_for(op.split(".", 1)[0])
    except Exception as e:                    # az árazás kiesése nem állítja meg a munkát (a helyi művelet úgyis 0)
        log.warning("árszabály nem olvasható (%s): %s", op, e)
        return 0
    return price(r, 1)


@asynccontextmanager
async def mcp_call(tool: str, args: dict[str, Any]):
    """Egy MCP-eszközhívás kreditútja (a token felhasználóján): az eszköz ára (pricing_rules: mcp:<tool>[.<action>], alapból
    0) a hívás ELŐTT foglalva — fedezet nélkül InsufficientCredits és az eszköz el sem indul —, sikerre levonva, hibára
    feloldva. A hívás alatt minden könyvmozgás (AI-lépés, külső feladat foglalása) a dobozba kerül: az eredmény studio_ai
    mezője ebből mondja meg a felhasznált kreditet és az egyenleget. Admin: nem fizet, a „kiszámlázandó” egy admin_usage sor."""
    uid = tenancy.current_user_id()
    box: dict[str, Any] = {"uid": uid, "used": 0, "held": 0, "balance": None, "would": 0, "op": call_operation(tool, args)}
    tok = _box.set(box)
    op_id, held = uuid.uuid4().hex, 0
    try:
        cost = await _call_price(box["op"]) if db.enabled() else 0
        if cost and uid == tenancy.ADMIN_ID:
            box["would"] = cost
        elif cost:
            held = (await hold(uid, cost, op_id, operation=box["op"]))["held"]
        try:
            yield box
        except BaseException:
            if held:
                await release(uid, op_id, held, operation=box["op"])
            raise
        if held:
            await settle(uid, op_id, held, cost, operation=box["op"])
    finally:
        _box.reset(tok)
    if uid == tenancy.ADMIN_ID and db.enabled() and (box["would"] or box.get("steps")):
        try:
            await apply(uid, 0, "admin_usage", box["op"], operation=box["op"], idempotency_key=f"admin:{op_id}",
                        meta={"would_charge": box["would"], "ai_steps": box.get("steps") or 0})
        except Exception as e:
            log.warning("admin-napló kimaradt (%s): %s", box["op"], e)


def insufficient_message(e: InsufficientCredits) -> str:
    """Egyértelmű, gépileg is olvasható hiba: insufficient_credits + mennyi kell és mennyi van."""
    from app.config import settings
    if e.needed is None:
        return str(e)
    if isinstance(e, PaidCreditsOnly):
        return (f"{e} Nothing was started. Buy a credit pack or subscribe (Plans: "
                f"{settings.PUBLIC_BASE_URL.rstrip('/')}/app/plans), or choose a cheaper option.")
    return (f"insufficient_credits: this needs {e.needed} credits, you have {e.available}. Nothing was started. Add credits "
            f"(Plans: {settings.PUBLIC_BASE_URL.rstrip('/')}/app/plans) or choose a cheaper option.")


# ---------- figyelmeztetések, napi egyeztetés ----------

async def notice(kind: str, message: str, meta: Optional[dict[str, Any]] = None, *, once_per_s: int = 86400) -> bool:
    """Admin-felületi figyelmeztetés (+ napló). Ugyanaz a fajta nyitva és frissebb, mint once_per_s → nem ismétli."""
    log.warning("ADMIN NOTICE %s: %s", kind, message)
    if not db.enabled():
        return False
    from psycopg.types.json import Jsonb
    rows = await db._rows("""INSERT INTO admin_notices (kind, message, meta)
                             SELECT %s, %s, %s WHERE NOT EXISTS (SELECT 1 FROM admin_notices WHERE kind = %s AND resolved_at IS NULL
                                                                 AND created_at > now() - make_interval(secs => %s))
                             RETURNING id""", (kind, message, Jsonb(meta or {}), kind, once_per_s))
    return bool(rows)


async def notices(open_only: bool = True, limit: int = 50) -> list[dict[str, Any]]:
    return await db._rows("SELECT * FROM admin_notices WHERE (%s = false OR resolved_at IS NULL) ORDER BY id DESC LIMIT %s",
                          (open_only, limit))


async def notice_resolve(nid: int) -> bool:
    return bool(await db._rows("UPDATE admin_notices SET resolved_at = now() WHERE id = %s AND resolved_at IS NULL RETURNING id",
                               (nid,)))


LOW_BALANCE = float(os.getenv("KIE_LOW_BALANCE", "2000"))     # kie-kredit (~$10) alatt riasztás
RECONCILE_INTERVAL_H = float(os.getenv("RECONCILE_INTERVAL_H", "24"))


async def reconcile() -> dict[str, Any]:
    """Napi egyeztetés: a szolgáltató egyenlege ↔ az előző pillanatkép mínusz a könyv provider-költségei azóta; alacsony
    egyenleg és eltérés → admin-figyelmeztetés. Plusz: kinek nem egyezik a users.credits a könyv összegével."""
    from app import providers
    out: dict[str, Any] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "providers": {}}
    for name, prov in providers.active().items():
        if not getattr(prov, "has_balance", True):      # a fal-nak nincs egyenleg-API-ja
            continue
        try:
            bal = float(await prov.balance())
        except Exception as e:
            await notice(f"{name}_balance_unknown", f"{name}: the balance could not be read ({str(e)[:160]}).")
            out["providers"][name] = {"error": str(e)[:200]}
            continue
        last = (await db._rows("SELECT * FROM provider_balance WHERE provider = %s ORDER BY id DESC LIMIT 1", (name,)) or [None])[0]
        cost = expected = drift = None
        if last:
            got = await db._rows("""SELECT COALESCE(sum(provider_cost), 0) AS c FROM credit_ledger
                                    WHERE provider = %s AND kind IN ('capture', 'admin_usage', 'release')
                                      AND created_at > %s::timestamptz""", (name, last["created_at"]))
            cost = float(got[0]["c"])
            expected = float(last["balance"]) - cost
            drift = round(expected - bal, 3)           # > 0: a szolgáltató többet vont le, mint amit a könyv tud
        await db._rows("INSERT INTO provider_balance (provider, balance, ledger_cost, expected, drift) VALUES (%s, %s, %s, %s, %s)",
                       (name, bal, cost, expected, drift))
        out["providers"][name] = {"balance": bal, "ledger_cost": cost, "expected": expected, "drift": drift}
        if bal < LOW_BALANCE:
            await notice(f"{name}_low_balance", f"{name} balance is low: {bal:g} credits (alert below {LOW_BALANCE:g}). "
                                                "Top up before AI video jobs start failing.", {"balance": bal})
        if drift is not None and abs(drift) > max(10.0, 0.02 * float(last["balance"])):
            await notice(f"{name}_drift", f"{name}: the balance moved {drift:+g} credits more than the Studio's ledger explains "
                                          "(another app using the same key, or a task we lost track of).",
                         {"drift": drift, "expected": expected, "balance": bal})
    bad = await db._rows("""SELECT u.id, u.credits, COALESCE(s.total, 0) AS ledger FROM users u
                            LEFT JOIN (SELECT user_id, sum(delta) AS total FROM credit_ledger GROUP BY user_id) s ON s.user_id = u.id
                            WHERE u.id <> 'admin' AND u.credits <> COALESCE(s.total, 0) LIMIT 20""")
    out["ledger_mismatch"] = bad
    if bad:
        await notice("ledger_mismatch", f"{len(bad)} account(s) have a balance that the credit ledger doesn't add up to "
                                        "(changed outside the app?).", {"users": [b["id"] for b in bad]})
    return out


async def _reconcile_loop() -> None:
    await asyncio.sleep(float(os.getenv("RECONCILE_FIRST_S", "300")))
    while True:
        try:
            async with db._pool.connection() as conn:          # több példány: egy egyeztet (a zár ugyanazon a kapcsolaton)
                ok = (await (await conn.execute("SELECT pg_try_advisory_lock(7351902)")).fetchone())[0]
                if ok:
                    try:
                        log.info("egyeztetés: %s", json.dumps(await reconcile(), default=str)[:500])
                    finally:
                        await conn.execute("SELECT pg_advisory_unlock(7351902)")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("egyeztetés hiba")
        await asyncio.sleep(RECONCILE_INTERVAL_H * 3600)


_tasks: list[asyncio.Task] = []


async def start() -> None:
    """Lifespan: induló árak, napi egyeztetés, a félbemaradt külső feladatok folytatása."""
    if not db.enabled():
        return
    try:
        await seed_rules()
    except Exception:
        log.exception("induló árak")
    _tasks.append(asyncio.create_task(_reconcile_loop()))
    # a félbemaradt AI-feladatok MINDIG folytatódnak (a kapcsoló kikapcsolása után is: a foglalás és a szolgáltató által
    # már kiszámlázott eredmény nem ragadhat be)
    from app.tools import ai_video
    _tasks.append(asyncio.create_task(ai_video.resume_loop()))


async def stop() -> None:
    for t in _tasks:
        t.cancel()
    _tasks.clear()
