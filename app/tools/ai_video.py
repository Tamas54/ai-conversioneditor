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
    """A kie callback-címe: csak ha van titok (KIE_WEBHOOK_SECRET) és nyilvános a szerver — különben lekérdezés."""
    from app.config import settings
    secret = os.getenv("KIE_WEBHOOK_SECRET", "")
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    if not secret or "localhost" in base or "127.0.0.1" in base or "testserver" in base:
        return None
    return f"{base}/webhooks/kie?token={secret}"


# ---------- nyilvántartás ----------

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


def public(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Egy op állapota a felhasználónak (a szolgáltatói adat — URL-ek, azonosítók — nélkül)."""
    head = rows[0]
    st = "running"
    if head["meta"].get("output") and all(r["settled"] for r in rows):
        st = "done"
    elif all(r["settled"] for r in rows):
        st = "cancelled" if any(r["status"] == "cancelled" for r in rows) else "failed"
    out = {"task_id": head["id"], "operation": head["operation"], "status": st,
           "parts": len(rows), "parts_done": sum(1 for r in rows if r["status"] in ("success", "fail", "cancelled")),
           "credits_held": head["hold"], "credits_charged": int(head["charged"] or 0) if st == "done" else 0,
           "created_at": head["created_at"]}
    if head["meta"].get("output"):
        out["result"] = head["meta"]["output"]
    err = next((r["error"] for r in rows if r["error"]), None)
    if err:
        out["error"] = err
    return out


async def get(op: str) -> dict[str, Any]:
    rows = await _rows_of(op)
    if not rows or rows[0]["user_id"] != tenancy.current_user_id():
        raise AIVideoError(f"No such AI task: {op}")
    return public(rows)


# ---------- indítás ----------

async def submit(operation: str, model: str, inputs: list[dict[str, Any]], *, hold: int, meta: dict[str, Any],
                 provider: str = "kie") -> str:
    """Hold a becsült maximumra (fedezet nélkül InsufficientCredits — semmi nem indul), a sorok, majd a szolgáltatói
    feladat(ok). Vissza: az op azonosítója."""
    uid = tenancy.current_user_id()
    prov = _provider(provider)
    op = uuid.uuid4().hex[:20]
    held = (await credits.hold(uid, hold, op, operation=operation, provider=provider,
                               meta={"model": model, **{k: meta[k] for k in ("units", "tier") if k in meta}}))["held"]
    from psycopg.types.json import Jsonb
    ids = [op] + [f"{op}-{i}" for i in range(1, len(inputs))]
    for i, (rid, inp) in enumerate(zip(ids, inputs)):
        await db._rows("""INSERT INTO provider_tasks (id, user_id, provider, model, operation, input, hold, meta)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                       (rid, uid, provider, model, operation, Jsonb(inp), held if i == 0 else 0,
                        Jsonb({**meta, "op": op, "i": i, "n": len(inputs)})))
    cb = callback_url()
    for rid, inp in zip(ids, inputs):
        try:
            tid = await prov.create(model, inp, callback_url=cb)
        except Exception as e:
            log.warning("AI-feladat nem indult (%s %s): %s", operation, model, e)
            await _abort(op, _friendly(e))
            if isinstance(e, ProviderError) and e.code == 402:
                await credits.notice(f"{provider}_out_of_credits", f"{provider} refused a job: no credits left on the "
                                                                    "provider account. Top it up.")
            raise AIVideoError(_friendly(e)) from None
        await _update(rid, provider_task_id=tid, status="running")
    log.info("AI-feladat indult: %s %s ×%d (hold %d)", operation, model, len(inputs), held)
    return op


def _friendly(e: Exception) -> str:
    if isinstance(e, ProviderError):
        if e.code == 402:
            return "The AI video service is out of credit on our side — try again later (your credits were not used)."
        if e.code in (422, 400):
            return f"The AI video service refused the request: {str(e).split(':', 1)[-1].strip()[:200]}"
        if e.code == 429:
            return "The AI video service is busy — try again in a minute (your credits were not used)."
    return f"The AI video service didn't take the job ({str(e)[:160]}). Your credits were not used."


async def _abort(op: str, error: str, status: str = "fail") -> None:
    """Az egész op lezárása eredmény nélkül: a hold teljesen visszajár."""
    rows = await _rows_of(op)
    if not rows or all(r["settled"] for r in rows):
        return
    head = rows[0]
    await credits.release(head["user_id"], op, head["hold"], operation=head["operation"], provider=head["provider"],
                          provider_cost=sum(float(r["provider_cost"] or 0) for r in rows),
                          meta={"model": head["model"], "error": error[:200]})
    for r in rows:
        await _update(r["id"], status=r["status"] if r["status"] in ("success", "fail") and status == "fail" else status,
                      settled=True, error=r["error"] or error[:500], finished_at=_now())
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
    tárhelyre; ha az op minden része kész → utófeldolgozás és elszámolás. Idempotens (a zár + a settled jelző + a
    könyv kulcsai)."""
    op = row["meta"].get("op") or row["id"]
    async with _lock(op):
        fresh = (await db._rows("SELECT * FROM provider_tasks WHERE id = %s", (row["id"],)))[0]
        if fresh["settled"] or fresh["status"] in ("success", "fail", "cancelled"):
            pass
        elif st.state == SUCCESS and not st.urls:
            await _update(fresh["id"], status="fail", provider_cost=st.cost, error="the AI service returned no file",
                          finished_at=_now())
        elif st.state == SUCCESS:
            tok = tenancy.set_user(fresh["user_id"])
            try:
                files = await _provider(fresh["provider"]).fetch_result(st)
            except Exception as e:
                log.warning("AI-eredmény letöltése nem sikerült (%s): %s", fresh["id"], e)
                files, st = [], TaskState(task_id=st.task_id, state=FAIL, cost=st.cost,
                                          error=f"the result couldn't be downloaded ({str(e)[:120]})")
            finally:
                tenancy.reset(tok)
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
    rows = await _rows_of(op)
    if not rows or all(r["settled"] for r in rows) or any(r["status"] not in ("success", "fail") for r in rows):
        return
    head = rows[0]
    if any(r["status"] == "fail" for r in rows):
        await _abort(op, next(r["error"] for r in rows if r["status"] == "fail") or "failed")
        return
    cost = sum(float(r["provider_cost"] or 0) for r in rows)
    tok = tenancy.set_user(head["user_id"])
    try:
        post = POST.get(head["operation"])
        output = await post(rows, [list(r["result"] or []) for r in rows]) if post else \
            {"files": [f for r in rows for f in r["result"] or []]}
    except Exception as e:
        log.exception("AI-utófeldolgozás hiba (%s)", op)
        await _abort(op, f"the result couldn't be finished: {str(e)[:200]}")
        return
    finally:
        tenancy.reset(tok)
    rule = await credits.rule_for(head["operation"], head["model"], head["meta"].get("tier") or "")
    est = credits.price(rule, float(head["meta"].get("units") or 1))
    charge = credits.price(rule, provider_units=cost) if any(r["provider_cost"] is not None for r in rows) else est
    res = await credits.settle(head["user_id"], op, head["hold"], charge, operation=head["operation"],
                               provider=head["provider"], provider_task_id=head["provider_task_id"], provider_cost=cost,
                               meta={"model": head["model"], "tier": head["meta"].get("tier"), "parts": len(rows),
                                     "estimate": est})
    await _update(head["id"], charged=res["charged"], meta_merge={"output": output})
    for r in rows:
        await _update(r["id"], settled=True)
    log.info("AI-feladat kész: %s %s — %s kredit (szolgáltató: %s)", head["operation"], op, res["charged"], cost)
    _wake(op)


async def cancel(op: str) -> dict[str, Any]:
    rows = await _rows_of(op)
    if not rows or rows[0]["user_id"] != tenancy.current_user_id():
        raise AIVideoError(f"No such AI task: {op}")
    if not all(r["settled"] for r in rows):
        for r in rows:
            if r["provider_task_id"]:
                await _provider(r["provider"]).cancel(r["provider_task_id"])
        await _abort(op, "cancelled", status="cancelled")
    return public(await _rows_of(op))


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
            if time.monotonic() - t0 > timeout:
                await _abort(op, "the AI service took too long")
                continue
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
        except ProviderError as e:
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


async def resume_loop() -> None:
    """Újraindítás után (és ha senki nem vár rá): a nyitott feladatok lekérdezése; a „created” (feladat nélkül
    maradt) sor 10 perc után visszaadja a holdot."""
    await asyncio.sleep(20)
    while True:
        try:
            rows = await db._rows("SELECT * FROM provider_tasks WHERE NOT settled ORDER BY created_at LIMIT 200")
            ops: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                ops.setdefault(r["meta"].get("op") or r["id"], []).append(r)
            for op, rs in ops.items():
                if op in _watching:
                    continue
                age = time.time() - _ts(rs[0]["created_at"])
                if any(r["status"] == "created" for r in rs) and age > 600:
                    await _abort(op, "the job never reached the AI service")
                elif age > TASK_TIMEOUT_S:
                    await _abort(op, "the AI service took too long")
                else:
                    await poll(await _rows_of(op))
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
        total += float((await video.probe_movie(crafts.existing(fid)))["duration"] or 0)
    if total > 30:
        raise AIVideoError(f"Reference videos may be 30 seconds in total (these are {total:.1f} s).")
    return total


async def estimate_clip(o: dict[str, Any]) -> dict[str, Any]:
    """Az AI-klip ára (Studio-kredit) a becsült maximumra — ezt foglaljuk le; a levonás a tényleges költség."""
    if o.get("finalize"):
        src = await _rows_of(str(o["finalize"]))
        dur = int(((src[0]["input"] if src else {}) or {}).get("duration") or 5)
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
    info = await video.probe_movie(crafts.existing(file_id))
    secs = max(1, math.ceil(float(info["duration"] or 0)))
    est = await credits.estimate("enhance", UPSCALE_MODEL, f"{f}x", secs)
    return {**est, "what": f"{f}x enhance of {secs} s ({info['width']}×{info['height']} → "
                           f"{info['width'] * f}×{info['height'] * f})", "seconds": secs}


def _factor(v: Any) -> int:
    try:
        f = int(float(str(v or 2).lower().rstrip("x")))
    except ValueError:
        f = 0
    if f not in FACTORS:
        raise AIVideoError("factor: 1 (clean up, same size) | 2 | 4")
    return f
