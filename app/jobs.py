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

    def eta(self, seconds: float) -> None:
        """Becsült hátralévő idő mostantól (a status ebből számolja a remaining_s-t és a ready_at-et)."""
        self._job.update(eta_s=round(float(seconds)), eta_from=time.time())


def start(kind: str, work: Callable[[Progress], Awaitable[dict[str, Any]]], eta_s: float | None = None,
          **extra: Any) -> dict[str, Any]:
    _gc()
    jid = uuid.uuid4().hex[:16]
    job = {"id": jid, "kind": kind, "owner": tenancy.current_user_id(), "status": "running",
           "done": 0, "total": 0, "stage": "starting", "result": None, "error": None, "started": time.time(), **extra}
    if eta_s is not None:
        job.update(eta_s=round(float(eta_s)), eta_from=job["started"])
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
    out = {k: v for k, v in job.items() if k not in ("owner", "eta_from") and not k.startswith("_")}
    if job.get("eta_s") is not None and job["status"] == "running":       # hátralévő idő a becslésből
        from app.tools import render_eta
        left = job["eta_from"] + job["eta_s"] - time.time()
        if left > 0:
            out.update(remaining_s=round(left), remaining=render_eta.text(left), ready_at=render_eta.ready_at(left))
        else:
            out.update(remaining_s=None, remaining="taking longer than estimated — still working")
    return out


def running(kind: str, **match: Any) -> list[dict[str, Any]]:
    """Ennek a folyamatnak a futó munkái (a sorban várók becsléséhez)."""
    return [j for j in _JOBS.values() if j["kind"] == kind and j["status"] == "running"
            and all(j.get(k) == v for k, v in match.items())]


def remaining_s(job: dict[str, Any]) -> float:
    if job.get("eta_s") is None:
        return 0.0
    return max(0.0, job["eta_from"] + job["eta_s"] - time.time())


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


def mine(job_id: str) -> bool:
    """Ennek a felhasználónak a jobja-e (a REST /v1/jobs mellett: az MCP jobs eszköze ezt is látja)."""
    job = _JOBS.get(job_id or "")
    return bool(job) and job["owner"] == tenancy.current_user_id()


async def wait_status(job_id: str, wait: Any = None) -> dict[str, Any]:
    """A job állapota; wait: legfeljebb ennyi másodpercig (≤ 55) vár, amíg fut — a kliens így nem pörgeti a status-t."""
    job = _JOBS.get(job_id)
    secs = max(0.0, min(float(wait or 0), 55.0))
    if secs and job and job["owner"] == tenancy.current_user_id() and job["status"] == "running" and job.get("_task"):
        try:
            await asyncio.wait_for(asyncio.shield(job["_task"]), secs)
        except asyncio.TimeoutError:
            pass
        except Exception:                       # a hiba a job állapotában van (failed)
            pass
    return await job_status(job_id)


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
