"""A felhasználó arculatai (Postgres: brand_kits) — melyik kerül egy dizájnra.

brand paraméter:  None → az alapértelmezett, ha „automatikusan” be van kapcsolva · "none"/"off" → semmi ·
                  "mine"/"default" → az alapértelmezett (kapcsolótól függetlenül) · szám → az az arculat ·
                  név → a saját ilyen nevű, különben a beépített katalógus (registry) arculata.
A kimenet a design_auto / design formátuma: {id, name, colors{primary, accent, bg, text, extra}, fonts{heading, body},
rules, logo_file_id, _logo_path}.
"""
from __future__ import annotations

from typing import Any, Optional

from app import db, tenancy

OFF = {"none", "off", "no", "false", "0"}


def _from_row(row: dict[str, Any]) -> dict[str, Any]:
    spec = dict(row.get("spec") or {})
    spec.update(id=row["id"], name=row.get("name") or spec.get("name"), is_default=row.get("is_default"),
                auto_apply=row.get("auto_apply"))
    fid = spec.get("logo_file_id")
    if fid:
        try:
            from app.tools.crafts import existing
            spec["_logo_path"] = str(existing(fid))
        except Exception:                    # a logófájl törölve: nélküle
            spec["_logo_path"] = None
    return spec


def _from_registry(name: str) -> Optional[dict[str, Any]]:
    from app.brands import get_brand
    b = get_brand(name)
    if b is None:
        return None
    return {"name": b.label, "colors": {"primary": b.colors.primary, "accent": b.colors.accent, "bg": b.colors.body_bg,
                                        "text": b.colors.text_primary, "extra": [b.colors.panel_bg]},
            "fonts": {"heading": b.fonts.heading, "body": b.fonts.body}, "rules": {}, "_registry": name}


async def resolve(param: Any = None) -> Optional[dict[str, Any]]:
    """A dizájnra kerülő arculat (vagy None)."""
    if param is not None and str(param).strip().lower() in OFF:
        return None
    uid = tenancy.current_user_id()
    if not db.enabled():
        return _from_registry(str(param)) if param not in (None, "", "mine", "default") else None
    if param in (None, ""):
        row = await db.brand_default(uid)
        return _from_row(row) if row and row.get("auto_apply") else None
    p = str(param).strip()
    if p.lower() in ("mine", "default", "my brand"):
        row = await db.brand_default(uid)
        if not row:
            raise ValueError("You have no brand yet — learn one from a few examples first.")
        return _from_row(row)
    if p.isdigit():
        row = await db.brand_get(uid, int(p))
        if row:
            return _from_row(row)
    row = await db.brand_by_name(uid, p)
    if row:
        return _from_row(row)
    reg = _from_registry(p)
    if reg:
        return reg
    raise ValueError(f"Unknown brand: {param}")


def public(spec: dict[str, Any]) -> dict[str, Any]:
    """A felületnek/ügynöknek: a belső mezők (_logo_path) nélkül, a logó előnézeti linkjével."""
    out = {k: v for k, v in spec.items() if not k.startswith("_")}
    if spec.get("logo_file_id"):
        try:
            from app.storage import public_url
            out["logo_url"] = public_url(spec["logo_file_id"])
        except Exception:
            pass
    return out
