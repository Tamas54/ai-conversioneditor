"""Megosztás a webes felületnek (app/sharing.py): meghívás, szerepek, link, „megosztva velem”, ütközés-választás, zár.

    GET    /api/share?file_id=…          ki fér hozzá, linkek, napló (ki mit mikor)
    POST   /api/share                    {file_id, email | user_id, role, keep_location?} — meghívás / szerepváltás /
                                         role="none" = eltávolítás
    DELETE /api/share/{share_id}         visszavonás
    POST   /api/share/link               {file_id, role} → link (csak belépve ér valamit)
    DELETE /api/share/link/{link_id}
    GET    /share/{token}                a link: belépés után megosztás a belépett felhasználónak → a fájlhoz
    GET    /api/shared                   megosztva velem
    GET    /api/share/conflict?file_id=  nyitott ütközés (a te változatod és a másiké)
    POST   /api/share/choose             {file_id} — ez legyen az aktuális változat
    POST   /api/share/lock               {file_id} — szerkesztés jelzése (szívverés); DELETE /api/share/lock?file_id=

A fájl-azonosítós kéréseket az auth-middleware már a tulajdonos munkaterületén futtatja (jogellenőrzés ott).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from app import share_versions, sharing, tenancy

router = APIRouter()


def _on() -> None:
    if not sharing.enabled():
        raise HTTPException(404, "Sharing isn't switched on on this server.")
    if tenancy.is_guest():
        raise HTTPException(401, "Sharing needs an account — create a free one.")


def _fid(v: Any) -> str:
    fid = str(v or "")
    if not sharing.FID_RE.match(fid):
        raise HTTPException(400, "Choose a file (file_id).")
    return fid


@router.get("/api/share")
async def share_info(file_id: str = Query(...)) -> dict[str, Any]:
    _on()
    return await sharing.people(tenancy.current_user_id(), _fid(file_id))


@router.post("/api/share")
async def share_add(body: dict[str, Any]) -> dict[str, Any]:
    _on()
    try:
        return await sharing.share(tenancy.current_user_id(), _fid(body.get("file_id")), email=body.get("email"),
                                   user_id=body.get("user_id"), role=str(body.get("role") or "editor"),
                                   keep_location=bool(body.get("keep_location")))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.delete("/api/share/{share_id:int}")
async def share_remove(share_id: int) -> dict[str, Any]:
    _on()
    try:
        return await sharing.unshare(tenancy.current_user_id(), share_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from None


@router.post("/api/share/link")
async def share_link(body: dict[str, Any]) -> dict[str, Any]:
    _on()
    try:
        return await sharing.create_link(tenancy.current_user_id(), _fid(body.get("file_id")), str(body.get("role") or "viewer"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.delete("/api/share/link/{link_id}")
async def share_link_revoke(link_id: int) -> dict[str, Any]:
    _on()
    try:
        return await sharing.revoke_link(tenancy.current_user_id(), link_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from None


@router.get("/share/{token}", include_in_schema=False)
async def share_join(token: str):
    """A szerepkörös link: a middleware már belépést kért (vendég → regisztráció, onnan vissza ide)."""
    _on()
    try:
        fid = await sharing.join_link(tenancy.current_user_id(), token)
    except ValueError as e:
        raise HTTPException(404, str(e)) from None
    return RedirectResponse(f"/app/file/{fid}", status_code=303)


@router.get("/api/shared")
async def shared_list() -> dict[str, Any]:
    _on()
    return {"shared": await sharing.shared_with_me(tenancy.current_user_id())}


@router.get("/api/share/conflict")
async def share_conflict(file_id: str = Query(...)) -> dict[str, Any]:
    _on()
    return {"conflict": await share_versions.conflict_for(tenancy.current_user_id(), _fid(file_id))}


@router.post("/api/share/choose")
async def share_choose(body: dict[str, Any]) -> dict[str, Any]:
    _on()
    try:
        return await share_versions.choose(tenancy.current_user_id(), _fid(body.get("file_id")))
    except FileNotFoundError:
        raise HTTPException(404, "File not found.") from None


@router.post("/api/share/lock")
async def share_lock(body: dict[str, Any]) -> dict[str, Any]:
    _on()
    return await share_versions.lock(tenancy.current_user_id(), _fid(body.get("file_id")))


@router.delete("/api/share/lock")
async def share_unlock(file_id: str = Query(...)) -> dict[str, Any]:
    _on()
    return await share_versions.unlock(tenancy.current_user_id(), _fid(file_id))
