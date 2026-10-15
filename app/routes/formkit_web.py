"""Az aláírások és az adatprofil webes végpontjai (a fiókhoz; a tartalom titkosítva tárolódik).

    GET    /api/signatures                 a saját aláírások listája
    GET    /api/signatures/{id}/image      a tiszta PNG (csak a tulajdonosnak, nem gyorsítótárazva)
    DELETE /api/signatures/{id}            végleges törlés
    GET/PUT/DELETE /api/profile            adatprofil az ügynöki kitöltéshez
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app import tenancy

router = APIRouter()


@router.get("/api/signatures")
async def signatures() -> dict[str, Any]:
    from app.formkit import signature as G
    return {"signatures": await G.list_(tenancy.current_user_id())}


@router.get("/api/signatures/{sid}/image")
async def signature_image(sid: str) -> Response:
    from app.formkit import signature as G
    try:
        png = await G.load(tenancy.current_user_id(), sid)
    except KeyError:
        raise HTTPException(404, "No such signature.")
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, no-store"})


@router.delete("/api/signatures/{sid}")
async def signature_delete(sid: str) -> dict[str, Any]:
    from app.formkit import signature as G
    if not await G.delete(tenancy.current_user_id(), sid):
        raise HTTPException(404, "No such signature.")
    return {"deleted": sid}


@router.get("/api/profile")
async def profile() -> dict[str, Any]:
    from app.formkit import profile as P
    return {"profile": await P.load_profile(tenancy.current_user_id()), "fields": list(P.PROFILE_KEYS)}


@router.put("/api/profile")
async def profile_put(body: dict[str, Any]) -> dict[str, Any]:
    from app.formkit import profile as P
    try:
        return {"profile": await P.save_profile(tenancy.current_user_id(), body.get("profile", body),
                                                merge=bool(body.get("merge", True)) if "profile" in body else True)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/api/profile")
async def profile_delete() -> dict[str, Any]:
    from app.formkit import profile as P
    return {"deleted": await P.delete_profile(tenancy.current_user_id())}
