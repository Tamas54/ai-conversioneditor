"""Hosszú munkák (OCR egy egész könyvre) háttérben, előrehaladással.

A kérés azonnal visszaad egy job_id-t; a felület a GET /api/jobs/{id}-t kérdezi. Az állapot a
folyamat memóriájában él (átmeneti UI-állapot, nem tárolás): újraindításkor a futó munka elvész,
de a drága rész — az oldalankénti OCR — a Postgres ocr_cache-ben megmarad, az újrafuttatás onnan olvas.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException

from app import tenancy

log = logging.getLogger("aice.jobs")
router = APIRouter()

_JOBS: dict[str, dict[str, Any]] = {}
_KEEP_S = 6 * 3600


class Progress:
    def __init__(self, job: dict[str, Any]):
        self._job = job

    def __call__(self, done: int, total: int, stage: str = "") -> None:
        self._job.update(done=done, total=total, stage=stage)


def start(kind: str, work: Callable[[Progress], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    _gc()
    jid = uuid.uuid4().hex[:16]
    job = {"id": jid, "kind": kind, "owner": tenancy.current_user_id(), "status": "running",
           "done": 0, "total": 0, "stage": "starting", "result": None, "error": None, "started": time.time()}
    _JOBS[jid] = job

    async def run() -> None:
        try:
            job["result"] = await work(Progress(job))
            job["status"] = "done"
        except asyncio.CancelledError:
            job.update(status="cancelled", error="Stopped.")
        except Exception as e:  # a felhasználó lássa, mi történt
            log.exception("job %s (%s) failed", jid, kind)
            job.update(status="failed", error=getattr(e, "detail", None) or str(e)[:300])
        job["finished"] = time.time()

    # a task létrehozáskor a futó kontextus másolatát kapja → a tenancy (melyik felhasználó tárhelye) átmegy.
    # (context= paramétert NEM adunk: élesben Python 3.10 + uvloop, ott nincs ilyen paraméter.)
    job["_task"] = asyncio.get_running_loop().create_task(run())
    return _public(job)


def _public(job: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in job.items() if k != "owner" and not k.startswith("_")}


def _gc() -> None:
    now = time.time()
    for jid in [j for j, v in _JOBS.items() if now - v.get("finished", now) > _KEEP_S]:
        _JOBS.pop(jid, None)


@router.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    job = _JOBS.get(job_id)
    if not job or job["owner"] != tenancy.current_user_id():
        raise HTTPException(404, "No such job (it may have been lost in a restart — start it again; finished pages are cached).")
    return _public(job)


@router.delete("/api/jobs/{job_id}")
async def job_cancel(job_id: str) -> dict[str, Any]:
    """Futó munka leállítása (a már kiolvasott oldalak a gyorsítótárban maradnak)."""
    job = _JOBS.get(job_id)
    if not job or job["owner"] != tenancy.current_user_id():
        raise HTTPException(404, "No such job.")
    if job["status"] == "running" and job.get("_task"):
        job["_task"].cancel()
        await asyncio.sleep(0)
    return _public(job)
