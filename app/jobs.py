"""Hosszú munkák (OCR egy egész könyvre) háttérben, előrehaladással.

A kérés azonnal visszaad egy job_id-t; a felület a GET /api/jobs/{id}-t kérdezi. Az állapot a
folyamat memóriájában él (átmeneti UI-állapot, nem tárolás): újraindításkor a futó munka elvész,
de a drága rész — az oldalankénti OCR — a Postgres ocr_cache-ben megmarad, az újrafuttatás onnan olvas.

A videó- és hangkódolás (fájleszközök, konverter) a közös kapun át fut (heavy(), lent): egyszerre néhány,
a többi job „queued” állapotban, a helyével vár.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, HTTPException

from app import tenancy

log = logging.getLogger("aice.jobs")
router = APIRouter()

_JOBS: dict[str, dict[str, Any]] = {}
_KEEP_S = 6 * 3600
_LIVE = ("running", "queued")          # még nem végzett (queued: a nehéz munka sorában vár)


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
        on_queue(lambda pos: _queued(job, pos))          # a nehéz munka sorában: queued + hely (heavy())
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


def mine(job_id: str) -> bool:
    """Ennek a felhasználónak a jobja-e (a REST /v1/jobs mellett: az MCP jobs eszköze ezt is látja)."""
    job = _JOBS.get(job_id or "")
    return bool(job) and job["owner"] == tenancy.current_user_id()


async def wait_status(job_id: str, wait: Any = None) -> dict[str, Any]:
    """A job állapota; wait: legfeljebb ennyi másodpercig (≤ 55) vár, amíg fut — a kliens így nem pörgeti a status-t."""
    job = _JOBS.get(job_id)
    secs = max(0.0, min(float(wait or 0), 55.0))
    if secs and job and job["owner"] == tenancy.current_user_id() and job["status"] in _LIVE and job.get("_task"):
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
    if job["status"] in _LIVE and job.get("_task"):
        job["_task"].cancel()
        await asyncio.sleep(0)
    return _public(job)


# ---------- nehéz munka: videó- és hangkódolás ffmpeggel ----------
# 10-23 (kódátnézés): a fájleszközök ffmpeg-futásainak nem volt korlátja — három felhasználó három 2 GB-os újrakódolása
# elvitte a gépet, vele a weboldalt. A kapu a MUNKÁT korlátozza, nem az ffmpeg-folyamatot: a gyors render egy munkán
# belül szándékosan párhuzamos szeletekben kódol (app/tools/fast_render.py), a videó-render a saját kapuja mögött.

def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "") or default))
    except ValueError:
        return default


def heavy_slots() -> int:
    """Egyszerre ennyi nehéz munka fut (HEAVY_JOB_SLOTS); alapból 8 logikai magonként egy, 2 és 4 között."""
    return _env_int("HEAVY_JOB_SLOTS", max(2, min(4, (os.cpu_count() or 4) // 8)))


def heavy_threads() -> int:
    """Egy nehéz munka ffmpeg-szálai (HEAVY_JOB_THREADS); alapból a magok egyenlő része a helyek között."""
    return _env_int("HEAVY_JOB_THREADS", max(2, (os.cpu_count() or 4) // heavy_slots()))


_held: ContextVar[bool] = ContextVar("aice_heavy_held", default=False)
_report: ContextVar[Optional[Callable[[Optional[int]], None]]] = ContextVar("aice_heavy_report", default=None)
_heavy_running = 0
_heavy_queue: list[tuple[asyncio.Future, Optional[Callable[[Optional[int]], None]]]] = []   # érkezési sorrendben


def on_queue(report: Callable[[Optional[int]], None]) -> None:
    """Kinek szól a sorbeli hely (1, 2, …; None = sorra került): a job vagy a /v1 task. Az aktuális taskra (és az
    általa indítottakra) él."""
    _report.set(report)


def queue_text(pos: int) -> str:
    ahead = pos - 1
    return (f"Waiting for a free slot: the server runs {heavy_slots()} video and audio jobs at a time, and "
            + ("yours is next." if not ahead else f"{ahead} job{'s are' if ahead > 1 else ' is'} ahead of yours.")
            + " It starts by itself.")


def _queued(job: dict[str, Any], pos: Optional[int]) -> None:
    if pos is None:
        job.pop("position", None)
        job.pop("message", None)
        if job["status"] == "queued":
            job.update(status="running", stage=job.pop("_stage", ""))
        return
    if job["status"] != "queued":
        job["_stage"] = job["stage"]
    job.update(status="queued", stage="queued", position=pos, message=queue_text(pos))


@contextlib.asynccontextmanager
async def heavy():
    """A nehéz munka kapuja: egyszerre legfeljebb heavy_slots() fut, a többi érkezési sorrendben vár, közben az ffmpeg
    heavy_threads() szálon dolgozik. Egy munkán belül egyszer foglal: a beágyazott hívás (konverzió egy eszközben, a
    munka saját al-taskjai) átmegy, nem vár önmagára."""
    global _heavy_running
    if _held.get():
        yield
        return
    if _heavy_running < heavy_slots() and not _heavy_queue:
        _heavy_running += 1
    else:
        fut = asyncio.get_running_loop().create_future()
        me = (fut, _report.get())
        _heavy_queue.append(me)
        _tell()
        try:
            await fut                                   # a _heavy_release() adja át a helyet (és számolja)
        except BaseException:
            if me in _heavy_queue:
                _heavy_queue.remove(me)
                _tell()
            elif fut.done() and not fut.cancelled():
                _heavy_release()                        # a hely épp megjött, amikor leállították: továbbadjuk
            raise
        finally:
            if me[1]:
                me[1](None)
    from app.convert import engines
    held, threads = _held.set(True), engines.FFMPEG_THREADS.set(heavy_threads())
    try:
        yield
    finally:
        engines.FFMPEG_THREADS.reset(threads)
        _held.reset(held)
        _heavy_release()


def _heavy_release() -> None:
    global _heavy_running
    _heavy_running -= 1
    while _heavy_queue and _heavy_running < heavy_slots():
        fut, _ = _heavy_queue.pop(0)
        if not fut.done():                              # a leállított várakozó már nem kér helyet
            _heavy_running += 1
            fut.set_result(None)
    _tell()


def _tell() -> None:
    for pos, (_, report) in enumerate(_heavy_queue, 1):
        if report:
            report(pos)
