"""Hosszú munkák (OCR egy egész könyvre) háttérben, előrehaladással.

A kérés azonnal visszaad egy job_id-t; a felület a GET /api/jobs/{id}-t kérdezi. Az állapot a
folyamat memóriájában él (átmeneti UI-állapot, nem tárolás): újraindításkor a futó munka elvész,
de a drága rész — az oldalankénti OCR — a Postgres ocr_cache-ben megmarad, az újrafuttatás onnan olvas.
"""
from __future__ import annotations

import asyncio
import contextvars
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
        except Exception as e:  # a felhasználó lássa, mi történt
            log.exception("job %s (%s) failed", jid, kind)
            job.update(status="failed", error=getattr(e, "detail", None) or str(e)[:300])
        job["finished"] = time.time()

    # a tenancy (melyik felhasználó tárhelye) a háttérfeladatba is átmegy
    asyncio.get_running_loop().create_task(run(), context=contextvars.copy_context())
    return _public(job)


def _public(job: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in job.items() if k != "owner"}


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
