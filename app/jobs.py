"""Hosszú munkák (OCR egy egész könyvre) háttérben, előrehaladással.

A kérés azonnal visszaad egy job_id-t; a felület a GET /api/jobs/{id}-t kérdezi. Az állapot a
folyamat memóriájában él (átmeneti UI-állapot, nem tárolás): újraindításkor a futó munka elvész,
de a drága rész — az oldalankénti OCR — a Postgres ocr_cache-ben megmarad, az újrafuttatás onnan olvas.

A videó- és hangkódolás (fájleszközök, konverter, a videó munkamásolata) a közös kapun át fut (heavy(), lent):
egyszerre néhány, a többi job „queued” állapotban, a helyével vár.
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
    """A becslésből hátralévő idő; a nehéz munka sorában álló jobé a beálláskor megmaradt (a várakozás nem fogy belőle)."""
    if job.get("eta_s") is None:
        return 0.0
    now = job.get("_queued_at") if job["status"] == "queued" else None
    return max(0.0, job["eta_from"] + job["eta_s"] - (now or time.time()))


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
# elvitte a gépet, vele a weboldalt. A kapu a MUNKÁT korlátozza, nem az ffmpeg-folyamatot. Egy-egy munka: egy fájleszköz,
# egy konverzió, a Fotókba mentés, a videó munkamásolata (footage.make_proxy, a szeletei egy helyen, a saját
# -threads-ükkel). Nem kér helyet: az előnézet és a render scene=… (video_preview.gate()), a projekt felépítése
# (video._build_gate()), a render (video._render_gate(), egyszerre egy; a háttérben futó végleges render nice 10-zel) —
# az előnézet így sosem áll be egy végleges render mögé, a munkamásolat sem.

def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "") or default))
    except ValueError:
        return default


def heavy_slots() -> int:
    """Egyszerre ennyi nehéz munka fut (HEAVY_JOB_SLOTS); alapból 8 logikai magonként egy, 2 és 4 között."""
    return _env_int("HEAVY_JOB_SLOTS", max(2, min(4, (os.cpu_count() or 4) // 8)))


def heavy_threads() -> int:
    """A fájleszközök és a konverter ffmpeg-szálai egy munkában (HEAVY_JOB_THREADS); alapból a magok egyenlő része a
    helyek között."""
    return _env_int("HEAVY_JOB_THREADS", max(2, (os.cpu_count() or 4) // heavy_slots()))


_held: ContextVar[bool] = ContextVar("aice_heavy_held", default=False)
_waited: ContextVar[float] = ContextVar("aice_heavy_waited", default=0.0)
_report: ContextVar[Optional[Callable[[Optional[int]], None]]] = ContextVar("aice_heavy_report", default=None)
_heavy_running = 0
_heavy_queue: list[tuple[asyncio.Future, Optional[Callable[[Optional[int]], None]]]] = []   # érkezési sorrendben


def on_queue(report: Callable[[Optional[int]], None]) -> None:
    """Kinek szól a sorbeli hely (1, 2, …; None = sorra került): a job vagy a /v1 task. Az aktuális taskra (és az
    általa indítottakra) él."""
    _report.set(report)


def queue_text(pos: int, then: float = 0) -> str:
    """then: a job becsült ideje, ha már futna (pl. a videó munkamásolattal) — a várakozás után ennyi."""
    ahead = pos - 1
    s = (f"Waiting for a free slot: the server runs {heavy_slots()} video and audio jobs at a time, and "
         + ("yours is next." if not ahead else f"{ahead} job{'s are' if ahead > 1 else ' is'} ahead of yours."))
    if then > 0:
        from app.tools import render_eta
        return s + f" It starts by itself, then takes {render_eta.text(then)}."
    return s + " It starts by itself."


def waited() -> float:
    """Ennyit várt eddig ez a munka a nehéz munka sorában — a mért idők (render_eta kalibrációja) ne számolják bele."""
    return _waited.get()


def _queued(job: dict[str, Any], pos: Optional[int]) -> None:
    now = time.time()
    if pos is None:
        job.pop("position", None)
        job.pop("message", None)
        if job["status"] == "queued":
            if job.get("eta_s") is not None:            # a várakozás nem fogy a becslésből: indulva ugyanannyi van hátra
                job["eta_from"] += now - job.get("_queued_at", now)
            job.pop("_queued_at", None)
            job.update(status="running", stage=job.pop("_stage", ""))
        return
    if job["status"] != "queued":
        job.update(_stage=job["stage"], _queued_at=now)
    job.update(status="queued", stage="queued", position=pos)
    job["message"] = queue_text(pos, remaining_s(job))


@contextlib.asynccontextmanager
async def heavy(threads: bool = True):
    """A nehéz munka kapuja: egyszerre legfeljebb heavy_slots() fut, a többi érkezési sorrendben vár. Egy munkán belül
    egyszer foglal: a beágyazott hívás (konverzió egy eszközben, a munka saját al-taskjai) átmegy, nem vár önmagára.
    threads=True: az ffmpeg heavy_threads() szálon dolgozik (fájleszközök, konverter). threads=False: a munka maga osztja
    a -threads-et a párhuzamos szeletei között (a munkamásolat: magok // szeletek) — a kapu nem szól bele, beágyazva
    sem."""
    global _heavy_running
    from app.convert import engines
    if _held.get():
        tok = None if threads else engines.FFMPEG_THREADS.set(None)
        try:
            yield
        finally:
            if tok is not None:
                engines.FFMPEG_THREADS.reset(tok)
        return
    if _heavy_running < heavy_slots() and not _heavy_queue:
        _heavy_running += 1
    else:
        fut = asyncio.get_running_loop().create_future()
        me, t0 = (fut, _report.get()), time.time()
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
            _waited.set(_waited.get() + time.time() - t0)
            if me[1]:
                me[1](None)
    held, cap = _held.set(True), engines.FFMPEG_THREADS.set(heavy_threads() if threads else None)
    try:
        yield
    finally:
        engines.FFMPEG_THREADS.reset(cap)
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
