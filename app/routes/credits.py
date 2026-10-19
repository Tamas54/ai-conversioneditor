"""Kreditkönyv végpontok (a logika: app/credits.py).

    GET  /api/account/ledger           a saját tranzakciótörténet (a Plans oldalon)
    GET  /api/estimate                 egy művelet ára a pricing_rules szerint (operation, model?, tier?, units?)
    GET  /api/admin/pricing            árszabályok — admin
    POST /api/admin/pricing            egy szabály mentése (operation+model+tier a kulcs) — admin
    DELETE /api/admin/pricing/{id}     — admin
    GET  /api/admin/credits/status     figyelmeztetések, egyeztetések, a könyv utolsó sorai — admin
    POST /api/admin/credits/reconcile  egyeztetés most — admin
    POST /api/admin/notices/{id}/resolve — admin
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException

from app import credits, db, tenancy

router = APIRouter()


def _need_db() -> None:
    if not db.enabled():
        raise HTTPException(503, "The credit ledger needs the database (DATABASE_URL).")


def _admin() -> None:
    if tenancy.current_user_id() != tenancy.ADMIN_ID:
        raise HTTPException(403, "admin only")
    _need_db()


@router.get("/api/account/ledger")
async def account_ledger(limit: int = 50, before: Optional[int] = None) -> dict[str, Any]:
    uid = tenancy.current_user_id()
    if tenancy.is_guest(uid) or not db.enabled():
        return {"entries": [], "balance": None}
    return {"entries": await credits.history(uid, limit, before), "balance": await credits.balance(uid)}


@router.get("/api/estimate")
async def estimate(operation: str, model: str = "", tier: str = "", units: float = 1) -> dict[str, Any]:
    est = await credits.estimate(operation, model, tier, units)
    return {**est, "balance": await credits.balance(tenancy.current_user_id())}


@router.get("/api/admin/pricing")
async def pricing_list() -> dict[str, Any]:
    _admin()
    return {"rules": await credits.rules(), "units": list(credits.UNITS)}


@router.post("/api/admin/pricing")
async def pricing_save(body: dict[str, Any]) -> dict[str, Any]:
    _admin()
    if not str(body.get("operation") or "").strip():
        raise HTTPException(400, "operation is required")
    try:
        return await credits.rule_save(body)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))


@router.delete("/api/admin/pricing/{rule_id}")
async def pricing_delete(rule_id: int) -> dict[str, Any]:
    _admin()
    if not await credits.rule_delete(rule_id):
        raise HTTPException(404, "no such rule")
    return {"deleted": rule_id}


@router.get("/api/admin/credits/status")
async def credits_status() -> dict[str, Any]:
    _admin()
    from app import providers
    return {"notices": await credits.notices(), "provider_flag": providers.flag() or "off",
            "providers": list(providers.active()),
            "balances": await db._rows("SELECT * FROM provider_balance ORDER BY id DESC LIMIT 14"),
            "ledger": await db._rows("""SELECT l.id, l.created_at, l.user_id, u.email, l.kind, l.delta, l.balance_after, l.reason,
                                               l.operation, l.provider, l.provider_cost, l.meta
                                        FROM credit_ledger l LEFT JOIN users u ON u.id = l.user_id ORDER BY l.id DESC LIMIT 60"""),
            "open_tasks": await db._rows("""SELECT id, user_id, provider, model, operation, status, hold, created_at
                                            FROM provider_tasks WHERE NOT settled ORDER BY created_at DESC LIMIT 30""")}


@router.post("/api/admin/credits/reconcile")
async def credits_reconcile() -> dict[str, Any]:
    _admin()
    return await credits.reconcile()


@router.post("/api/admin/notices/{nid}/resolve")
async def notice_resolve(nid: int) -> dict[str, Any]:
    _admin()
    if not await credits.notice_resolve(nid):
        raise HTTPException(404, "no such open notice")
    return {"resolved": nid}
