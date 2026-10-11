"""Csomagok, kreditek, havi keretek és Stripe (Checkout + ügyfélportál + webhook).

Árazás (10-11, a mért önköltségre): 1 kredit ≈ €0,10. Kreditbe az kerül, amibe kép kerül (FLUX/Kontext 3–4 ¢);
a szöveges AI (DeepSeek, ~0,1 ¢/hívás) az előfizetésben „fair use”, az ingyenes fióknak havi keret.
Stripe-termékeket NEM kell előre felvenni: a Checkout price_data-val dolgozik. Kulcs nélkül a fizetés
„hamarosan” állapotú, minden más (keretek, kreditek) működik.

Beállítás: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET (Stripe → Developers → Webhooks →
<PUBLIC_BASE_URL>/api/stripe/webhook; események: checkout.session.completed, invoice.paid,
customer.subscription.updated, customer.subscription.deleted).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request

from app import tenancy
from app.config import settings

log = logging.getLogger("aice.billing")
router = APIRouter()

CURRENCY = os.getenv("BILLING_CURRENCY", "eur")
STRIPE_KEY = os.getenv("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

# havi keretek: deck · revise · document · photo_edit (fotó/hó) · scan_pages · form_pages · batch (fotó/kérés)
PLANS: dict[str, dict[str, Any]] = {
    "free": {"name": "Free", "monthly_credits": 10,
             "quota": {"deck": 3, "revise": 20, "document": 10, "photo_edit": 10, "scan_pages": 100, "form_pages": 20},
             "batch": 10, "retention_days": 30},
    "plus": {"name": "Plus", "month": 9, "year": 90, "monthly_credits": 150,
             "quota": {"deck": 300, "revise": 3000, "document": 1000, "photo_edit": 1500, "scan_pages": 1000, "form_pages": 500},
             "batch": 50, "retention_days": 90},
    "pro": {"name": "Pro", "month": 19, "year": 190, "monthly_credits": 400,
            "quota": {"deck": 1500, "revise": 15000, "document": 5000, "photo_edit": 8000, "scan_pages": 5000, "form_pages": 2500},
            "batch": 200, "retention_days": 365},
}
PACKS = {"pack100": {"credits": 100, "price": 10}, "pack300": {"credits": 300, "price": 25}}
SCAN_PAGES_PER_CREDIT = 20          # a havi oldalkeret felett


def enabled() -> bool:
    return bool(STRIPE_KEY)


def month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


# ---------- keretek ----------

async def plan_of(uid: str) -> str:
    from app import db
    if uid == tenancy.ADMIN_ID or not db.enabled():
        return "pro"
    u = await db.user_get(uid)
    return (u or {}).get("plan") or "free"


async def use(kind: str, n: int = 1) -> None:
    """Havi keret fogyasztása (deck, revise, document, photo_edit, form_pages). Túllépés → 402 + javaslat."""
    from app import db
    uid = tenancy.current_user_id()
    if uid == tenancy.ADMIN_ID or tenancy.is_guest(uid) or not db.enabled():
        return
    plan = await plan_of(uid)
    limit = PLANS[plan]["quota"].get(kind)
    if limit is None:
        return
    used = await db.usage_add(uid, month_key(), kind, n, limit)
    if used is None:
        nice = {"deck": "presentations", "revise": "changes", "document": "AI documents", "photo_edit": "photo edits",
                "form_pages": "form pages", "scan_pages": "scanned pages"}.get(kind, kind)
        raise HTTPException(402, f"You've used this month's {limit} {nice} on the {PLANS[plan]['name']} plan. "
                                 "Upgrade for more — see Plans.")


async def scan_pages(n: int) -> int:
    """Scan to text: a havi oldalkeretből, felette 20 oldalanként 1 kredit. Vissza: levont kredit."""
    from app import db
    uid = tenancy.current_user_id()
    if uid == tenancy.ADMIN_ID or not db.enabled() or tenancy.is_guest(uid):
        return 0
    plan = await plan_of(uid)
    limit = PLANS[plan]["quota"]["scan_pages"]
    used = await db.usage_get(uid, month_key(), "scan_pages")
    free_left = max(0, limit - used)
    over = max(0, n - free_left)
    await db.usage_add(uid, month_key(), "scan_pages", n, None)
    credits = -(-over // SCAN_PAGES_PER_CREDIT)
    if credits:
        await db.credits_spend(uid, credits, "scan pages over the monthly allowance", ref=f"{over} pages")
    return credits


async def monthly_topup(uid: str) -> None:
    """Ingyenes fiók: hónaponként feltöltés legalább 10 kreditre (nem halmozódik). A fizetős csomag havi
    kreditje a Stripe invoice.paid eseményére jön."""
    from app import db
    if uid == tenancy.ADMIN_ID or tenancy.is_guest(uid) or not db.enabled():
        return
    await db.free_topup(uid, month_key(), PLANS["free"]["monthly_credits"])


def batch_limit(plan: str) -> int:
    return PLANS.get(plan, PLANS["free"])["batch"]


# ---------- Stripe ----------

def _form(d: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Beágyazott dict → Stripe form-kódolás (line_items[0][price_data][currency]=eur …)."""
    out: list[tuple[str, str]] = []
    for k, v in d.items():
        key = f"{prefix}[{k}]" if prefix else k
        if isinstance(v, dict):
            out += _form(v, key)
        elif isinstance(v, list):
            for i, x in enumerate(v):
                out += _form(x, f"{key}[{i}]") if isinstance(x, dict) else [(f"{key}[{i}]", str(x))]
        elif isinstance(v, bool):
            out.append((key, "true" if v else "false"))
        elif v is not None:
            out.append((key, str(v)))
    return out


async def _stripe(method: str, path: str, data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request(method, f"https://api.stripe.com/v1/{path}", auth=(STRIPE_KEY, ""),
                            data=_form(data) if data else None)
    body = r.json()
    if r.status_code >= 400:
        log.warning("Stripe %s %s: %s", method, path, body.get("error", {}).get("message"))
        raise HTTPException(502, "The payment provider refused the request: " + str(body.get("error", {}).get("message", ""))[:200])
    return body


def _line_item(item: str, interval: Optional[str]) -> tuple[dict[str, Any], str]:
    base = settings.PRODUCT_NAME
    if item in PACKS:
        p = PACKS[item]
        return ({"quantity": 1, "price_data": {"currency": CURRENCY, "unit_amount": p["price"] * 100,
                                                "product_data": {"name": f"{base} — {p['credits']} credits"}}}, "payment")
    if item in ("plus", "pro"):
        plan = PLANS[item]
        iv = "year" if interval == "year" else "month"
        return ({"quantity": 1, "price_data": {"currency": CURRENCY, "unit_amount": plan[iv] * 100, "recurring": {"interval": iv},
                                                "product_data": {"name": f"{base} {plan['name']}"}}}, "subscription")
    raise HTTPException(400, "Unknown plan or pack.")


async def checkout(uid: str, email: Optional[str], item: str, interval: Optional[str]) -> str:
    if not enabled():
        raise HTTPException(503, "Payments aren't switched on yet — soon.")
    line, mode = _line_item(item, interval)
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    data: dict[str, Any] = {
        "mode": mode, "line_items": [line], "client_reference_id": uid,
        "success_url": f"{base}/app/plans?paid=1", "cancel_url": f"{base}/app/plans",
        "metadata": {"uid": uid, "item": item, "interval": interval or ""},
        "allow_promotion_codes": True, "automatic_tax": {"enabled": os.getenv("STRIPE_TAX", "") == "1"},
    }
    from app import db
    u = await db.user_get(uid)
    if u and u.get("stripe_customer_id"):
        data["customer"] = u["stripe_customer_id"]
    elif email:
        data["customer_email"] = email
    if mode == "subscription":
        data["subscription_data"] = {"metadata": {"uid": uid, "item": item}}
    else:
        data["customer_creation"] = "always"
        data["payment_intent_data"] = {"metadata": {"uid": uid, "item": item}}
    s = await _stripe("POST", "checkout/sessions", data)
    return s["url"]


async def portal(uid: str) -> str:
    from app import db
    if not enabled():
        raise HTTPException(503, "Payments aren't switched on yet — soon.")
    u = await db.user_get(uid)
    if not u or not u.get("stripe_customer_id"):
        raise HTTPException(400, "No subscription yet.")
    s = await _stripe("POST", "billing_portal/sessions",
                      {"customer": u["stripe_customer_id"], "return_url": settings.PUBLIC_BASE_URL.rstrip("/") + "/app/plans"})
    return s["url"]


def verify(payload: bytes, header: str, secret: str, tolerance: int = 300) -> dict[str, Any]:
    """Stripe-Signature: t=…,v1=… — HMAC-SHA256(secret, f"{t}.{payload}")."""
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    t, sig = parts.get("t", ""), [v for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p) if k == "v1"]
    if not t or not sig:
        raise ValueError("missing signature")
    expected = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in sig):
        raise ValueError("bad signature")
    if abs(time.time() - int(t)) > tolerance:
        raise ValueError("stale signature")
    return json.loads(payload)


async def handle(event: dict[str, Any]) -> str:
    """Webhook-esemény → csomag / kredit. Idempotens (stripe_events tábla)."""
    from app import db
    if await db.stripe_event_seen(event["id"]):
        return "duplicate"
    result = await _apply(event)
    await db.stripe_event_done(event["id"], event["type"])
    return result


async def _apply(event: dict[str, Any]) -> str:
    from app import db
    obj = event["data"]["object"]
    kind = event["type"]
    if kind == "checkout.session.completed":
        uid = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("uid")
        item = (obj.get("metadata") or {}).get("item")
        if not uid:
            return "no uid"
        await db.user_set_billing(uid, customer=obj.get("customer"))
        if item in PACKS and obj.get("payment_status") == "paid":
            await db.credits_add(uid, PACKS[item]["credits"], f"bought {item}", ref=obj.get("id"))
            return f"pack {item}"
        if item in ("plus", "pro"):
            await db.user_set_billing(uid, plan=item, subscription=obj.get("subscription"))
            return f"plan {item}"           # a havi kredit az invoice.paid-re jön (az első számla is)
        return "ignored"
    if kind == "invoice.paid":
        sub = obj.get("subscription") or (obj.get("parent") or {}).get("subscription_details", {}).get("subscription")
        u = await db.user_by_subscription(sub, obj.get("customer"))
        if not u:
            return "no user"
        plan = u.get("plan") if u.get("plan") in ("plus", "pro") else \
            ((obj.get("subscription_details") or {}).get("metadata") or {}).get("item") or "plus"
        await db.user_set_billing(u["id"], plan=plan, subscription=sub)
        await db.credits_add(u["id"], PLANS[plan]["monthly_credits"], f"{plan} monthly credits", ref=obj.get("id"))
        return f"credits {plan}"
    if kind in ("customer.subscription.deleted",) or (kind == "customer.subscription.updated" and obj.get("status") in ("canceled", "unpaid")):
        u = await db.user_by_subscription(obj.get("id"), obj.get("customer"))
        if u:
            await db.user_set_billing(u["id"], plan="free", subscription="")
            return "downgraded"
        return "no user"
    return "ignored"


# ---------- végpontok ----------

@router.get("/api/billing/plans")
async def plans() -> dict[str, Any]:
    return {"enabled": enabled(), "currency": CURRENCY, "credit_eur": 0.10,
            "plans": {k: {kk: vv for kk, vv in v.items()} for k, v in PLANS.items()}, "packs": PACKS,
            "costs": {"ai_image": settings.IMAGE_CREDIT_COST, "restore": 2, "colourise": 1,
                      "scan_pages_per_credit": SCAN_PAGES_PER_CREDIT}}


@router.post("/api/billing/checkout")
async def api_checkout(body: dict[str, Any]) -> dict[str, Any]:
    from app import db
    uid = tenancy.current_user_id()
    if tenancy.is_guest(uid):
        raise HTTPException(401, "Create a free account first.")
    u = await db.user_get(uid) if db.enabled() else None
    return {"url": await checkout(uid, (u or {}).get("email"), body.get("item") or "", body.get("interval"))}


@router.post("/api/billing/portal")
async def api_portal() -> dict[str, Any]:
    return {"url": await portal(tenancy.current_user_id())}


@router.post("/api/stripe/webhook", include_in_schema=False)
async def stripe_webhook(request: Request) -> dict[str, Any]:
    if not WEBHOOK_SECRET:
        raise HTTPException(503, "webhook not configured")
    payload = await request.body()
    try:
        event = verify(payload, request.headers.get("stripe-signature", ""), WEBHOOK_SECRET)
    except Exception as e:
        log.warning("Stripe webhook elutasítva: %s", e)
        raise HTTPException(400, "bad signature")
    result = await handle(event)
    log.info("Stripe %s %s → %s", event.get("type"), event.get("id"), result)
    return {"ok": True, "result": result}
