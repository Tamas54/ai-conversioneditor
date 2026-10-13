"""Tartalmi ellenőrzés, élő adatok és ágak a fájlnézetből (10-14).

    POST /api/editor/snapshot     — a szerkesztő pillanatnyi állapota (a figyelő módhoz): NEM új változat, csak egy
                                    ideiglenes másolat a felhasználó temp-mappájában → snapshot_id
    POST /api/check               — {file_id | snapshot_id, facts?: bool} → job: {consistency, facts?}
    POST /api/check/apply         — {file_id, items[]} → új változat megjegyzésekkel / követett javításokkal

A vendég ide nem jut be (auth: _GUEST_OK nem engedi) — mind AI-hívás.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.storage.files import temp_path

router = APIRouter()

_SNAP = re.compile(r"^snap_[0-9a-f]{32}\.docx$")
_SNAP_MAX_MB = 60


def _doc_path(body: dict[str, Any]) -> Path:
    snap = body.get("snapshot_id")
    if snap:
        if not _SNAP.match(str(snap)) or not temp_path(snap).exists():
            raise HTTPException(404, "That snapshot is gone — save the document and check again.")
        return temp_path(snap)
    fid = body.get("file_id")
    if not fid:
        raise HTTPException(400, "Choose a file.")
    from app.tools import crafts
    try:
        p = crafts.existing(fid)
    except FileNotFoundError:
        raise HTTPException(404, "File not found.") from None
    if p.suffix.lower() != ".docx":
        raise HTTPException(400, "Checks work on Word documents (.docx).")
    return p


@router.post("/api/editor/snapshot")
async def editor_snapshot(request: Request) -> dict[str, Any]:
    data = await request.body()
    if not data or len(data) > _SNAP_MAX_MB * 1024 * 1024:
        raise HTTPException(400, "Empty or too large.")
    if data[:2] != b"PK":
        raise HTTPException(400, "Not a Word document.")
    for old in sorted(temp_path("x").parent.glob("snap_*.docx"))[:-20]:     # a régiek el (felhasználónként 20 marad)
        old.unlink(missing_ok=True)
    sid = f"snap_{uuid.uuid4().hex}.docx"
    temp_path(sid).write_bytes(data)
    return {"snapshot_id": sid}


async def _charge_claims(n: int) -> None:
    from app import db, tenancy
    uid = tenancy.current_user_id()
    if n <= 0 or not db.enabled() or uid == tenancy.ADMIN_ID:
        return
    per = max(1, settings.FACTCHECK_CLAIMS_PER_CREDIT)
    try:
        await db.credits_spend(uid, -(-n // per), "fact check", ref=f"{n} claims")
    except db.InsufficientCredits:
        raise HTTPException(402, f"Checking {n} facts needs {-(-n // per)} credit{'s' if n > per else ''} — "
                                 "see Plans for more.") from None


@router.post("/api/check")
async def api_check(body: dict[str, Any]) -> dict[str, Any]:
    from app import billing, jobs
    from app.tools import content_check as cc
    path = _doc_path(body)
    if not await cc.consistency_is_cached(path):
        await billing.use("check")
    want_facts = bool(body.get("facts"))

    async def work(progress):
        progress(0, 2, "reading the document")
        out: dict[str, Any] = {"file_id": body.get("file_id"), "snapshot_id": body.get("snapshot_id")}
        out["consistency"] = await cc.consistency(path)
        if want_facts:
            out["facts"] = await cc.facts(path, charge=_charge_claims, progress=lambda d, t, s: progress(d, t, s))
        return out
    return jobs.start("check", work)


@router.post("/api/check/apply")
async def api_check_apply(body: dict[str, Any]) -> dict[str, Any]:
    from app.tools import content_check as cc
    items = body.get("items")
    if not body.get("file_id") or not isinstance(items, list) or not items:
        raise HTTPException(400, "Choose what to put into the document.")
    try:
        return await cc.apply(body["file_id"], [x for x in items if isinstance(x, dict)])
    except FileNotFoundError:
        raise HTTPException(404, "File not found.") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


# ---------- élő adatok ----------

@router.get("/api/live")
async def api_live(file_id: str) -> dict[str, Any]:
    from app.tools import live_data
    try:
        return await live_data.listing(file_id)
    except FileNotFoundError:
        raise HTTPException(404, "File not found.") from None


@router.post("/api/live/bind")
async def api_live_bind(body: dict[str, Any]) -> dict[str, Any]:
    from app.tools import datasources as ds
    from app.tools import live_data
    if not body.get("file_id") or not body.get("quote") or not isinstance(body.get("spec"), dict):
        raise HTTPException(400, "Choose the number to keep up to date.")
    try:
        return await live_data.bind(body["file_id"], str(body["quote"]), body["spec"], body.get("paragraph"))
    except FileNotFoundError:
        raise HTTPException(404, "File not found.") from None
    except (ValueError, ds.SourceError) as e:
        raise HTTPException(400, str(e)) from None


@router.post("/api/live/refresh")
async def api_live_refresh(body: dict[str, Any]) -> dict[str, Any]:
    from app import billing, jobs
    from app.tools import live_data
    fid = body.get("file_id")
    if not fid:
        raise HTTPException(400, "Choose a file.")
    await billing.use("check")

    async def work(progress):
        return await live_data.refresh(fid, progress)
    return jobs.start("live", work)


# ---------- ágak és összefésülés ----------

@router.get("/api/branches")
async def api_branches(file_id: str) -> dict[str, Any]:
    import asyncio
    from app.tools import branches
    try:
        return await asyncio.to_thread(branches.tree, file_id)
    except FileNotFoundError:
        raise HTTPException(404, "File not found.") from None


@router.post("/api/branches/name")
async def api_branch_name(body: dict[str, Any]) -> dict[str, Any]:
    from app.tools import branches
    if not body.get("file_id"):
        raise HTTPException(400, "Choose a version.")
    try:
        return branches.set_branch(body["file_id"], str(body.get("name") or ""))
    except FileNotFoundError:
        raise HTTPException(404, "File not found.") from None


@router.post("/api/branches/compare")
async def api_merge_plan(body: dict[str, Any]) -> dict[str, Any]:
    from app import jobs
    from app.tools import branches
    a, b = body.get("a"), body.get("b")
    if not a or not b or a == b:
        raise HTTPException(400, "Choose two different versions.")

    async def work(progress):
        progress(0, 1, "comparing the branches")
        return await branches.plan(a, b)
    return jobs.start("merge-plan", work)


@router.post("/api/branches/merge")
async def api_merge(body: dict[str, Any]) -> dict[str, Any]:
    from app import billing, jobs
    from app.tools import branches
    a, b = body.get("a"), body.get("b")
    if not a or not b or a == b:
        raise HTTPException(400, "Choose two different versions.")
    choices = body.get("choices") if isinstance(body.get("choices"), dict) else {}
    instruction = str(body.get("instruction") or "")
    if not choices and not instruction.strip():
        raise HTTPException(400, "Pick A, B or both for the places that differ — or say how to combine them.")
    await billing.use("revise")

    async def work(progress):
        return await branches.merge(a, b, choices, instruction, progress)
    return jobs.start("merge", work)
