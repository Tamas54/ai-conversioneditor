"""Nyilvános API: feladatok (job) és részfeladatok (task) — állapot, végrehajtás, események.

Egy job nevesített taskok halmaza; egy task bemenete egy korábbi task neve (vagy nevek listája). A függőség
nélküli taskok azonnal, párhuzamosan indulnak; ha egy task hibázik, a tőle függők is hibával zárulnak, a
független ágak futnak tovább. Az élő állapot memóriában van (long-poll, SSE), és minden változás a Postgresbe
is kerül (api_jobs, api_tasks), így újraindítás után is lekérdezhető; a futás közben megszakadt job „error”.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app import tenancy

log = logging.getLogger("aice.api")

JOB_TTL_S = 24 * 3600
SANDBOX_TTL_S = 3600
MAX_TASKS = 50


def _now() -> float:
    return time.time()


def _iso(t: Optional[float]) -> Optional[str]:
    if not t:
        return None
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class Task:
    id: str
    name: str
    operation: str
    payload: dict[str, Any]
    depends_on: list[str]
    status: str = "waiting"           # waiting, processing, finished, error, cancelled
    result: Optional[dict[str, Any]] = None
    message: Optional[str] = None
    code: Optional[str] = None
    percent: int = 0
    credits: int = 0
    created_at: float = field(default_factory=_now)
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    upload_event: Optional[asyncio.Event] = None

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "operation": self.operation, "status": self.status,
                "depends_on": self.depends_on, "message": self.message, "code": self.code, "percent": self.percent,
                "credits": self.credits, "result": self.result, "created_at": _iso(self.created_at),
                "started_at": _iso(self.started_at), "ended_at": _iso(self.ended_at),
                "payload": {k: v for k, v in self.payload.items() if k not in _SECRET_FIELDS}}


_SECRET_FIELDS = {"access_key_id", "secret_access_key", "session_token", "password", "private_key", "sas_token",
                  "connection_string", "credentials", "access_token", "token", "headers", "file"}


@dataclass
class Job:
    id: str
    owner: str
    tasks: dict[str, Task]
    tag: Optional[str] = None
    webhook_url: Optional[str] = None
    sandbox: bool = False
    status: str = "waiting"           # waiting, processing, finished, error, cancelled
    created_at: float = field(default_factory=_now)
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    listeners: list[asyncio.Queue] = field(default_factory=list)
    runner: Optional[asyncio.Task] = None

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "tag": self.tag, "status": self.status, "sandbox": self.sandbox,
                "created_at": _iso(self.created_at), "started_at": _iso(self.started_at), "ended_at": _iso(self.ended_at),
                "credits": sum(t.credits for t in self.tasks.values()),
                "tasks": [t.public() for t in self.tasks.values()],
                "links": {"self": f"/v1/jobs/{self.id}", "events": f"/v1/jobs/{self.id}/events"}}

    def emit(self, event: str, data: dict[str, Any]) -> None:
        for q in list(self.listeners):
            try:
                q.put_nowait({"event": event, "data": data})
            except asyncio.QueueFull:
                pass


_JOBS: dict[str, Job] = {}


def new_id() -> str:
    return secrets.token_hex(10)


def get(job_id: str, owner: str) -> Optional[Job]:
    j = _JOBS.get(job_id)
    return j if j and j.owner == owner else None


def task_by_id(task_id: str) -> Optional[tuple[Job, Task]]:
    for j in _JOBS.values():
        for t in j.tasks.values():
            if t.id == task_id:
                return j, t
    return None


def list_jobs(owner: str, status: Optional[str] = None, tag: Optional[str] = None, limit: int = 50) -> list[Job]:
    js = [j for j in _JOBS.values() if j.owner == owner and (not status or j.status == status) and (not tag or j.tag == tag)]
    return sorted(js, key=lambda j: j.created_at, reverse=True)[:limit]


def gc() -> None:
    now = _now()
    for jid, j in list(_JOBS.items()):
        ttl = SANDBOX_TTL_S if j.sandbox else JOB_TTL_S
        if j.ended_at and now - j.ended_at > ttl:
            _JOBS.pop(jid, None)


# ---------- létrehozás, validálás ----------

class JobError(ValueError):
    def __init__(self, msg: str, code: str = "INVALID_JOB"):
        super().__init__(msg)
        self.code = code


def build(owner: str, spec: dict[str, Any], sandbox: bool) -> Job:
    from app.api_v1 import operations as ops
    tasks_spec = spec.get("tasks")
    if not isinstance(tasks_spec, dict) or not tasks_spec:
        raise JobError("tasks: give at least one named task, e.g. {\"in\": {\"operation\": \"import/url\", …}}")
    if len(tasks_spec) > MAX_TASKS:
        raise JobError(f"at most {MAX_TASKS} tasks per job")
    tasks: dict[str, Task] = {}
    for name, ts in tasks_spec.items():
        if not isinstance(ts, dict) or not isinstance(ts.get("operation"), str):
            raise JobError(f"task {name!r}: operation is required")
        if not (1 <= len(name) <= 64) or not all(c.isalnum() or c in "-_." for c in name):
            raise JobError(f"task name {name!r}: letters, digits, - _ . (1–64)")
        op = ts["operation"]
        if not ops.known(op):
            raise JobError(f"task {name!r}: unknown operation {op!r}", "UNKNOWN_OPERATION")
        inp = ts.get("input")
        deps = [inp] if isinstance(inp, str) else list(inp or [])
        if not all(isinstance(d, str) for d in deps):
            raise JobError(f"task {name!r}: input must be a task name or a list of names")
        payload = {k: v for k, v in ts.items() if k not in ("operation", "input")}
        ops.validate(op, payload, has_input=bool(deps))
        tasks[name] = Task(new_id(), name, op, payload, deps)
    for t in tasks.values():
        for d in t.depends_on:
            if d not in tasks:
                raise JobError(f"task {t.name!r}: input {d!r} is not a task of this job")
    _check_cycles(tasks)
    webhook = spec.get("webhook_url")
    if webhook is not None and (not isinstance(webhook, str) or not webhook.startswith(("http://", "https://"))):
        raise JobError("webhook_url: an http(s) URL")
    tag = spec.get("tag")
    job = Job(new_id(), owner, tasks, tag=str(tag)[:120] if tag else None, webhook_url=webhook, sandbox=sandbox)
    return job


def _check_cycles(tasks: dict[str, Task]) -> None:
    seen, stack = set(), set()

    def visit(n: str) -> None:
        if n in stack:
            raise JobError(f"tasks form a cycle at {n!r}")
        if n in seen:
            return
        stack.add(n)
        for d in tasks[n].depends_on:
            visit(d)
        stack.discard(n)
        seen.add(n)
    for n in tasks:
        visit(n)


# ---------- futtatás ----------

def start(job: Job) -> None:
    gc()
    _JOBS[job.id] = job
    job.runner = asyncio.get_running_loop().create_task(_run(job))


def _queued(job: Job, t: Task, pos: Optional[int]) -> None:
    """A task a nehéz munka sorában vár (status marad „processing”): az üzenete mondja meg, hányadik."""
    from app import jobs as studio_jobs
    t.message = studio_jobs.queue_text(pos) if pos else None
    job.emit("task.updated", t.public())


async def _run(job: Job) -> None:
    from app import jobs as studio_jobs
    from app.api_v1 import operations as ops, store, webhooks
    job.status, job.started_at = "processing", _now()
    await store.save_job(job)
    job.emit("job.created", job.public())
    running: dict[str, asyncio.Task] = {}

    async def run_task(t: Task) -> None:
        t.status, t.started_at = "processing", _now()
        studio_jobs.on_queue(lambda pos: _queued(job, t, pos))   # videó / hang: a közös kapu sorában (app/jobs.py heavy)
        job.emit("task.updated", t.public())
        await store.save_task(job, t)
        try:
            inputs = [job.tasks[d] for d in t.depends_on]
            t.result = await ops.execute(job, t, inputs)
            t.status, t.percent = "finished", 100
        except asyncio.CancelledError:
            t.status, t.message, t.code = "cancelled", "Cancelled.", "CANCELLED"
            raise
        except ops.OpError as e:
            t.status, t.message, t.code = "error", str(e), e.code
        except Exception as e:  # a hívó lássa, mi történt
            log.exception("task %s (%s) failed", t.id, t.operation)
            t.status, t.message, t.code = "error", (getattr(e, "detail", None) or str(e))[:500], "PROCESSING_ERROR"
        finally:
            t.ended_at = _now()
            job.emit("task.updated", t.public())
            await store.save_task(job, t)

    try:
        while True:
            pending = [t for t in job.tasks.values() if t.status == "waiting"]
            for t in pending:
                deps = [job.tasks[d] for d in t.depends_on]
                if any(d.status in ("error", "cancelled") for d in deps):
                    t.status, t.code, t.ended_at = "error", "INPUT_TASK_FAILED", _now()
                    t.message = "An input task failed: " + ", ".join(d.name for d in deps if d.status in ("error", "cancelled"))
                    job.emit("task.updated", t.public())
                    await store.save_task(job, t)
                elif all(d.status == "finished" for d in deps) and t.name not in running:
                    running[t.name] = asyncio.get_running_loop().create_task(run_task(t))
            active = [r for n, r in running.items() if not r.done()]
            if not active:
                if not any(t.status == "waiting" for t in job.tasks.values()):
                    break
                continue
            await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        for r in running.values():
            r.cancel()
        for t in job.tasks.values():
            if t.status in ("waiting", "processing"):
                t.status, t.code, t.message, t.ended_at = "cancelled", "CANCELLED", "Cancelled.", _now()
        job.status = "cancelled"
    else:
        job.status = "error" if any(t.status == "error" for t in job.tasks.values()) else "finished"
    job.ended_at = _now()
    await store.save_job(job)
    event = "job.finished" if job.status == "finished" else "job.failed"
    job.emit(event, job.public())
    job.done.set()
    asyncio.get_running_loop().create_task(webhooks.deliver_job(job, event))


async def cancel(job: Job) -> None:
    if job.runner and not job.runner.done():
        job.runner.cancel()
        try:
            await asyncio.wait_for(job.done.wait(), 10)
        except asyncio.TimeoutError:
            pass
