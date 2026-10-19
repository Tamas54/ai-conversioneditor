"""AI-videó külső szolgáltatóval (kie.ai: Seedance 2.5 klip, Topaz felskálázás) — a szolgáltatói réteg (app/providers) és a
saját videóréteg (app/tools/video.py) között: kredit (becslés → hold → feladat → capture / release), a feladat
nyilvántartása (provider_tasks), várakozás (lekérdezés visszalépéssel, a webhook felébreszti), eredmény AZONNAL a saját
tárhelyre, újraindítás után folytatás (resume_loop).

Egy felhasználói művelet („op”) egy vagy több szolgáltatói feladat (hosszú videó felskálázása darabokban): a hold és az
elszámolás az op-é, a sorok meta.op-ja köti össze őket.
"""
from __future__ import annotations

import asyncio
import json
import math
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Optional

from app import credits, db, tenancy
from app.providers.base import FAIL, SUCCESS, TERMINAL, ProviderError, TaskState

log = logging.getLogger("aice.ai_video")

POLL_FIRST_S, POLL_MAX_S = 3.0, 30.0
TASK_TIMEOUT_S = float(os.getenv("AI_TASK_TIMEOUT_H", "6")) * 3600

_events: dict[str, asyncio.Event] = {}          # op → a webhook felébreszti a várakozót
_locks: dict[str, asyncio.Lock] = {}
_watching: set[str] = set()                     # ebben a folyamatban figyelt op-ok (a resume_loop ezeket kihagyja)
# művelet → utófeldolgozás (a letöltött szolgáltatói fájlokból a felhasználónak átadott eredmény; app/tools/ai_video alul)
POST: dict[str, Callable[[list[dict[str, Any]], list[list[str]]], Awaitable[dict[str, Any]]]] = {}


class AIVideoError(ValueError):
    """Érthető hiba a felhasználónak (rossz bemenet, kikapcsolt funkció, szolgáltatói hiba)."""


def _provider(name: str = "kie"):
    from app import providers
    return providers.get(name)


def require_enabled() -> None:
    from app import providers
    if tenancy.is_guest():
        from app.auth import ACCOUNT_NEEDED
        raise PermissionError(ACCOUNT_NEEDED)
    if not providers.enabled_for():
        raise AIVideoError("AI video (generated clips, Topaz enhance) isn't switched on on this server yet.")
    if not db.enabled():
        raise AIVideoError("AI video needs the database (DATABASE_URL).")


def callback_url() -> Optional[str]:
    """A kie callback-címe: csak ha van titok (KIE_WEBHOOK_SECRET az URL-ben és/vagy a kie HMAC-kulcsa,
    KIE_WEBHOOK_HMAC_KEY) és nyilvános a szerver — különben csak lekérdezés."""
    from app.config import settings
    secret, hkey = os.getenv("KIE_WEBHOOK_SECRET", ""), os.getenv("KIE_WEBHOOK_HMAC_KEY", "")
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    if not (secret or hkey) or "localhost" in base or "127.0.0.1" in base or "testserver" in base:
        return None
    return f"{base}/webhooks/kie" + (f"?token={secret}" if secret else "")


# ---------- nyilvántartás ----------
#
# A MEGSZAKÍTÁS SZABÁLYA (egy szabály minden útra — DELETE /api/ai/task, a job megszakítása, MCP jobs cancel):
#   • amíg egyik rész sincs a szolgáltatónál: semmi nem indul el, a foglalás teljesen visszajár;
#   • ha már ott van: a kie-feladat nem állítható le (nincs ilyen API-ja), és fizetünk érte — ezért a foglalás marad,
#     a feladat végén a felhasználó a TÉNYLEGES költséget fizeti (hibánál 0), az eredmény a fájljai közé kerül, de a
#     timeline-ra nem tesszük. Ugyanígy: ha egy rész nem indult el, vagy túl sokáig tart, NEM engedjük el a foglalást,
#     amíg a már elindult részek futnak — a végén a szolgáltató költsége a könyvbe kerül (hiba: a felhasználónak 0).

async def _rows_of(op: str) -> list[dict[str, Any]]:
    return await db._rows("SELECT * FROM provider_tasks WHERE id = %s OR meta->>'op' = %s ORDER BY (meta->>'i')::int NULLS FIRST",
                          (op, op))


async def _update(rid: str, **cols: Any) -> None:
    from psycopg.types.json import Jsonb
    sets, vals = [], []
    for k, v in cols.items():
        if k == "meta_merge":
            sets.append("meta = meta || %s")
            vals.append(Jsonb(v))
        else:
            sets.append(f"{k} = %s")
            vals.append(Jsonb(v) if k in ("result", "input", "meta") else v)
    await db._rows(f"UPDATE provider_tasks SET {', '.join(sets)}, updated_at = now() WHERE id = %s RETURNING id", (*vals, rid))


def _launched(rows: list[dict[str, Any]]) -> bool:
    return any(r["provider_task_id"] for r in rows)


def public(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Egy op állapota a felhasználónak (a szolgáltatói adat — URL-ek, azonosítók, költség — nélkül)."""
    head = rows[0]
    settled = all(r["settled"] for r in rows)
    cancelled = bool(head["meta"].get("cancel_requested")) or any(r["status"] == "cancelled" for r in rows)
    if settled:
        st = "cancelled" if cancelled else "done" if head["meta"].get("output") else "failed"
    else:
        st = "cancelling" if cancelled else "running"
    out = {"task_id": head["id"], "operation": head["operation"], "status": st,
           "parts": len(rows), "parts_done": sum(1 for r in rows if r["status"] in ("success", "fail", "cancelled")),
           "credits_held": head["hold"], "credits_charged": int(head["charged"] or 0) if settled else 0,
           "created_at": head["created_at"]}
    if head["meta"].get("output"):
        out["result"] = head["meta"]["output"]
    err = next((r["error"] for r in rows if r["error"]), None) or head["meta"].get("abort")
    if err:
        out["error"] = err
    if st == "cancelling":
        out["note"] = ("Cancelled — but the AI service already had the job and can't be stopped: when it ends you pay what "
                       "it actually charged (nothing if it fails), and the result goes to your files.")
    return out


async def get(op: str) -> dict[str, Any]:
    rows = await _rows_of(op)
    if not rows or rows[0]["user_id"] != tenancy.current_user_id():
        raise AIVideoError(f"No such AI task: {op}")
    return public(rows)


@asynccontextmanager
async def _context(row: dict[str, Any], files: Optional[list[str]] = None):
    """A művelet gazdája és — megosztott fájlnál — a tulajdonos munkaterülete (app/sharing.py): a webhook és az újraindítás
    utáni folytatás is ott keresi a forrást és oda teszi az eredményt, ahol a kérés tette volna. A jogot MOST kérdezzük
    le (nem a foglaláskori másolatból): ha a megosztást közben visszavonták, a tulajdonosnál már nem ír. files: amit ez
    a művelet maga hozott létre ott (a letöltött nyers eredmények — a saját DB-sorainkból), azokhoz hozzáfér."""
    tok = tenancy.set_user(row["user_id"])
    ws = row["meta"].get("workspace")
    try:
        if ws and ws.get("owner") and ws["owner"] != row["user_id"]:
            from app import sharing
            roles = await sharing.family_roles(ws["owner"], row["user_id"])
            with sharing.workspace(ws["owner"], row["user_id"], roles, write=True) as sh:
                sh.created.update(f for f in files or () if f)
                yield
        else:
            yield
    finally:
        tenancy.reset(tok)


# ---------- indítás ----------

async def reserve(operation: str, model: str, *, hold: int, meta: dict[str, Any], provider: str = "kie") -> str:
    """1. lépés, a kérésen belül: hold a becsült maximumra (fedezet nélkül InsufficientCredits — semmi nem indul) és az op
    sora („created”). Az előkészítés (feltöltés, darabolás) utána jön a jobban; ha az elakad, a hold visszajár."""
    uid = tenancy.current_user_id()
    _provider(provider)
    if hold <= 0 and uid != tenancy.ADMIN_ID:     # ár nélkül (törölt / 0 szabály) nem adjuk ingyen a fizetős szolgáltatást
        raise AIVideoError("This AI feature has no price set on this server yet — ask the admin.")
    op = uuid.uuid4().hex[:20]
    held = (await credits.hold(uid, hold, op, operation=operation, provider=provider,
                               meta={"model": model, **{k: meta[k] for k in ("units", "tier") if k in meta}}))["held"]
    ws, sh = tenancy.workspace_id(), tenancy.shared()
    if ws != uid and sh is not None:              # megosztott fájlon: a tulajdonos munkaterülete (a befejezéshez)
        meta = {**meta, "workspace": {"owner": ws, "roles": dict(getattr(sh, "roles", {}) or {})}}
    from psycopg.types.json import Jsonb
    await db._rows("""INSERT INTO provider_tasks (id, user_id, provider, model, operation, hold, meta)
                      VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                   (op, uid, provider, model, operation, held, Jsonb({**meta, "op": op, "i": 0, "n": 1})))
    _watching.add(op)
    return op


async def launch(op: str, inputs: list[dict[str, Any]]) -> None:
    """2. lépés: a szolgáltatói feladat(ok) — egy op több része (hosszú videó darabjai) külön sor, közös hold. Az op
    zárja alatt: a megszakítás vagy előtte jön (semmi nem indul), vagy utána (a már elindult rész fizetős)."""
    from psycopg.types.json import Jsonb
    async with _lock(op):
        rows = await _rows_of(op)
        head = rows[0]
        if head["settled"]:
            raise AIVideoError("The AI job was cancelled before it started — nothing was charged.")
        prov = _provider(head["provider"])
        await _update(op, input=inputs[0], meta_merge={"n": len(inputs)})
        ids = [op] + [f"{op}-{i}" for i in range(1, len(inputs))]
        for i, (rid, inp) in enumerate(zip(ids[1:], inputs[1:]), 1):
            await db._rows("""INSERT INTO provider_tasks (id, user_id, provider, model, operation, input, hold, meta)
                              VALUES (%s, %s, %s, %s, %s, %s, 0, %s) RETURNING id""",
                           (rid, head["user_id"], head["provider"], head["model"], head["operation"], Jsonb(inp),
                            Jsonb({**head["meta"], "i": i, "n": len(inputs)})))
        cb = callback_url()
        for rid, inp in zip(ids, inputs):
            try:
                tid = await prov.create(head["model"], inp, callback_url=cb)
            except Exception as e:
                log.warning("AI-feladat nem indult (%s %s): %s", head["operation"], head["model"], e)
                # ha egy korábbi rész már fut: a foglalás marad, amíg az véget nem ér (a költsége a könyvbe kerül)
                await _abort(op, _friendly(e), locked=True)
                if isinstance(e, ProviderError) and e.code == 402:
                    await credits.notice(f"{head['provider']}_out_of_credits", f"{head['provider']} refused a job: no "
                                                                                "credits left on the provider account. Top it up.")
                raise AIVideoError(_friendly(e)) from None
            await _update(rid, provider_task_id=tid, status="running")
    log.info("AI-feladat indult: %s %s ×%d (hold %d)", head["operation"], head["model"], len(inputs), head["hold"])


async def submit(operation: str, model: str, inputs: list[dict[str, Any]], *, hold: int, meta: dict[str, Any],
                 provider: str = "kie") -> str:
    """reserve + launch egyben (ha a bemenet már kész). Vissza: az op azonosítója."""
    op = await reserve(operation, model, hold=hold, meta=meta, provider=provider)
    try:
        await launch(op, inputs)
    finally:
        _watching.discard(op)
    return op


async def run_reserved(op: str, prepare: Callable[[], Awaitable[list[dict[str, Any]]]],
                       progress: Optional[Callable[..., None]] = None) -> dict[str, Any]:
    """A jobban: előkészítés → launch → várakozás. A job megszakítása (CancelledError) a megszakítás szabálya szerint;
    az előkészítés hibája visszaadja a foglalást; a várakozás hibája után a resume_loop fejezi be."""
    stage = "prepare"
    try:
        inputs = await prepare()
        stage = "launch"
        await asyncio.shield(launch(op, inputs))         # a szolgáltatónál vagy elindul, vagy nem — félúton nem szakad
        stage = "wait"
        return await wait(op, progress)
    except BaseException as e:
        _watching.discard(op)
        if isinstance(e, asyncio.CancelledError):
            await asyncio.shield(cancel_op(op))
        elif stage == "prepare":
            await asyncio.shield(_abort(op, f"the input couldn't be prepared: {str(e)[:200]}"))
        raise


def _friendly(e: Exception) -> str:
    if isinstance(e, ProviderError):
        if e.code == 402:
            return "The AI video service is out of credit on our side — try again later (your credits were not used)."
        if e.code in (422, 400):
            return f"The AI video service refused the request: {str(e).split(':', 1)[-1].strip()[:200]}"
        if e.code == 429:
            return "The AI video service is busy — try again in a minute (your credits were not used)."
    return f"The AI video service didn't take the job ({str(e)[:160]}). Your credits were not used."


async def _abort(op: str, error: str, status: str = "fail", *, locked: bool = False) -> None:
    """Az op lezárása eredmény nélkül: a hold visszajár — DE csak ha egyik rész sem fut a szolgáltatónál. Ha fut, a még el
    nem indult részek hibásak lesznek, és az op a futók végén zárul (_finish_op), a szolgáltatói költséggel."""
    if not locked:
        async with _lock(op):
            return await _abort(op, error, status, locked=True)
    rows = await _rows_of(op)
    if not rows or all(r["settled"] for r in rows):
        return
    head = rows[0]
    if any(r["status"] == "running" for r in rows):
        for r in rows:
            if r["status"] == "created":
                await _update(r["id"], status="fail", error=error[:500], finished_at=_now())
        await _update(head["id"], meta_merge={"abort": error[:300]})
        return
    await credits.release(head["user_id"], op, head["hold"], operation=head["operation"], provider=head["provider"],
                          provider_cost=sum(float(r["provider_cost"] or 0) for r in rows),
                          meta={"model": head["model"], "error": error[:200]})
    for r in rows:
        await _update(r["id"], status=r["status"] if r["status"] in ("success", "fail") and status == "fail" else status,
                      settled=True, error=r["error"] or error[:500], finished_at=_now())
    await _cleanup(head, rows, None)                    # amit nem fizetett ki, az nem marad nála (pl. félkész darabok)
    _wake(op)


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _lock(op: str) -> asyncio.Lock:
    return _locks.setdefault(op, asyncio.Lock())


def _wake(op: str) -> None:
    ev = _events.get(op)
    if ev:
        ev.set()


# ---------- befejezés ----------

async def on_state(row: dict[str, Any], st: TaskState) -> None:
    """Egy szolgáltatói feladat végállapota (lekérdezésből vagy webhookból): siker → az eredmény AZONNAL a saját
    tárhelyre (a művelet munkaterületén); ha az op minden része kész → utófeldolgozás és elszámolás. Idempotens (a zár +
    a settled jelző + a könyv kulcsai)."""
    op = row["meta"].get("op") or row["id"]
    async with _lock(op):
        fresh = (await db._rows("SELECT * FROM provider_tasks WHERE id = %s", (row["id"],)))[0]
        if fresh["settled"] or fresh["status"] in ("success", "fail", "cancelled"):
            pass
        elif st.state == SUCCESS and not st.urls:
            await _update(fresh["id"], status="fail", provider_cost=st.cost, error="the AI service returned no file",
                          finished_at=_now())
        elif st.state == SUCCESS:
            try:
                async with _context(fresh):
                    files = await _provider(fresh["provider"]).fetch_result(st)
            except Exception as e:
                log.warning("AI-eredmény letöltése nem sikerült (%s): %s", fresh["id"], e)
                files, st = [], TaskState(task_id=st.task_id, state=FAIL, cost=st.cost,
                                          error=f"the result couldn't be downloaded ({str(e)[:120]})")
            if st.state == SUCCESS:
                await _update(fresh["id"], status="success", result=files, provider_cost=st.cost, finished_at=_now(),
                              meta_merge={k: v for k, v in st.extra.items() if v})
            else:
                await _update(fresh["id"], status="fail", provider_cost=st.cost, error=st.error, finished_at=_now())
        else:
            await _update(fresh["id"], status="fail", provider_cost=st.cost, error=(st.error or "failed")[:500],
                          finished_at=_now())
    await _finish_op(op)


async def _finish_op(op: str) -> None:
    """Ha az op minden része végállapotban van: utófeldolgozás és elszámolás — egyszer (az op zárja alatt)."""
    async with _lock(op):
        await _finish_op_locked(op)


async def _finish_op_locked(op: str) -> None:
    rows = await _rows_of(op)
    if not rows or all(r["settled"] for r in rows) or any(r["status"] not in ("success", "fail") for r in rows):
        return
    head = rows[0]
    cancelled = bool(head["meta"].get("cancel_requested"))
    failed = any(r["status"] == "fail" for r in rows)
    cost = sum(float(r["provider_cost"] or 0) for r in rows)
    if failed and not cancelled:              # a felhasználónak 0 — a szolgáltató költsége a feloldáson a könyvben
        await _abort(op, next((r["error"] for r in rows if r["status"] == "fail"), None) or "failed", locked=True)
        return
    output = head["meta"].get("output")
    if not failed and not output:             # utófeldolgozás EGYSZER: az eredmény a meta-ban, mielőtt elszámolunk
        try:
            async with _context(head, [f for r in rows for f in r["result"] or []]):
                post = POST.get(head["operation"])
                output = await post(rows, [list(r["result"] or []) for r in rows]) if post else \
                    {"files": [f for r in rows for f in r["result"] or []]}
        except Exception as e:
            log.exception("AI-utófeldolgozás hiba (%s)", op)
            if not cancelled:
                await _abort(op, f"the result couldn't be finished: {str(e)[:200]}", locked=True)
                return
            output = None
        if output:
            await _update(head["id"], meta_merge={"output": output})
    rule = await credits.rule_for(head["operation"], head["model"], head["meta"].get("tier") or "")
    est = credits.price(rule, float(head["meta"].get("units") or 1))
    charge = credits.price(rule, provider_units=cost) if any(r["provider_cost"] is not None for r in rows) else est
    res = await credits.settle(head["user_id"], op, head["hold"], charge, operation=head["operation"],
                               provider=head["provider"], provider_task_id=head["provider_task_id"], provider_cost=cost,
                               meta={"model": head["model"], "tier": head["meta"].get("tier"), "parts": len(rows),
                                     "estimate": est, **({"cancelled": True} if cancelled else {})})
    if res["charged"] or head["charged"] is None:
        await _update(head["id"], charged=res["charged"])
    for r in rows:
        await _update(r["id"], settled=True)
    await _cleanup(head, rows, output)
    log.info("AI-feladat kész: %s %s — %s kredit (szolgáltató: %s)%s", head["operation"], op, res["charged"], cost,
             " — megszakítva" if cancelled else "")
    _wake(op)


async def _cleanup(head: dict[str, Any], rows: list[dict[str, Any]], output: Optional[dict[str, Any]]) -> None:
    """A nyers szolgáltatói letöltések, amik nem lettek maguk az eredmény (pl. a felskálázás darabjai)."""
    from app.storage import output_path
    keep = {(output or {}).get("file_id")} | set((output or {}).get("files") or [])
    async with _context(head, [f for r in rows for f in r["result"] or []] + [f for f in keep if f]):
        for r in rows:
            for fid in r["result"] or []:
                if fid not in keep:
                    try:
                        output_path(fid).unlink(missing_ok=True)
                    except (OSError, ValueError):
                        pass


async def cancel_op(op: str) -> dict[str, Any]:
    """A megszakítás szabálya (fent): el nem indult op → teljes visszaadás; már a szolgáltatónál lévő → a foglalás marad,
    a végén a tényleges költség, az eredmény a fájlok közé."""
    finish = False
    async with _lock(op):
        rows = await _rows_of(op)
        if not rows or all(r["settled"] for r in rows):
            return public(rows) if rows else {}
        if _launched(rows):
            for r in rows:
                if r["provider_task_id"] and r["status"] == "running":
                    try:
                        await _provider(r["provider"]).cancel(r["provider_task_id"])
                    except Exception as e:
                        log.info("szolgáltatói megszakítás (%s): %s", r["id"], e)
            await _update(rows[0]["id"], meta_merge={"cancel_requested": True})
            for r in rows:                    # a még el nem indult részek már nem indulnak
                if r["status"] == "created":
                    await _update(r["id"], status="fail", error="cancelled", finished_at=_now())
            finish = True
        else:
            await _abort(op, "cancelled", status="cancelled", locked=True)
    if finish:
        await _finish_op(op)
    _wake(op)
    return public(await _rows_of(op))


async def cancel(op: str) -> dict[str, Any]:
    """DELETE /api/ai/task/{op}: a hívó saját feladata; a hozzá tartozó job is leáll (ugyanaz a szabály)."""
    rows = await _rows_of(op)
    if not rows or rows[0]["user_id"] != tenancy.current_user_id():
        raise AIVideoError(f"No such AI task: {op}")
    from app import jobs
    j = jobs._JOBS.get(str(rows[0]["meta"].get("job") or ""))
    if j and j.get("status") == "running" and j.get("_task") and j.get("owner") == rows[0]["user_id"]:
        j["_task"].cancel()                   # a job a saját megszakítási ágán hívja a cancel_op-ot
        await asyncio.sleep(0)
    return await cancel_op(op)


# ---------- várakozás: lekérdezés visszalépéssel; a webhook felébreszti ----------

async def wait(op: str, progress: Optional[Callable[..., None]] = None, timeout: float = TASK_TIMEOUT_S) -> dict[str, Any]:
    ev = _events.setdefault(op, asyncio.Event())
    _watching.add(op)
    t0, delay = time.monotonic(), POLL_FIRST_S
    try:
        while True:
            rows = await _rows_of(op)
            if all(r["settled"] for r in rows):
                return public(rows)
            if time.monotonic() - t0 > timeout:   # nem engedjük el: a resume_loop tovább figyeli, az eredmény a fájlok közé jön
                raise AIVideoError(f"The AI service is still working on it after {timeout / 3600:g} h — the result will "
                                   f"appear in your files when it ends (task {op}).")
            if progress:
                done = sum(1 for r in rows if r["status"] in ("success", "fail"))
                progress(done, len(rows), "generating" if done < len(rows) else "finishing")
            await poll(rows)
            try:
                await asyncio.wait_for(ev.wait(), timeout=delay)
                ev.clear()
            except asyncio.TimeoutError:
                pass
            delay = min(POLL_MAX_S, delay * 1.6)
    finally:
        _watching.discard(op)
        _events.pop(op, None)


async def poll(rows: list[dict[str, Any]]) -> None:
    for r in rows:
        if r["settled"] or r["status"] != "running" or not r["provider_task_id"]:
            continue
        try:
            st = await _provider(r["provider"]).status(r["provider_task_id"])
        except Exception as e:                # a szolgáltató nem érhető el / nincs beállítva / hibás válasz: később újra
            log.info("AI-állapot nem jött (%s): %s", r["id"], e)
            continue
        if st.state in TERMINAL:
            await on_state(r, st)
    rows = await _rows_of(rows[0]["meta"].get("op") or rows[0]["id"])
    if not all(r["settled"] for r in rows) and all(r["status"] in ("success", "fail") for r in rows):
        await _finish_op(rows[0]["meta"].get("op") or rows[0]["id"])


async def on_webhook(provider_task_id: str) -> str:
    """A callback csak jelzés: a végeredményt a szolgáltatótól kérdezzük le (a kie aláírása sem fedi a törzset)."""
    rows = await db._rows("SELECT * FROM provider_tasks WHERE provider_task_id = %s", (provider_task_id,))
    if not rows:
        return "unknown"
    row = rows[0]
    if row["settled"]:
        return "duplicate"
    st = await _provider(row["provider"]).status(provider_task_id)
    if st.state not in TERMINAL:
        return "not finished"
    await on_state(row, st)
    _wake(row["meta"].get("op") or row["id"])
    return "settled"


GIVE_UP_S = float(os.getenv("AI_TASK_GIVE_UP_D", "14")) * 86400     # a kie 2 hónapig őrzi a feladat adatait


async def resume_once() -> None:
    """Egy kör: a nyitott op-ok, amikre ebben a folyamatban senki nem vár."""
    rows = await db._rows("SELECT * FROM provider_tasks WHERE NOT settled ORDER BY created_at LIMIT 200")
    ops: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        ops.setdefault(r["meta"].get("op") or r["id"], []).append(r)
    for op, rs in ops.items():
        if op in _watching:
            continue
        age = time.time() - _ts(rs[0]["created_at"])
        try:                                  # egy hibás op nem állítja meg a többit
            if not _launched(rs) and age > 1800:
                await _abort(op, "the job never reached the AI service")
            elif _launched(rs) and age > GIVE_UP_S:
                await _give_up(op)
            else:
                await poll(await _rows_of(op))
        except Exception:
            log.exception("AI-feladat folytatása (%s)", op)


async def _give_up(op: str) -> None:
    """Hetekig nincs végeredmény a szolgáltatónál: a foglalás visszajár, az admin figyelmeztetést kap (a kie naplója dönt)."""
    async with _lock(op):
        rows = await _rows_of(op)
        if not rows or all(r["settled"] for r in rows):
            return
        for r in rows:
            if r["status"] in ("running", "created"):
                await _update(r["id"], status="fail", error="no answer from the AI service", finished_at=_now())
        await _update(rows[0]["id"], meta_merge={"cancel_requested": False})
        await _abort(op, "no answer from the AI service", locked=True)
    await credits.notice("ai_task_given_up", f"AI task {op} got no final answer from {rows[0]['provider']} for "
                                             f"{GIVE_UP_S / 86400:g} days; its hold was released — check the provider's log.",
                         {"op": op, "provider_task_ids": [r["provider_task_id"] for r in rows]}, once_per_s=0)


async def resume_loop() -> None:
    """Újraindítás után (és ha senki nem vár rá) — MINDIG fut, ha van adatbázis (a kapcsoló kikapcsolása után is): a
    nyitott feladatok lekérdezése; az el sem indult op 30 perc után visszaadja a holdot."""
    await asyncio.sleep(20)
    while True:
        try:
            await resume_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("AI-feladatok folytatása")
        await asyncio.sleep(60)


def _ts(v: Any) -> float:
    from datetime import datetime
    return datetime.fromisoformat(str(v)).timestamp()


# ---------- modellek és becslés (OMLETT 2.2) ----------

CLIP_MODEL = "bytedance/seedance-2-5"             # szöveg→videó, kép→videó (első / első+utolsó kocka), referencia mód
FINAL_MODEL = "bytedance/seedance-2-5-draft"      # egy draft (480p) véglegesítése 1080p-re, ugyanazzal a mozgással
UPSCALE_MODEL = "topaz/video-upscale"             # 1x / 2x / 4x
RESOLUTIONS = ("480p", "720p", "1080p")
ASPECTS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive")
CLIP_MIN_S, CLIP_MAX_S = 4, 30
FACTORS = (1, 2, 4)
TOPAZ_MAX_MB = 50                                 # a kie API bemeneti korlátja (mp4 / mov / mkv)
# élesítő motorok (ENHANCE_ENGINE): név → (szolgáltató, modell, a bemenet többi mezője, egy darab legfeljebb MB).
# A szerveren nincs GPU: mindegyik a szolgáltatónál fut. Alap: SeedVR2 a fal-on, ha van FAL_KEY (a tömörített, kicsinyített
# régi anyag referenciája), különben a kie Topaza.
ENGINES: dict[str, tuple[str, str, dict[str, Any], Optional[float]]] = {
    "seedvr": ("fal", "fal-ai/seedvr/upscale/video", {}, 95),
    "topaz-starlight": ("fal", "fal-ai/topaz/upscale/video", {"topaz_model": "Starlight Precise 2.5"}, 95),
    "topaz-proteus": ("fal", "fal-ai/topaz/upscale/video", {"topaz_model": "Proteus"}, 95),
    "topaz": ("kie", UPSCALE_MODEL, {}, None),     # None = TOPAZ_MAX_MB (hívás idején olvasva)
}


def enhance_engine() -> str:
    from app import providers
    e = os.getenv("ENHANCE_ENGINE", "").strip().lower()
    if e in ENGINES:
        return e
    return "seedvr" if providers.configured("fal") else "topaz"


def _enhance_units(engine: str, f: int, info: dict[str, Any], secs: int) -> tuple[str, float]:
    """Az élesítés árának szintje és egységei a motor árlapja szerint: SeedVR2 = kimeneti megapixel (szélesség × magasság ×
    képkockák), a fal Topaza = mp a kimenet felbontásának sávjában, a kie Topaza = mp a szorzó szerint."""
    w, h = int(info["width"]) * f, int(info["height"]) * f
    if engine == "seedvr":
        fps = float(info.get("fps") or 30) or 30.0
        return "any", float(math.ceil(w * h * fps * secs / 1e6))
    if ENGINES[engine][0] == "fal":
        short = min(w, h)
        return ("720p" if short <= 720 else "1080p" if short <= 1080 else "4k"), float(secs)
    return f"{f}x", float(secs)
REF_LIMITS = {"reference_images": 30, "reference_videos": 10, "reference_audios": 10}


def _ids(v: Any) -> list[str]:
    if v is None:
        return []
    return [str(x.get("file_id") if isinstance(x, dict) else x) for x in (v if isinstance(v, list) else [v]) if x]


def clip_options(o: dict[str, Any]) -> dict[str, Any]:
    """Az AI-klip beállításai, ellenőrizve: mode = text | first_frame | first_last | reference (nem keverhető)."""
    o = dict(o or {})
    prompt = str(o.get("prompt") or "").strip()
    if not prompt and not o.get("finalize"):
        raise AIVideoError('Say what should happen in the clip: ai={"prompt": "…"} (or text=…).')
    draft = bool(o.get("draft"))
    res = str(o.get("resolution") or ("480p" if draft else "720p")).lower().replace("p", "") + "p"
    if res not in RESOLUTIONS:
        raise AIVideoError(f"resolution: {' | '.join(RESOLUTIONS)}")
    if draft and res != "480p":
        raise AIVideoError("A draft is always 480p (cheap preview) — finalise it to 1080p afterwards.")
    try:
        dur = int(round(float(o.get("duration") or 5)))
    except (TypeError, ValueError):
        raise AIVideoError("duration: seconds, 4–30") from None
    if not CLIP_MIN_S <= dur <= CLIP_MAX_S:
        raise AIVideoError(f"duration: {CLIP_MIN_S}–{CLIP_MAX_S} seconds (got {dur})")
    first = o.get("first_frame") or o.get("image")
    last = o.get("last_frame")
    refs = {k: _ids(o.get(k)) for k in REF_LIMITS}
    if last and not first:
        raise AIVideoError("A last frame needs a first frame too (first_frame / image).")
    if (first or last) and any(refs.values()):
        raise AIVideoError("First/last-frame mode and reference mode can't be mixed — use one: first_frame (+ last_frame) "
                           "OR reference_images / reference_videos / reference_audios.")
    for k, n in REF_LIMITS.items():
        if len(refs[k]) > n:
            raise AIVideoError(f"{k}: at most {n}")
    aspect = str(o.get("aspect") or ("adaptive" if first or any(refs.values()) else "16:9"))
    if aspect not in ASPECTS:
        raise AIVideoError(f"aspect: {' | '.join(ASPECTS)}")
    mode = "reference" if any(refs.values()) else "first_last" if last else "first_frame" if first else "text"
    return {"prompt": prompt, "resolution": res, "duration": dur, "audio": bool(o.get("audio", False)), "draft": draft,
            "aspect": aspect, "mode": mode, "first_frame": first, "last_frame": last, **refs}


async def _ref_video_seconds(ids: list[str]) -> float:
    from app.tools import crafts, video
    total = 0.0
    for fid in ids:
        total += await true_seconds(crafts.existing(fid))
    if total > 30:
        raise AIVideoError(f"Reference videos may be 30 seconds in total (these are {total:.1f} s).")
    return total


async def estimate_clip(o: dict[str, Any]) -> dict[str, Any]:
    """Az AI-klip ára (Studio-kredit) a becsült maximumra — ezt foglaljuk le; a levonás a tényleges költség."""
    if o.get("finalize"):
        fin = await _draft_row(str(o["finalize"]))       # csak a hívó saját, kész draftja
        dur = int((fin["input"] or {}).get("duration") or fin["meta"].get("duration") or 5)
        est = await credits.estimate("ai_clip", FINAL_MODEL, "1080p", dur)
        return {**est, "what": f"final 1080p of the draft ({dur} s)"}
    c = clip_options(o)
    ref_s = await _ref_video_seconds(c["reference_videos"]) if c["reference_videos"] else 0.0
    tier = c["resolution"] + ("+video" if ref_s else "")
    units = c["duration"] + (math.ceil(ref_s) if ref_s else 0)
    est = await credits.estimate("ai_clip", CLIP_MODEL, tier, units)
    return {**est, "what": f"{'draft ' if c['draft'] else ''}{c['resolution']} {c['duration']} s clip"
                           + (f" (+{ref_s:.0f} s reference video)" if ref_s else "")}


async def estimate_enhance(file_id: str, factor: Any) -> dict[str, Any]:
    from app.tools import crafts, video
    f = _factor(factor)
    path = crafts.existing(file_id)
    info = await video.probe_movie(path)
    secs = max(1, math.ceil(await true_seconds(path) - 1e-6))
    engine = enhance_engine()
    tier, units = _enhance_units(engine, f, info, secs)
    est = await credits.estimate("enhance", ENGINES[engine][1], tier, units)
    return {**est, "what": f"{f}x enhance of {secs} s ({info['width']}×{info['height']} → "
                           f"{info['width'] * f}×{info['height'] * f})", "seconds": secs, "engine": engine, "tier": tier,
            "units": units}


def _factor(v: Any) -> int:
    try:
        f = int(float(str(v or 2).lower().rstrip("x")))
    except ValueError:
        f = 0
    if f not in FACTORS:
        raise AIVideoError("factor: 1 (clean up, same size) | 2 | 4")
    return f


# ---------- közös: job ----------

async def _start_job(kind: str, est: dict[str, Any], work: Callable[..., Awaitable[dict[str, Any]]], op: str) -> dict[str, Any]:
    from app import jobs
    j = jobs.start(kind, work)
    await _update(op, meta_merge={"job": j["id"]})
    return {**j, "job_id": j["id"], "task_id": op, "estimate": {k: est[k] for k in ("credits", "what") if k in est},
            "how": 'Call video(action="status", job_id=…) until status is "done" (result) or "failed"; the estimated '
                   "credits are reserved now and only what the AI service actually charged is taken."}


async def _source_video(file_id: str) -> str:
    """Videó file_id; egy videóprojekt (.ecproj) → a legutóbbi renderje."""
    from app.storage import output_path, read_meta
    from app.tools import crafts, video
    if file_id.lower().endswith(".ecproj"):
        rid = (read_meta(file_id) or {}).get("render_file_id")
        if not rid or not output_path(rid).exists():
            raise AIVideoError("This video project has no render yet — render it first (video action=render).")
        file_id = rid
    ext = file_id.rsplit(".", 1)[-1].lower() if "." in file_id else ""
    if ext not in video.MOVIE_EXTS:
        raise AIVideoError("Enhance takes a video (mp4, mov, webm, m4v, mkv) or a video project.")
    crafts.existing(file_id)
    return file_id


# ---------- „Minőségjavítás” (Topaz, OMLETT 2.5) ----------

async def _ffmpeg(*args: str) -> None:
    from app.config import settings
    from app.convert.engines import run
    await run("ffmpeg", "-v", "error", "-y", *args, timeout=settings.VIDEO_TIMEOUT_S)


async def _topaz_inputs(src_path, info: dict[str, Any], work, paid_s: float, max_mb: Optional[float] = None) -> list:
    """Az élesítő bemenete(i) ≤ max_mb (a kie Topaza 50 MB, a fal 95), legfeljebb a KIFIZETETT másodpercek (-t: a fejlécben hamisított hossz mögötti képet
    sem töltjük fel): mp4/mov/mkv → kép-másolással; WebM / m4v → mp4; nagyobb → időben darabolva, kép-másolással
    (kulcskockánál vágva), a túl nagy darab célbitrátára kódolva. Vissza: a darabok útvonalai."""
    limit = (max_mb or TOPAZ_MAX_MB) * 1024 * 1024 * 0.9
    ext = src_path.suffix.lower().lstrip(".")
    base = work / "in.mp4"
    if ext not in ("mp4", "mov", "mkv"):          # webm / m4v → mp4 (H.264, jó minőségben; a hang külön jön vissza)
        await _ffmpeg("-i", str(src_path), "-t", f"{paid_s:.3f}", "-map", "0:v:0", "-an", "-c:v", "libx264", "-crf", "16",
                      "-preset", "veryfast", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(base))
    else:
        await _ffmpeg("-i", str(src_path), "-t", f"{paid_s:.3f}", "-map", "0:v:0", "-an", "-c:v", "copy",
                      "-movflags", "+faststart", str(base))
    if base.stat().st_size <= limit:
        return [base]
    dur = max(1.0, float(info["duration"] or 1))
    seg = max(2.0, dur * limit * 0.8 / base.stat().st_size)
    await _ffmpeg("-i", str(base), "-map", "0:v:0", "-c", "copy", "-f", "segment", "-segment_time", f"{seg:.2f}",
                  "-reset_timestamps", "1", "-segment_format", "mp4", str(work / "part%03d.mp4"))
    parts = sorted(work.glob("part*.mp4"))
    for k, p in enumerate(parts):                  # ritka kulcskocka → túl nagy darab: célbitrátára kódolva
        if p.stat().st_size > limit:
            pd = float((await _probe(p))["duration"] or seg)
            kbps = int(limit * 8 / 1000 / max(1.0, pd) * 0.9)
            q = work / f"partq{k:03d}.mp4"
            await _ffmpeg("-i", str(p), "-c:v", "libx264", "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize",
                          f"{kbps * 2}k", "-pix_fmt", "yuv420p", "-an", str(q))
            q.replace(p)
    return parts


async def true_seconds(path) -> float:
    """A felvétel valódi hossza: a fejléc (format / stream) ÉS a ténylegesen meglévő képkockák (csomagok ÷ fps) közül a
    nagyobb — egy „1 s”-ra hamisított fejléc mögötti 20 s kép így 20 s-ként fizet (és a feltöltést -t úgyis levágja)."""
    from app.convert.engines import run
    out, _ = await run("ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-print_format", "json",
                       "-show_entries", "stream=duration,nb_read_packets,avg_frame_rate,r_frame_rate:format=duration",
                       str(path), timeout=300)
    d = json.loads(out.decode("utf-8", "replace") or "{}")
    st = (d.get("streams") or [{}])[0]

    def num(v: Any) -> float:
        try:
            a, _, b = str(v or "0").partition("/")
            return float(a) / float(b) if b else float(a)
        except (ValueError, ZeroDivisionError):
            return 0.0
    rates = [r for r in (num(st.get("avg_frame_rate")), num(st.get("r_frame_rate"))) if 1 <= r <= 240]
    cands = [num((d.get("format") or {}).get("duration")), num(st.get("duration"))]
    if rates and num(st.get("nb_read_packets")):
        cands.append(num(st.get("nb_read_packets")) / min(rates))
    return max(cands + [0.0])


async def _probe(path) -> dict[str, Any]:
    from app.tools import video
    return await video.probe_movie(path)


async def start_enhance(file_id: str, factor: Any) -> dict[str, Any]:
    """Topaz felskálázás (1x = tisztítás azonos méreten, 2x, 4x) → job; az eredmény a fájl ÚJ változata (verziófa)."""
    require_enabled()
    f = _factor(factor)
    src = await _source_video(file_id)
    est = await estimate_enhance(src, f)
    prov_name, model, extra, max_mb = ENGINES[est["engine"]]
    op = await reserve("enhance", model, hold=est["credits"], provider=prov_name,
                       meta={"units": est["units"], "tier": est["tier"], "source": src, "factor": f, "engine": est["engine"]})

    async def prepare() -> list[dict[str, Any]]:
        import shutil
        from app.storage import temp_path
        from app.tools import crafts
        path = crafts.existing(src)
        tmp = temp_path(f"enh_{uuid.uuid4().hex[:10]}")
        tmp.mkdir(parents=True)
        try:
            parts = await _topaz_inputs(path, await _probe(path), tmp, float(est["seconds"]), max_mb)
            prov = _provider(prov_name)
            return [{"video": await prov.upload(p), "factor": f, **extra} for p in parts]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def work(progress):
        progress(0, 1, "preparing the video")
        return _job_result(await run_reserved(op, prepare, progress))
    return await _start_job("ai_video", est, work, op)


def _job_result(res: dict[str, Any]) -> dict[str, Any]:
    if res["status"] == "cancelled":
        raise AIVideoError(f"The AI job was cancelled — {res['credits_charged']} credits charged (what the AI service "
                           "really charged before it could be stopped)" + (", the result is in your files." if res.get("result") else "."))
    if res["status"] != "done":
        raise AIVideoError(f"The AI job {res['status']}: {res.get('error') or 'no result'} — your reserved credits came back.")
    out = dict(res.get("result") or {})
    return {**out, "task_id": res["task_id"], "credits_charged": res["credits_charged"]}


async def _post_enhance(rows: list[dict[str, Any]], files: list[list[str]]) -> dict[str, Any]:
    """A darabok összefűzése (kép-másolással), az eredeti hang visszakeverése, új változat a forrás mellé."""
    import shutil
    from app.storage import display_label, output_path, public_url, temp_path, write_meta
    from app.storage.files import new_file_id
    from app.tools import crafts
    head = rows[0]
    src, f = head["meta"]["source"], int(head["meta"]["factor"])
    pieces = [output_path(x[0]) for x in files]
    out_id = new_file_id("mp4")
    tmp = temp_path(f"enhc_{uuid.uuid4().hex[:10]}")
    tmp.mkdir(parents=True)
    try:
        video_only = pieces[0]
        if len(pieces) > 1:
            lst = tmp / "list.txt"
            lst.write_text("".join(f"file '{p}'\n" for p in pieces))
            video_only = tmp / "joined.mp4"
            try:
                await _ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(video_only))
            except Exception:                         # eltérő paraméterű darabok: újrakódolva, jó minőségben
                await _ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst), "-c:v", "libx264", "-crf", "16",
                              "-preset", "veryfast", "-pix_fmt", "yuv420p", str(video_only))
        orig = crafts.existing(src)
        k = (await _probe(orig)).get("audio_stream")          # az első dekódolható hangsáv (iPhone: a térhang nem)
        if k is not None:                             # az eredeti hang változatlanul (ha az MP4 befogadja), különben AAC
            from app.tools import encoding
            for audio in (["-c:a", "copy"], encoding.aac()):
                try:
                    await _ffmpeg("-i", str(video_only), "-i", str(orig), "-map", "0:v:0", "-map", f"1:a:{k}", "-c:v", "copy",
                                  *audio, "-shortest", "-movflags", "+faststart", str(output_path(out_id)))
                    break
                except Exception:
                    if audio != ["-c:a", "copy"]:
                        raise
        else:
            await _ffmpeg("-i", str(video_only), "-map", "0:v:0", "-c", "copy", "-movflags", "+faststart",
                          str(output_path(out_id)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)        # a nyers darabokat az elszámolás után a _cleanup törli
    stem = display_label(src).rsplit(".", 1)[0]
    label = f"{stem} (enhanced {f}x).mp4"
    write_meta(out_id, label=label, source_file_id=src, operation="enhance",
               extra={"ai": {"provider": head["provider"], "model": head["model"], "factor": f}})
    info = await _probe(output_path(out_id))
    return {"file_id": out_id, "url": public_url(out_id), "label": label, "width": info["width"], "height": info["height"],
            "duration": info["duration"], "made_from": src,
            "next": "A new version — use its file_id like any video: overlay, merge-files, edit."}


POST["enhance"] = _post_enhance


# ---------- MCP: video(action="enhance" | "ai_clip" | "estimate") ----------

# a video eszköz action="schema" referenciája (a "video" sáv köti be: topic "enhance" / "ai_clip")
AI_VIDEO_REFERENCE: dict[str, str] = {
    "enhance": (
        'video(action="enhance", file_id=<video or project>, factor=1|2|4) — AI upscale on the AI service\'s GPUs (SeedVR2 '
        "or Topaz via fal.ai, or Topaz via kie.ai — the server picks): 1 = clean up at the same size, 2 / 4 = sharper and "
        "bigger; best on old, small or compressed footage. Costs credits by the output's size and length; the estimate is "
        "reserved first, only the real cost is taken, a failed job costs nothing. Long or big videos are cut into parts and "
        "joined back; the original sound is kept as it is. The result is a NEW version of "
        "the file (made_from = the source) — use its file_id like any video: overlay, merge-files, edit. "
        'action="estimate" with the same arguments → {credits, what, balance} without starting anything. Returns a job: '
        'video(action="status", job_id=…). Cancelling (jobs cancel) before the upload reaches the AI service costs nothing; '
        "after that the service can't be stopped: you pay what it really charged and the result is kept in your files."),
    "ai_clip": (
        'video(action="ai_clip", ai={…}, file_id?=<project to put the clip in>) — a generated clip (Seedance 2.5 via '
        "kie.ai). ai: prompt (what happens, camera, style), resolution 480p|720p|1080p (default 720p), duration 4–30 s "
        "(default 5), audio true|false (default false), aspect 16:9|9:16|1:1|4:3|3:4|21:9|adaptive. Picture to start "
        "from (pick ONE mode): image / first_frame = file_id of a picture, or {\"frame\": seconds} = that moment of the "
        "file_id video/project (its last render) — the clip continues the shot; + last_frame = where it should end; OR "
        "reference_images [≤30 file_ids] / reference_videos [≤10, ≤30 s in total] / reference_audios — the look, the "
        "person, the sound to follow (not mixable with first/last frame). draft=true → a cheap 480p preview, then "
        'ai={"finalize": task_id} makes the same clip final at 1080p. Where it goes (with file_id = a video project): '
        "at=seconds → a full-screen cut-in over the timeline at that time (its sound mixed if audio) | after_scene=\"s3\" "
        "(or 0 = first) → a new scene of its own; without them the clip just comes back as a file. Costs credits by "
        "length × resolution (480p/720p/1080p: 28/63/158 kie credits a second × the Studio multiplier); the estimate is "
        'reserved, only the real cost is taken, a failed job costs nothing. action="estimate" → the price only. Cancelling '
        "after the AI service has the job: you pay what it charged, the clip is kept in your files (not on the timeline)."),
}


async def mcp(act: str, *, file_id: Optional[str] = None, factor: Any = None, ai: Optional[dict] = None,
              text: Optional[str] = None, duration: Any = None, draft: bool = False) -> dict[str, Any]:
    """A video eszköz AI-akciói (a horog a video eszközben egy sor: app/mcp_server.py)."""
    try:
        return await _mcp(act, file_id=file_id, factor=factor, ai=dict(ai or {}), text=text, duration=duration, draft=draft)
    except (AIVideoError, PermissionError) as e:
        raise ValueError(str(e)) from None


async def _mcp(act: str, *, file_id, factor, ai: dict[str, Any], text, duration, draft) -> dict[str, Any]:
    if act == "estimate":
        if factor is not None or ai.get("factor") is not None or (ai.get("for") or ai.get("action")) == "enhance":
            require_enabled()
            est = await estimate_enhance(await _source_video(file_id or ""), factor if factor is not None else ai.get("factor"))
        else:
            require_enabled()
            est = await estimate_clip(_clip_args(ai, text, duration, draft))
        return {**credits.public_estimate(est), "balance": await credits.balance(tenancy.current_user_id())}
    if act == "enhance":
        if not file_id:
            raise AIVideoError('enhance needs file_id (a video or a video project) and factor 1|2|4.')
        return await start_enhance(file_id, factor if factor is not None else ai.get("factor") or 2)
    if act == "ai_clip":
        if ai.get("task_id") and not ai.get("finalize"):
            return await get(str(ai["task_id"]))
        return await start_clip(_clip_args(ai, text, duration, draft), project=file_id)
    raise AIVideoError(f"unknown AI action {act}")


def _clip_args(ai: dict[str, Any], text: Optional[str], duration: Any, draft: bool) -> dict[str, Any]:
    out = dict(ai)
    if text and not out.get("prompt"):
        out["prompt"] = text
    if duration is not None and out.get("duration") is None:
        out["duration"] = duration
    if draft and "draft" not in out:
        out["draft"] = True
    return out


# ---------- AI-klip (Seedance 2.5, OMLETT 2.4) ----------

IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "heic", "heif", "gif", "bmp", "tif", "tiff"}
DRAFT_VALID_S = 7 * 86400                         # a kie a draft azonosítóját 7 napig fogadja el


def _frame_ref(v: Any) -> Optional[dict[str, Any]]:
    """{"frame": s} | {"at": s} | {"scene": "s3"} → a timeline egy kockája; különben None."""
    if isinstance(v, dict) and any(k in v for k in ("frame", "at", "scene")) and not v.get("file_id"):
        return v
    return None


async def _image_input(spec: Any, project: Optional[str], work) -> Any:
    """Egy kép-bemenet (első/utolsó kocka, referenciakép) → helyi JPEG a kie követelményei szerint (300–6000 px, 0,4–2,5
    arány), vagy egy nyilvános URL ahogy van. A timeline kockája: a projekt (vagy videó) legutóbbi renderjéből."""
    from app.tools import crafts, video
    fr = _frame_ref(spec)
    dst = work / f"img_{uuid.uuid4().hex[:8]}.jpg"
    if fr is not None:
        if not project:
            raise AIVideoError('A frame of the timeline needs file_id = the video or video project (ai={"image": {"frame": 3.5}}).')
        try:
            vid, t, _ = video.frame_time(project, fr.get("scene"), float(fr.get("frame", fr.get("at"))) if
                                         fr.get("frame", fr.get("at")) is not None else None)
        except ValueError as e:
            raise AIVideoError(str(e)) from None
        src, pre = crafts.existing(vid), ["-ss", f"{t:.3f}"]
    elif isinstance(spec, str) and spec.startswith(("http://", "https://")):
        return spec
    else:
        fid = str(spec.get("file_id") if isinstance(spec, dict) else spec)
        ext = fid.rsplit(".", 1)[-1].lower() if "." in fid else ""
        if ext not in IMAGE_EXTS:
            raise AIVideoError(f"{fid} isn't a picture — give an image file_id, or {{\"frame\": seconds}} of the video.")
        try:
            src = crafts.existing(fid)
        except FileNotFoundError:
            raise AIVideoError(f"No such picture in this workspace: {fid}") from None
        pre = []
    # 300–6000 px, az arány 0,4–2,5 között (kívül: a középre vágva), JPEG
    vf = ("scale='if(lt(min(iw,ih),300),iw*300/min(iw,ih),if(gt(max(iw,ih),4096),iw*4096/max(iw,ih),iw))':-2,"
          "crop='min(iw,ih*2.5)':'min(ih,iw*2.5)'")
    await _ffmpeg(*pre, "-i", str(src), "-frames:v", "1", "-vf", vf, "-q:v", "2", str(dst))
    return dst


async def _video_ref(fid: str, work) -> Any:
    """Referencia-videó a kie követelményei szerint: 480p/720p (409 600–927 408 pixel), 24–60 fps, ≤ 200 MB."""
    from app.tools import crafts
    src = crafts.existing(fid)
    fps = min(60.0, max(24.0, float((await _probe(src))["fps"] or 30)))
    secs = min(30.0, await true_seconds(src))     # a becslés ugyanebből számolt: többet nem küldünk
    dst = work / f"ref_{uuid.uuid4().hex[:8]}.mp4"
    await _ffmpeg("-i", str(src), "-t", f"{secs:.3f}", "-vf", "scale='trunc(sqrt(800000*iw/ih)/2)*2':-2", "-r", f"{fps:g}",
                  "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", str(dst))
    return dst


async def _audio_ref(fid: str, work) -> Any:
    from app.tools import crafts
    dst = work / f"aud_{uuid.uuid4().hex[:8]}.mp3"
    await _ffmpeg("-i", str(crafts.existing(fid)), "-t", "30", "-vn", "-c:a", "libmp3lame", "-b:a", "160k", str(dst))
    return dst


async def _inputs_for(c: dict[str, Any], project: Optional[str]) -> dict[str, Any]:
    """A klip bemenete a kie-nek: a képek / referenciák feltöltve (a kie feltöltő hostja, 24 óráig él)."""
    import shutil
    from app.storage import temp_path
    prov = _provider()
    work = temp_path(f"clip_{uuid.uuid4().hex[:10]}")
    work.mkdir(parents=True)

    async def up(p: Any) -> str:
        return p if isinstance(p, str) else await prov.upload(p)
    try:
        inp: dict[str, Any] = {"prompt": c["prompt"], "resolution": c["resolution"], "duration": c["duration"],
                               "audio": c["audio"], "aspect": c["aspect"], "format": "mp4"}
        if c["draft"]:
            inp["draft"] = True
        if c["first_frame"]:
            inp["first_frame"] = await up(await _image_input(c["first_frame"], project, work))
        if c["last_frame"]:
            inp["last_frame"] = await up(await _image_input(c["last_frame"], project, work))
        if c["reference_images"]:
            inp["reference_images"] = [await up(await _image_input(x, project, work)) for x in c["reference_images"]]
        if c["reference_videos"]:
            inp["reference_videos"] = [await up(await _video_ref(x, work)) for x in c["reference_videos"]]
        if c["reference_audios"]:
            inp["reference_audios"] = [await up(await _audio_ref(x, work)) for x in c["reference_audios"]]
        return inp
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def _draft_row(op: str) -> dict[str, Any]:
    rows = await _rows_of(op)
    if not rows or rows[0]["user_id"] != tenancy.current_user_id():
        raise AIVideoError(f"No such AI clip: {op}")
    r = rows[0]
    if r["model"] != CLIP_MODEL or not (r["input"] or {}).get("draft"):
        raise AIVideoError("Only a draft clip (draft=true) can be finalised.")
    if not r["meta"].get("output"):
        raise AIVideoError("That draft isn't finished (or failed) — make a new draft first.")
    if time.time() - _ts(r["created_at"]) > DRAFT_VALID_S:
        raise AIVideoError("That draft is older than 7 days — the AI service no longer keeps it; make a new draft.")
    return r


def timeline_changes(clip_fid: str, dur: float, *, at: Any = None, after_scene: Any = None, audio: bool = False) -> dict[str, Any]:
    """Az elkészült klip helye a videóprojektben (a meglévő szerkesztő-úton, app/tools/video.py edit): at → teljes képes
    bevágás (fullscreen réteg) azon az időponton; after_scene → egy saját jelenet a klippel (graphic + image elem)."""
    if at is not None:
        return {"add_layers": [{"type": "fullscreen", "at": at, "dur": round(float(dur), 2), "image": clip_fid,
                                "sound": "mix" if audio else "off"}]}
    return {"add": [{"after": after_scene, "headline": "", "caption": "", "duration": round(float(dur), 2),
                     "chart": {"type": "graphic", "backdrop": 0, "elements": [
                         {"shape": "image", "image": clip_fid, "x": 0.5, "y": 0.5, "w": 1, "h": 1, "fit": "cover"}]}}]}


async def start_clip(opts: dict[str, Any], project: Optional[str] = None) -> dict[str, Any]:
    """Seedance 2.5 klip → job. opts: prompt, resolution, duration, audio, aspect, draft, first_frame/image, last_frame,
    reference_*; finalize = egy draft task_id-je (1080p). project + at / after_scene → a klip a timeline-ra kerül."""
    require_enabled()
    at, after = opts.get("at"), opts.get("after_scene", opts.get("scene"))
    if (at is not None or after is not None) and not project:
        raise AIVideoError("To put the clip on a timeline, give file_id = the video project (or its video).")
    if project and (at is not None or after is not None):
        from app.tools import video
        try:
            video.project_of(project)
        except ValueError as e:
            raise AIVideoError(str(e)) from None
    fin = await _draft_row(str(opts["finalize"])) if opts.get("finalize") else None
    c = None if fin else clip_options(opts)
    est = await estimate_clip(opts)
    if fin:
        dur = int(fin["input"].get("duration") or 5)
        op = await reserve("ai_clip", FINAL_MODEL, hold=est["credits"],
                           meta={"units": dur, "tier": "1080p", "prompt": fin["meta"].get("prompt"), "duration": dur,
                                 "audio": bool(fin["input"].get("audio")), "draft_of": fin["id"]})
    else:
        op = await reserve("ai_clip", CLIP_MODEL, hold=est["credits"],
                           meta={"units": est["units"], "tier": est["tier"], "prompt": c["prompt"][:300],
                                 "duration": c["duration"], "audio": c["audio"], "draft": c["draft"], "mode": c["mode"]})

    async def prepare() -> list[dict[str, Any]]:
        if fin:
            return [{"draft_task_id": fin["provider_task_id"], "resolution": "1080p"}]
        return [await _inputs_for(c, project)]

    async def work(progress):
        progress(0, 1, "preparing")
        res = _job_result(await run_reserved(op, prepare, progress))
        if c and c["draft"]:
            res["draft"] = True
            res["next"] = (f'A cheap 480p preview. Happy with it? video(action="ai_clip", ai={{"finalize": "{res["task_id"]}"}}) '
                           "makes it final at 1080p (the same shot). Not happy? Change the prompt and make a new draft.")
        if project and (at is not None or after is not None):
            progress(1, 1, "putting it on the timeline")
            from app.tools import video
            try:
                ed = await video.edit(project, timeline_changes(res["file_id"], res["duration"], at=at, after_scene=after,
                                                                audio=bool(res.get("audio"))), progress=progress)
                res["timeline"] = ed
            except Exception as e:
                log.warning("AI-klip a timeline-ra nem került (%s): %s", project, e)
                res["timeline_error"] = (f"The clip is ready, but it couldn't be put on the timeline ({str(e)[:200]}). "
                                         "Put it in yourself: video(action=\"edit\", file_id=…, changes="
                                         + json.dumps(timeline_changes(res["file_id"], res["duration"], at=at,
                                                                       after_scene=after)) + ")")
        return res
    return await _start_job("ai_video", est, work, op)


async def _post_clip(rows: list[dict[str, Any]], files: list[list[str]]) -> dict[str, Any]:
    from app.storage import output_path, public_url, write_meta
    head = rows[0]
    fid = files[0][0]
    info = await _probe(output_path(fid))
    words = " ".join(str(head["meta"].get("prompt") or "AI clip").split()[:6])
    final = head["model"] == FINAL_MODEL
    label = f"AI clip — {words}{' (draft)' if head['meta'].get('draft') else ' (1080p)' if final else ''}.mp4"
    src = None
    if final:
        prev = await _rows_of(head["meta"].get("draft_of") or "")
        src = ((prev[0]["meta"].get("output") or {}) if prev else {}).get("file_id")
    write_meta(fid, label=label, operation="ai_clip", source_file_id=src,
               extra={"ai": {"provider": head["provider"], "model": head["model"], "prompt": head["meta"].get("prompt"),
                             "mode": head["meta"].get("mode"), "draft": bool(head["meta"].get("draft"))}})
    return {"file_id": fid, "url": public_url(fid), "label": label, "width": info["width"], "height": info["height"],
            "duration": info["duration"], "audio": info["audio_stream"] is not None}


POST["ai_clip"] = _post_clip


async def task_of_file(file_id: str) -> dict[str, Any]:
    """Melyik AI-feladat készítette ezt a fájlt (pl. a draft véglegesítéséhez a felületről)."""
    rows = await db._rows("""SELECT id FROM provider_tasks WHERE user_id = %s AND meta->'output'->>'file_id' = %s
                             ORDER BY created_at DESC LIMIT 1""", (tenancy.current_user_id(), file_id))
    if not rows:
        raise AIVideoError("This file wasn't made by an AI clip job.")
    return await get(rows[0]["id"])
