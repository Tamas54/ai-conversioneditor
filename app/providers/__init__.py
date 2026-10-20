"""Külső AI-szolgáltatók cserélhető rétege (OMLETT 2.1): a szolgáltatói adat (feladat-azonosító, állapot, letöltési URL,
mezőnevek) itt marad; a saját szintézis-/renderréteg (app/tools/video.py) csak a letöltött, saját tárhelyen lévő fájlt látja.

Kapcsoló (kockázatos — valódi pénzt költ):
    KIE_API_KEY         a kie.ai kulcsa (csak env-ből): AI-klip (Seedance), Topaz a kie-n
    FAL_KEY             a fal.ai kulcsa (csak env-ből): élesítés (SeedVR2 / Topaz) — ENHANCE_ENGINE választ
    AI_VIDEO            "" (alap: ki) | "admin" (csak az admin próbálhatja) | "1" (mindenki, kreditért)
"""
from __future__ import annotations

import os
from typing import Optional

from app.providers.base import Provider, ProviderError

__all__ = ["Provider", "ProviderError", "flag", "enabled_for", "active", "get"]


def flag() -> str:
    v = os.getenv("AI_VIDEO", "").strip().lower()
    return "admin" if v == "admin" else "on" if v in ("1", "on", "true", "yes", "all") else ""


def _configured() -> dict[str, type]:
    from app.providers.fal import FalProvider
    from app.providers.kie import KieProvider
    return {**({"kie": KieProvider} if os.getenv("KIE_API_KEY") else {}),
            **({"fal": FalProvider} if os.getenv("FAL_KEY") else {})}


def configured(name: str) -> bool:
    return name in _configured()


def enabled_for(uid: Optional[str] = None) -> bool:
    from app import tenancy
    f = flag()
    if not f or not _configured():
        return False
    return f == "on" or (uid or tenancy.current_user_id()) == tenancy.ADMIN_ID


_instances: dict[str, Provider] = {}


def active() -> dict[str, Provider]:
    """A bekapcsolt és kulccsal rendelkező szolgáltatók (napi egyeztetés, folytatás újraindítás után)."""
    if not flag():
        return {}
    return {name: get(name) for name in _configured()}


def get(name: str = "kie") -> Provider:
    cls = _configured().get(name)
    if cls is None:
        raise ProviderError(f"AI provider {name!r} is not configured on this server.")
    inst = _instances.get(name)
    if inst is None or not inst.same_key():
        inst = _instances[name] = cls()
    return inst
