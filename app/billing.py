"""Csomagok, kreditek, havi keretek és Stripe (Checkout + ügyfélportál + webhook).

Árazás (10-11, a mért önköltségre): 1 kredit ≈ €0,10. Kreditbe az kerül, amibe kép kerül (10-21 óta a kie.ai-n: FLUX.2,
Kontext; az ár a közös képletből, app/credits.py — veszteség soha);
a szöveges AI (DeepSeek, ~0,1 ¢/hívás) a webes felületen az előfizetésben „fair use”, az ingyenes fióknak havi keret.
MCP-n (ChatGPT, Claude… — a gondolkodás a kliens modelljén, a felhasználó előfizetésén fut) a szerver SAJÁT
modellhívása (képválasztás látómodellel, képkereső szavak, cikkből forgatókönyv, képleírás, tényellenőrzés…) egy
„AI-lépés”: havi keret, felette 20 lépésenként 1 kredit, előre (Kommandant 10-17: „ez is kredites legyen”). Ami már
fizet (szkennelt oldal, fotó-helyreállítás kredittel), annak a modellhívása nem számít külön (prepaid).
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
             "quota": {"deck": 3, "revise": 20, "document": 10, "photo_edit": 10, "scan_pages": 100, "form_pages": 20, "check": 40},
             "batch": 10, "retention_days": 30, "api_tasks": 250, "ai_steps": 60},
    "plus": {"name": "Plus", "month": 9, "year": 90, "monthly_credits": 150,
             "quota": {"deck": 300, "revise": 3000, "document": 1000, "photo_edit": 1500, "scan_pages": 1000, "form_pages": 500, "check": 3000},
             "batch": 50, "retention_days": 90, "api_tasks": 5000, "ai_steps": 1500},
    "pro": {"name": "Pro", "month": 19, "year": 190, "monthly_credits": 400,
            "quota": {"deck": 1500, "revise": 15000, "document": 5000, "photo_edit": 8000, "scan_pages": 5000, "form_pages": 2500, "check": 15000},
            "batch": 200, "retention_days": 365, "api_tasks": 25000, "ai_steps": 6000},
}
PACKS = {"pack100": {"credits": 100, "price": 10}, "pack300": {"credits": 300, "price": 25}}
SCAN_PAGES_PER_CREDIT = 20          # a havi oldalkeret felett
API_TASKS_PER_CREDIT = 20           # REST API: a havi feladatkeret felett 20 feladatonként 1 kredit
AI_STEPS_PER_CREDIT = 20            # MCP: a szerver saját modellhívásai a havi keret felett 20-anként 1 kredit


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
                "form_pages": "form pages", "scan_pages": "scanned pages",
                "check": "document checks"}.get(kind, kind)
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


async def _metered(uid: str, kind: str, n: int, per_credit: int, reason: str) -> int:
    """Havi keret (PLANS[plan][kind]), felette per_credit egységenként 1 kredit, a blokk ELEJÉN levonva (előre fizet,
    fedezet nélkül nem indul — InsufficientCredits). Vissza: levont kredit."""
    from app import db
    plan = await plan_of(uid)
    limit = PLANS[plan].get(kind, 0)
    before = await db.usage_get(uid, month_key(), kind)
    after = before + n
    over_before, over_after = max(0, before - limit), max(0, after - limit)
    blocks = -(-over_after // per_credit) - -(-over_before // per_credit)
    if blocks:
        await db.credits_spend(uid, blocks, reason, ref=f"{n} {kind.replace('_', ' ')}")
    await db.usage_add(uid, month_key(), kind, n, None)
    return blocks


async def api_task(uid: str, n: int = 1) -> int:
    """REST API: egy (vagy n) feldolgozó feladat (átalakítás, eszköz) a havi keretből; felette 20-anként 1 kredit,
    a 20-as blokk ELEJÉN levonva (így előre fizet, és fedezet nélkül nem indul). Vissza: levont kredit."""
    from app import db
    if uid == tenancy.ADMIN_ID or not db.enabled() or tenancy.is_guest(uid):
        return 0
    return await _metered(uid, "api_tasks", n, API_TASKS_PER_CREDIT, "API tasks over the monthly allowance")


# ---------- MCP: a szerver saját modellhívásai ----------

from contextlib import contextmanager  # noqa: E402
from contextvars import ContextVar  # noqa: E402

_prepaid: ContextVar[bool] = ContextVar("aice_ai_prepaid", default=False)
_steps: ContextVar[Optional[list]] = ContextVar("aice_ai_steps", default=None)   # [lépés, kredit] — a hívás eredményébe


@contextmanager
def prepaid():
    """A művelet már fizetett (oldalkeret, kredit) — a benne futó modellhívás nem AI-lépés."""
    tok = _prepaid.set(True)
    try:
        yield
    finally:
        _prepaid.reset(tok)


@contextmanager
def count_steps():
    """Egy MCP-hívás alatt elhasznált AI-lépések és kreditek (a burkoló teszi az eredménybe: studio_ai)."""
    box = [0, 0]
    tok = _steps.set(box)
    try:
        yield box
    finally:
        _steps.reset(tok)


class AIStepsExhausted(ValueError):
    """A havi AI-lépések elfogytak és nincs kredit — érthető üzenet, mit tehet a kliens maga."""


AI_STEPS_HOW = ("This step runs the Studio's own AI model (picking pictures, finding image search words, writing a "
                "script, describing an image, checking facts…). Add credits (Plans), or do that part yourself — you are "
                "an AI too: give each scene its own image (a URL or file_id) or \"image_query\" English words, pass a "
                "finished scenes=[…] plan, look at pictures with files(action=\"preview\"), and use the editing commands "
                "directly instead of edit_with_instruction.")


async def ai_step(n: int = 1) -> int:
    """MCP-ről indult munka közben a szerver saját modellhívása: havi keret (ai_steps), felette 20 lépésenként 1
    kredit. Webes használatban nem számol (ott a csomagkeretek a fair use), és nem számol, ami már fizetett."""
    from app import db
    if not tenancy.client_makes_images.get() or _prepaid.get():
        return 0
    uid = tenancy.current_user_id()
    box = _steps.get()
    if box is not None:
        box[0] += n
    if uid == tenancy.ADMIN_ID or tenancy.is_guest(uid) or not db.enabled():
        return 0
    plan = await plan_of(uid)
    try:
        spent = await _metered(uid, "ai_steps", n, AI_STEPS_PER_CREDIT, "Studio AI steps over the monthly allowance (MCP)")
    except db.InsufficientCredits:
        raise AIStepsExhausted(f"Your {PLANS[plan]['ai_steps']:,} Studio AI steps for this month are used up and there "
                               f"are no credits left (1 credit = {AI_STEPS_PER_CREDIT} more steps). " + AI_STEPS_HOW) from None
    if box is not None:
        box[1] += spent
    return spent


async def monthly_topup(uid: str) -> None:
    """Ingyenes fiók: hónaponként feltöltés legalább 10 kreditre (nem halmozódik). A havi előfizetés kreditje a Stripe
    invoice.paid eseményére jön; az ÉVES előfizetésé havonta (yearly_grant) — nem egyszer a számlával."""
    from app import db
    if uid == tenancy.ADMIN_ID or tenancy.is_guest(uid) or not db.enabled():
        return
    await db.free_topup(uid, month_key(), PLANS["free"]["monthly_credits"])
    await yearly_grant(uid)


async def yearly_grant(uid: str, user: Optional[dict[str, Any]] = None) -> int:
    """Éves előfizetés (Kommandant 10-21: „a korrektséget fenn kell tartani”): a meghirdetett havi keret minden hónapban
    jár, amíg az előfizetés él — a hónap kulcsával idempotensen (a számla hónapja és ez ugyanaz a kulcs). A kulcs
    „stripe:”-ral kezdődik: kifizetett kredit (credits.PAID_ONLY_OPS). Vissza: a jóváírt kredit (0, ha már megkapta)."""
    from app import credits, db
    u = user or await db.user_get(uid)
    if not u or u.get("plan") not in ("plus", "pro") or u.get("billing_interval") != "year" or not u.get("stripe_subscription_id"):
        return 0
    n = PLANS[u["plan"]]["monthly_credits"]
    r = await credits.apply(uid, n, "grant", f"{u['plan']} monthly credits (yearly plan)", ref=month_key(),
                            idempotency_key=f"stripe:yearly:{uid}:{month_key()}")
    return 0 if r["duplicate"] else n


async def yearly_grants() -> int:
    """Minden éves előfizető e havi kerete (a háttérhurok hívja; a webes fiók megnyitása is jóváírja). Vissza: hány fiók kapott."""
    from app import db
    n = 0
    for u in await db._rows("""SELECT id, plan, billing_interval, stripe_subscription_id FROM users
                               WHERE plan IN ('plus', 'pro') AND billing_interval = 'year' AND stripe_subscription_id IS NOT NULL"""):
        try:
            n += bool(await yearly_grant(u["id"], u))
        except Exception:
            log.exception("éves havi keret (%s)", u["id"])
    return n


async def yearly_loop() -> None:
    """Hathavonta: a hónap első napjain is legfeljebb néhány óra a késés. Több példány: az idempotencia-kulcs véd."""
    import asyncio
    while True:
        try:
            got = await yearly_grants()
            if got:
                log.info("éves előfizetők havi kerete: %d fiók", got)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("éves havi keret")
        await asyncio.sleep(6 * 3600)


def _invoice_interval(obj: dict[str, Any]) -> Optional[str]:
    """A számla tételének ciklusa (month | year) — a price.recurring vagy a régi plan mezőből."""
    for line in ((obj.get("lines") or {}).get("data") or []):
        iv = ((line.get("price") or {}).get("recurring") or {}).get("interval") or (line.get("plan") or {}).get("interval")
        if iv:
            return iv
    return None


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
            await db.credits_add(uid, PACKS[item]["credits"], f"bought {item}", ref=obj.get("id"), kind="purchase",
                                 idempotency_key=f"stripe:{obj.get('id')}")
            return f"pack {item}"
        if item in ("plus", "pro"):
            iv = "year" if (obj.get("metadata") or {}).get("interval") == "year" else "month"
            await db.user_set_billing(uid, plan=item, subscription=obj.get("subscription"), interval=iv)
            return f"plan {item}"           # a havi kredit az invoice.paid-re jön (az első számla is)
        return "ignored"
    if kind == "invoice.paid":
        sub = obj.get("subscription") or (obj.get("parent") or {}).get("subscription_details", {}).get("subscription")
        u = await db.user_by_subscription(sub, obj.get("customer"))
        if not u:
            return "no user"
        plan = u.get("plan") if u.get("plan") in ("plus", "pro") else \
            ((obj.get("subscription_details") or {}).get("metadata") or {}).get("item") or "plus"
        iv = _invoice_interval(obj) or u.get("billing_interval") or "month"
        await db.user_set_billing(u["id"], plan=plan, subscription=sub, interval=iv)
        if iv == "year":                    # az éves díj: a havi keret most és utána havonta (yearly_grant)
            await yearly_grant(u["id"])
            return f"credits {plan} (yearly)"
        await db.credits_add(u["id"], PLANS[plan]["monthly_credits"], f"{plan} monthly credits", ref=obj.get("id"), kind="grant",
                             idempotency_key=f"stripe:{obj.get('id')}")
        return f"credits {plan}"
    if kind in ("customer.subscription.deleted",) or (kind == "customer.subscription.updated" and obj.get("status") in ("canceled", "unpaid")):
        u = await db.user_by_subscription(obj.get("id"), obj.get("customer"))
        if u:
            await db.user_set_billing(u["id"], plan="free", subscription="")
            return "downgraded"
        return "no user"
    return "ignored"


# ---------- végpontok ----------

def costs() -> dict[str, Any]:
    """Mibe kerül kredit — az app Plans oldala és a nyilvános árlap (/pricing) is innen. A külső AI ára a közös képletből
    (app/credits.py: a szolgáltató költsége × 2 ÷ a legolcsóbban eladott kredit értéke): a kódbeli árlap szerint."""
    from app.tools import ai_image, ai_video
    edit = ai_image.edit_price()
    return {"ai_image": ai_image.gen_price("1024x1024"), "ai_image_large": ai_image.picture_price(), "ai_edit": edit,
            "restore": edit, "colourise": edit, "ai_clip_5s": ai_video.clip_prices(5),
            "scan_pages_per_credit": SCAN_PAGES_PER_CREDIT, "ai_steps_per_credit": AI_STEPS_PER_CREDIT}


@router.get("/api/billing/plans")
async def plans() -> dict[str, Any]:
    from app.site.content import plan_features
    return {"enabled": enabled(), "currency": CURRENCY, "credit_eur": 0.10,
            "plans": {k: {**v, "features": plan_features("en", k)} for k, v in PLANS.items()}, "packs": PACKS,
            "costs": costs()}


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
