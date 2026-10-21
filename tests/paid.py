"""Fizetett kredit a teszt-fiókoknak: az AI-videó (credits.PAID_ONLY_OPS) csak a Stripe-ból jött kreditből foglalhat."""
from __future__ import annotations

import secrets


def add_paid(call, uid: str, n: int) -> None:
    from app import db
    if n:
        call(lambda: db.credits_add(uid, n, "bought pack", kind="purchase", idempotency_key=f"stripe:t_{secrets.token_hex(6)}"))


def paid_user(call, n: int, name: str | None = None) -> str:
    """Új fiók n fizetett kredittel (kezdő kredit nélkül)."""
    from app import db
    uid = call(db.user_create, f"{secrets.token_hex(6)}@example.com", "jelszó123456", name, 0)["id"]
    add_paid(call, uid, n)
    return uid
