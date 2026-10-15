"""A jobok és taskok tartós nyoma a Postgresben (api_jobs, api_tasks) — hibatűrően: a DB kiesése nem állítja meg
a futást. Memóriában nem talált jobot innen olvasunk vissza (újraindítás után)."""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("aice.api.store")


def _ts(t: Optional[float]):
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.timezone.utc) if t else None


async def save_job(job) -> None:
    from app import db
    if not db.enabled():
        return
    try:
        await db._rows("""INSERT INTO api_jobs (id, owner_id, tag, status, sandbox, webhook_url, credits, created_at, started_at, ended_at)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                          ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, credits = EXCLUDED.credits,
                            started_at = EXCLUDED.started_at, ended_at = EXCLUDED.ended_at""",
                       (job.id, job.owner, job.tag, job.status, job.sandbox, job.webhook_url,
                        sum(t.credits for t in job.tasks.values()), _ts(job.created_at), _ts(job.started_at), _ts(job.ended_at)))
        for t in job.tasks.values():
            await save_task(job, t)
    except Exception as e:
        log.warning("api_jobs save failed: %s", e)


async def save_task(job, t) -> None:
    from app import db
    if not db.enabled():
        return
    from psycopg.types.json import Jsonb
    try:
        pub = t.public()
        await db._rows("""INSERT INTO api_tasks (id, job_id, owner_id, name, operation, status, depends_on, payload, result,
                                                 message, code, percent, credits, created_at, started_at, ended_at)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                          ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, result = EXCLUDED.result,
                            message = EXCLUDED.message, code = EXCLUDED.code, percent = EXCLUDED.percent,
                            credits = EXCLUDED.credits, started_at = EXCLUDED.started_at, ended_at = EXCLUDED.ended_at""",
                       (t.id, job.id, job.owner, t.name, t.operation, t.status, Jsonb(t.depends_on), Jsonb(pub["payload"]),
                        Jsonb(t.result) if t.result is not None else None, t.message, t.code, t.percent, t.credits,
                        _ts(t.created_at), _ts(t.started_at), _ts(t.ended_at)))
    except Exception as e:
        log.warning("api_tasks save failed: %s", e)


async def load_job(job_id: str, owner: str) -> Optional[dict[str, Any]]:
    """Egy memóriában már nem élő job a DB-ből (újraindítás után): a félbemaradt feldolgozás „error”."""
    from app import db
    if not db.enabled():
        return None
    rows = await db._rows("SELECT * FROM api_jobs WHERE id = %s AND owner_id = %s", (job_id, owner))
    if not rows:
        return None
    j = rows[0]
    tasks = await db._rows("SELECT * FROM api_tasks WHERE job_id = %s ORDER BY created_at", (job_id,))
    interrupted = j["status"] in ("waiting", "processing")
    out_tasks = []
    for t in tasks:
        st = t["status"]
        if st in ("waiting", "processing"):
            st = "error"
        out_tasks.append({"id": t["id"], "name": t["name"], "operation": t["operation"], "status": st,
                          "depends_on": t["depends_on"], "message": t["message"] or ("Interrupted by a server restart — start the job again."
                                                                                       if st != t["status"] else None),
                          "code": t["code"] or ("INTERRUPTED" if st != t["status"] else None), "percent": t["percent"],
                          "credits": t["credits"], "result": t["result"], "created_at": t["created_at"],
                          "started_at": t["started_at"], "ended_at": t["ended_at"], "payload": t["payload"]})
    return {"id": j["id"], "tag": j["tag"], "status": "error" if interrupted else j["status"], "sandbox": j["sandbox"],
            "created_at": j["created_at"], "started_at": j["started_at"], "ended_at": j["ended_at"], "credits": j["credits"],
            "tasks": out_tasks, "links": {"self": f"/v1/jobs/{j['id']}", "events": f"/v1/jobs/{j['id']}/events"}}


async def list_db(owner: str, status: Optional[str], tag: Optional[str], limit: int) -> list[dict[str, Any]]:
    from app import db
    if not db.enabled():
        return []
    q = "SELECT id, tag, status, sandbox, credits, created_at, started_at, ended_at FROM api_jobs WHERE owner_id = %s"
    args: list[Any] = [owner]
    if status:
        q += " AND status = %s"
        args.append(status)
    if tag:
        q += " AND tag = %s"
        args.append(tag)
    q += " ORDER BY created_at DESC LIMIT %s"
    args.append(limit)
    return await db._rows(q, tuple(args))
