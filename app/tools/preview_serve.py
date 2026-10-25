"""Szakasz-gyorsítótáras HLS-előnézet — a kiszolgálás (10-25, plan20261025_preview_cache.md, P2 sáv).

A szakaszok (app/tools/preview_cache.py: plan → hash-ek, render_run → <hash>.ts, render_audio → a hang-rendition)
itt kapnak lejátszási listát, aláírt URL-t és egy folyamaton belüli elsőbbségi sort:
    0  a lejátszó kérte (a szakasz-végpont — valaki vár rá),
    1  a szerkesztés kérése várja (az első piszkos + a 0. szakasz),
    2  a lejátszófej környéke / a legutóbb kért utáni néhány (előtöltés),
    3  háttér-feltöltés elölről.
Egy kért szakasz a FUTAMÁT rendereli (preview_cache.runs: néhány összefüggő szakasz egy ffmpeg-hívásban, a k-tól
előre); ugyanaz a szakasz kétszer kérve egyszer készül, mindenki ugyanarra a munkára vár (hash → élő munka), egy
már sorban álló munka a magasabb elsőbbségű kéréssel előrébb kerül (új bejegyzés; a régit a dolgozó eldobja).

A dolgozók (VIDEO_PREVIEW_WORKERS, alap 2) a video_preview.gate()-en belül renderelnek — a kaput a preview_cache maga
kéri (render_run, render_audio, a context felépítése), a dolgozó NEM tartja: a kapu nem újrabelépő szemafor (2), két
dolgozó két helye mellett a belső kérés örökre várna. SOSEM a jobs.heavy() kapuján (PR #41: az előnézet nem áll be a
fájleszközök / a végleges render mögé), és ÜRES kontextusban indulnak: se a hívó heavy-helye (_held), se a végleges
render nice 10-e (engines.background) nem öröklődik rájuk — a munka tenancyje (kinek a munkaterületén) a kéréskor
rögzül és munkánként áll be.

A terv (plan_of) a preview_cache.context() terve: a hash-ekben a végig látható rétegek mért köre és (motoros úton /
kézzel szerkesztett projektnél) a projektfájl is benne van — ezt csak a context tudja, a puszta plan(spec) nem.

Python 3.10: az asyncio-sor és a dolgozók eseményhurkonként jönnek létre (az egyik hurokhoz kötött primitív a
másikban elakad); a tesztek _reset()-tel kezdenek.

Aláírás: mint a /files/{id}?t= (storage/files.py): HMAC-SHA256 a FILE_SIGNING_KEY-jel, az üzenet [uid|]pid|hash|exp
(a lejátszási listáké pid||exp — a projektfájl letöltési tokenje így nem nyitja a listát, és fordítva), élettartam
FILE_TTL_HOURS, a munkaterület az u= paraméterben (a tokenbe aláírva). A szakasz-URL a tartalom hash-e: ami egyszer
kész, az örökre ugyanaz (immutable).
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import hmac
import itertools
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from app import tenancy
from app.config import settings

log = logging.getLogger("aice.preview_serve")

P_PLAYER, P_EDIT, P_NEAR, P_FILL = 0, 1, 2, 3
MPEGURL, MP2T = "application/vnd.apple.mpegurl", "video/mp2t"
IMMUTABLE = "private, max-age=31536000, immutable"
AUDIO_INDEX = "index.m3u8"                    # a hang-rendition listája az audio_dir-ben (P1 írja UTOLJÁRA)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
FAIL_HOLD_S = 15.0                            # elakadt szakasz: ennyi ideig nem renderel újra (a lejátszó újrapróbál)
TOUCH_EVERY_S = 2.0                           # a kiszolgált szakaszok last_used_at-je kötegben
SWEEP_EVERY_S = 300.0                         # LRU (preview_cache.sweep) legfeljebb ennyi időnként, render után


def _env_num(name: str, default: float, lo: float) -> float:
    try:
        return max(lo, float(os.getenv(name, "") or default))
    except ValueError:
        return default


def wait_s() -> float:
    """Ennyit vár a szakasz-végpont (és a request()) egy hiányzó szakaszra, utána 503 + Retry-After."""
    return _env_num("VIDEO_PREVIEW_SEG_WAIT_S", 20.0, 0.0)


def n_workers() -> int:
    return int(_env_num("VIDEO_PREVIEW_WORKERS", 2, 1))


def prefetch_n() -> int:
    """A lejátszó által kért szakasz után ennyit tölt elő (elsőbbség 2)."""
    return int(_env_num("VIDEO_PREVIEW_PREFETCH", 5, 0))


def _pc():
    from app.tools import preview_cache
    return preview_cache


def _run_max() -> int:
    return max(1, int(getattr(_pc(), "RUN_MAX", 5)))


# ---------- aláírt URL-ek ----------

def _msg(pid: str, h: Optional[str], exp: int, uid: str) -> bytes:
    body = f"{pid}|{h or ''}|{exp}"
    return (body if uid == tenancy.ADMIN_ID else f"{uid}|{body}").encode()


def _sig(pid: str, h: Optional[str], exp: int, uid: str) -> str:
    return hmac.new(settings.FILE_SIGNING_KEY.encode(), _msg(pid, h, exp, uid), hashlib.sha256).hexdigest()[:32]


def _workspace(pid: str) -> str:
    from app.storage.files import file_workspace
    return file_workspace(pid)


def sign(pid: str, h: Optional[str] = None, *, uid: Optional[str] = None, ttl: Optional[int] = None) -> tuple[str, int, str]:
    """→ (token, exp, uid). h=None: a projekt lejátszási listái (és a preview.json); h: egy szakasz / a hang-rendition."""
    uid = uid or _workspace(pid)
    exp = int(time.time()) + (settings.FILE_TTL_HOURS * 3600 if ttl is None else int(ttl))
    return f"{exp}.{_sig(pid, h, exp, uid)}", exp, uid


def verify(pid: str, h: Optional[str], token: str, uid: str = tenancy.ADMIN_ID) -> bool:
    try:
        exp_s, sig = (token or "").split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(_sig(pid, h, exp, uid), sig)


def _query(pid: str, h: Optional[str], exp: int, uid: str) -> str:
    q = f"?p={pid}&t={exp}.{_sig(pid, h, exp, uid)}" if h else f"?t={exp}.{_sig(pid, None, exp, uid)}"
    return q + ("" if uid == tenancy.ADMIN_ID else f"&u={uid}")


def signed(pid: str, h: Optional[str] = None) -> str:
    """A kész query: h nélkül „?t=<exp>.<sig>[&u=…]” (lista, preview.json), h-val „?p=<pid>&t=…[&u=…]” (szakasz, hang)."""
    _, exp, uid = sign(pid, h)
    return _query(pid, h, exp, uid)


def _base() -> str:
    return settings.PUBLIC_BASE_URL.rstrip("/")


def playlist_url(pid: str) -> str:
    """A master lista teljes, aláírt címe (a válasz `preview.playlist_url`-je, a preview.json)."""
    return f"{_base()}/api/video/{pid}/preview.m3u8{signed(pid)}"


def json_url(pid: str) -> str:
    """A lejátszó (P4) állapot-címe: GET → status()."""
    return f"{_base()}/api/video/{pid}/preview.json{signed(pid)}"


# ---------- a terv (a verzió preview_cache.context()-jéé → gyorsítótárban) ----------

_PLANS: dict[tuple[str, str], tuple[tuple[int, int], dict[str, Any]]] = {}
_PLANS_MAX = 64


def _stamp(pid: str) -> tuple[int, int]:
    """A projektfájl és a metája ideje: a böngészős mentés / a meta (spec) cseréje új tervet ad."""
    from app.storage import output_path
    from app.storage.metadata import _meta_path_for
    m = _meta_path_for(pid)
    return output_path(pid).stat().st_mtime_ns, (m.stat().st_mtime_ns if m.exists() else 0)


def _remember(pid: str, stamp: tuple[int, int], plan: dict[str, Any]) -> None:
    _PLANS[(_workspace(pid), pid)] = (stamp, plan)
    while len(_PLANS) > _PLANS_MAX:
        _PLANS.pop(next(iter(_PLANS)))


def cached_plan(pid: str) -> Optional[dict[str, Any]]:
    """A már ismert terv (plan_of / schedule után), ha a projekt és a metája azóta nem változott; különben None."""
    try:
        st = _stamp(pid)
    except OSError:
        return None
    hit = _PLANS.get((_workspace(pid), pid))
    return hit[1] if hit and hit[0] == st else None


async def plan_of(pid: str) -> dict[str, Any]:
    """A verzió szakasz-terve: a preview_cache.context(pid) terve (a context verziónként egyszer épül, a P1 tartja
    gyorsítótárban; itt a projekt és a meta idejéhez kötve). FileNotFoundError: nincs ilyen projekt; LookupError: nincs
    szakasz-előnézete (nem a saját felvételre épülő projekt); RuntimeError: a környezet most nem épült fel (átmeneti)."""
    from app.storage import output_path, read_meta
    if not pid.lower().endswith(".ecproj") or not output_path(pid).exists():
        raise FileNotFoundError(pid)
    hit = cached_plan(pid)
    if hit is not None:
        return hit
    st = _stamp(pid)
    spec = (read_meta(pid) or {}).get("spec")
    if not isinstance(spec, dict) or spec.get("kind") != "overlay" or not spec.get("base"):
        raise LookupError("This project has no segment preview (it isn't built on your own footage).")
    try:
        plan = (await _pc().context(pid))["plan"]
    except ValueError as e:                    # a projekt nem ilyen (P1: nem overlay) — végleges
        raise LookupError(f"This project has no segment preview: {str(e)[:160]}") from None
    except Exception as e:                     # a környezet most nem épült fel (motor, lemez) — a lejátszó újrapróbálja
        log.warning("előnézet-terv %s: %s", pid, e)
        raise RuntimeError(f"the preview isn't ready to play yet: {str(e)[:160]}") from None
    _remember(pid, st, plan)
    return plan


def k_of(plan: dict[str, Any], h: str) -> Optional[int]:
    """A hash első előfordulása a tervben (ugyanaz a tartalom többször is szerepelhet — ugyanaz a fájl)."""
    for i, s in enumerate(plan.get("segments") or []):
        if s["hash"] == h:
            return i
    return None


def _seg_file(plan: dict[str, Any], h: str) -> Path:
    return Path(_pc().seg_path(plan["source_id"], h))


def _have(plan: dict[str, Any], h: str) -> bool:
    """A kiszolgálás igazsága a fájl (P1 tmp + átnevezéssel írja: ami létezik, az teljes)."""
    return _seg_file(plan, h).exists()


def _audio_index(plan: dict[str, Any]) -> Optional[Path]:
    ah = plan.get("audio_hash")
    return Path(_pc().audio_dir(plan["source_id"], ah)) / AUDIO_INDEX if ah else None


def ready_hashes(plan: dict[str, Any]) -> set[str]:
    return {s["hash"] for s in plan.get("segments") or [] if _have(plan, s["hash"])}


def _seg_at(plan: dict[str, Any], t: float) -> int:
    segs = plan.get("segments") or []
    for s in segs:
        if float(s["out_start"]) + float(s["dur"]) > t:
            return int(s["k"])
    return max(0, len(segs) - 1)


# ---------- az elsőbbségi sor (eseményhurkonként) ----------

class _Superseded(RuntimeError):
    """A háttér-feltöltés munkája egy újabb verzió tervére cserélődött (ugyanaz a felvétel)."""


class _Job:
    __slots__ = ("kind", "pid", "plan", "ks", "keys", "prio", "uid", "shared", "fut", "started")

    def __init__(self, st: "_State", kind: str, pid: str, plan: dict[str, Any], ks: list[int], keys: list[str], prio: int):
        self.kind, self.pid, self.plan, self.ks, self.keys, self.prio = kind, pid, plan, ks, keys, prio
        self.uid, self.shared = tenancy.current_user_id(), tenancy.shared()   # a hívó munkaterülete (megosztottan is)
        self.fut: asyncio.Future = st.loop.create_future()
        self.started = False


class _State:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.seq = itertools.count()
        self.live: dict[str, _Job] = {}            # hash | "a:<audio_hash>" → a munka, ami előállítja
        self.failed: dict[str, tuple[float, str]] = {}
        self.workers: list[asyncio.Task] = []
        self.tasks: set[asyncio.Task] = set()
        self.touched: dict[str, set[str]] = {}       # source_id → a kiszolgált hash-ek (LRU, kötegben)
        self.touch_task: Optional[asyncio.Task] = None
        self.swept = 0.0
        self.stats = {"hits": 0, "misses": 0, "runs": 0, "segments": 0, "failed": 0}


_S: Optional[_State] = None


def _state() -> _State:
    global _S
    loop = asyncio.get_running_loop()
    if _S is None or _S.loop is not loop:
        _S = _State(loop)
    return _S


def _reset() -> None:
    """Tesztekhez: a sor, a dolgozók és a terv-gyorsítótár eldobása (a dolgozók a saját hurkukban állnak le)."""
    global _S
    st, _S = _S, None
    _PLANS.clear()
    if st is not None and not st.loop.is_closed():
        for t in st.workers + list(st.tasks) + ([st.touch_task] if st.touch_task else []):
            try:
                st.loop.call_soon_threadsafe(t.cancel)
            except RuntimeError:
                pass


def stats() -> dict[str, Any]:
    st = _S
    return {**(st.stats if st else {}), "queued": st.queue.qsize() if st else 0, "live": len(set(st.live.values())) if st else 0}


def _ensure_workers(st: _State) -> None:
    st.workers = [w for w in st.workers if not w.done()]
    while len(st.workers) < n_workers():
        # üres kontextus: a hívó jobs._held-je / engines._BACKGROUND-ja / tenancyje NEM megy át (lásd a modul elejét)
        st.workers.append(contextvars.Context().run(st.loop.create_task, _worker(st)))


def _push(st: _State, job: _Job, prio: int) -> None:
    st.queue.put_nowait((prio, next(st.seq), job))


def _new_job(st: _State, kind: str, pid: str, plan: dict[str, Any], ks: list[int], keys: list[str], prio: int) -> _Job:
    job = _Job(st, kind, pid, plan, ks, keys, prio)
    for key in keys:
        st.live[key] = job
    _push(st, job, prio)
    _ensure_workers(st)
    return job


def _bump(st: _State, job: _Job, prio: int) -> None:
    if prio < job.prio and not job.started:
        job.prio = prio
        _push(st, job, prio)                       # a régi bejegyzést a dolgozó eldobja (started / más elsőbbség)


def _enqueue_run(st: _State, pid: str, plan: dict[str, Any], k: int, prio: int) -> _Job:
    """A k. szakasz futama (k-tól előre, ami még nincs kész és nem készül, legfeljebb RUN_MAX, egy részen belül —
    preview_cache.runs dönt). Ha k már készül: arra a munkára vár (és előrébb kerül)."""
    segs = plan["segments"]
    h = segs[k]["hash"]
    job = st.live.get(h)
    if job is not None:
        _bump(st, job, prio)
        return job
    ks, seen = [k], {h}
    for j in range(k + 1, min(len(segs), k + _run_max())):
        hj = segs[j]["hash"]
        if hj in seen or hj in st.live or _have(plan, hj):
            break
        ks.append(j)
        seen.add(hj)
    run = next((list(r) for r in _pc().runs(plan, ks) if k in r), [k])
    return _new_job(st, "run", pid, plan, run, [segs[i]["hash"] for i in run], prio)


def _enqueue_audio(st: _State, pid: str, plan: dict[str, Any], prio: int) -> Optional[_Job]:
    ah = plan.get("audio_hash")
    if not ah:
        return None
    job = st.live.get(f"a:{ah}")
    if job is not None:
        _bump(st, job, prio)
        return job
    return _new_job(st, "audio", pid, plan, [], [f"a:{ah}"], prio)


def _prefetch(st: _State, pid: str, plan: dict[str, Any], k: int) -> None:
    """A lejátszó k-t kérte: a következő néhány (elsőbbség 2), ami még nincs kész és nem készül."""
    segs = plan["segments"]
    j, end = k + 1, min(len(segs), k + 1 + prefetch_n())
    while j < end:
        h = segs[j]["hash"]
        if h in st.live or _have(plan, h):
            j += 1
            continue
        j = max(_enqueue_run(st, pid, plan, j, P_NEAR).ks + [j]) + 1


@contextlib.contextmanager
def _as(job: _Job):
    t_user = tenancy.set_user(job.uid)
    t_sh = tenancy.enter_shared(job.shared)
    try:
        yield
    finally:
        tenancy.leave_shared(t_sh)
        tenancy.reset(t_user)


def _done_ok(job: _Job) -> bool:
    if job.kind == "audio":
        idx = _audio_index(job.plan)
        return bool(idx and idx.exists())
    return all(_have(job.plan, h) for h in job.keys)


async def _worker(st: _State) -> None:
    while True:
        prio, _, job = await st.queue.get()
        if job.started or job.fut.done() or prio != job.prio:
            continue                               # felülírt elsőbbségű / már futó / elvetett bejegyzés
        job.started = True
        t0 = time.monotonic()
        try:
            with _as(job):
                # a context verziónként egyszer épül (P1: gyorsítótár + zár); a render a video_preview.gate()-et maga
                # kéri — itt NEM tartjuk (nem újrabelépő: két dolgozó mellett a belső kérés örökre várna), és nem heavy()
                ctx = await _pc().context(job.pid)
                if job.kind == "audio":
                    await _pc().render_audio(ctx)
                else:
                    await _pc().render_run(ctx, list(job.ks))
            if not _done_ok(job):
                raise RuntimeError("the preview renderer finished without writing "
                                   + ("the audio track" if job.kind == "audio" else f"segment(s) {job.ks}"))
            st.stats["runs"] += 1
            st.stats["segments"] += len(job.ks)
            log.info("előnézet %s %s %s: %.2f s (elsőbbség %d)", job.pid, job.kind, job.ks or "", time.monotonic() - t0, job.prio)
            if not job.fut.done():
                job.fut.set_result(None)
        except asyncio.CancelledError:
            if not job.fut.done():
                job.fut.cancel()
            raise
        except Exception as e:
            st.stats["failed"] += 1
            log.warning("előnézet %s %s %s elakadt: %s", job.pid, job.kind, job.ks or "", e)
            for key in job.keys:
                st.failed[key] = (time.monotonic(), str(e)[:300])
            if not job.fut.done():
                job.fut.set_exception(e)
                job.fut.exception()                # „never retrieved” ne szóljon, ha a háttérmunkára senki sem várt
        finally:
            for key in job.keys:
                if st.live.get(key) is job:
                    del st.live[key]
        _maybe_sweep(st)


def _maybe_sweep(st: _State) -> None:
    sweep = getattr(_pc(), "sweep", None)
    now = time.monotonic()
    if sweep is None or now - st.swept < SWEEP_EVERY_S:
        return
    st.swept = now

    async def go():
        try:
            await sweep()
        except Exception as e:
            log.warning("előnézet-gyorsítótár takarítása: %s", e)
    _spawn(st, go())


def _spawn(st: _State, coro) -> asyncio.Task:
    t = st.loop.create_task(coro)
    st.tasks.add(t)
    t.add_done_callback(st.tasks.discard)
    return t


def _touch(st: _State, source_id: str, hashes: list[str]) -> None:
    st.touched.setdefault(source_id, set()).update(hashes)
    if st.touch_task is None or st.touch_task.done():
        async def flush():
            await asyncio.sleep(TOUCH_EVERY_S)
            todo, st.touched = st.touched, {}
            for sid, hs in todo.items():
                try:
                    await _pc().touch(sorted(hs), sid)
                except Exception as e:
                    log.debug("előnézet touch: %s", e)
        st.touch_task = st.loop.create_task(flush())


async def _wait(job: _Job, timeout: Optional[float]) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(job.fut), wait_s() if timeout is None else timeout)
    except asyncio.TimeoutError:
        raise TimeoutError("the preview segment is still rendering") from None


def _held_failure(st: _State, key: str) -> Optional[str]:
    bad = st.failed.get(key)
    if bad and time.monotonic() - bad[0] < FAIL_HOLD_S:
        return bad[1]
    return None


# ---------- a sávok API-ja (P3: _preview_and_final; a végpontok) ----------

async def request(pid: str, k: int, *, priority: int = P_PLAYER, plan: Optional[dict[str, Any]] = None,
                  timeout: Optional[float] = None) -> Path:
    """A k. szakasz fájlja: kész → azonnal; különben a futama sorba (elsőbbséggel), és legfeljebb wait_s()-ig
    (VIDEO_PREVIEW_SEG_WAIT_S) vár rá — utána TimeoutError (a munka megy tovább, a következő kérés ugyanarra vár).
    A lejátszó kérése (0) a következő néhányat is előtölti. RuntimeError: a render elakadt."""
    plan = plan or await plan_of(pid)
    segs = plan["segments"]
    if not 0 <= k < len(segs):
        raise IndexError(f"segment {k} is not in this preview (0…{len(segs) - 1})")
    h = segs[k]["hash"]
    path = _seg_file(plan, h)
    st = _state()
    if path.exists():
        st.stats["hits"] += 1
        _touch(st, plan["source_id"], [h])
        if priority == P_PLAYER:
            _prefetch(st, pid, plan, k)
        return path
    st.stats["misses"] += 1
    if h not in st.live and (why := _held_failure(st, h)):
        raise RuntimeError(why)
    job = _enqueue_run(st, pid, plan, k, priority)
    if priority == P_PLAYER:
        _prefetch(st, pid, plan, max(job.ks + [k]))
    await _wait(job, timeout)
    if not path.exists():
        raise RuntimeError("the preview segment was not written")
    return path


async def request_audio(pid: str, *, priority: int = P_PLAYER, plan: Optional[dict[str, Any]] = None,
                        timeout: Optional[float] = None) -> Path:
    """A hang-rendition könyvtára (benne index.m3u8): kész → azonnal; különben sorba, és vár (mint a request())."""
    plan = plan or await plan_of(pid)
    idx = _audio_index(plan)
    if idx is None:
        raise LookupError("This preview has no sound track.")
    if idx.exists():
        return idx.parent
    st = _state()
    key = f"a:{plan['audio_hash']}"
    if key not in st.live and (why := _held_failure(st, key)):
        raise RuntimeError(why)
    job = _enqueue_audio(st, pid, plan, priority)
    await _wait(job, timeout)
    if not idx.exists():
        raise RuntimeError("the preview sound track was not written")
    return idx.parent


def warm(pid: str, plan: dict[str, Any]) -> None:
    """A lista kérésekor (a végpontok): a 0. szakasz futama és a hang elsőbbséggel (0) sorba, ha még nincs kész — a
    lejátszó mindig a 0.-kal indul, és ez gyorsan kell (P4: a hls.js később indulva nem hozza létre a puffereit). Nem vár."""
    segs = plan.get("segments") or []
    st = _state()
    if segs and not _have(plan, segs[0]["hash"]) and not _held_failure(st, segs[0]["hash"]):
        _enqueue_run(st, pid, plan, 0, P_PLAYER)
    idx = _audio_index(plan)
    if idx is not None and not idx.exists() and not _held_failure(st, f"a:{plan['audio_hash']}"):
        _enqueue_audio(st, pid, plan, P_PLAYER)


def schedule(pid: str, plan: dict[str, Any], dirty: list[int], *, playhead: float = 0.0) -> None:
    """Háttér-előtöltés az új verzióra (futó eseményhurokból hívandó): a 0. és az első piszkos szakasz (1), a lejátszófej
    körüli néhány (2), aztán minden hiányzó elölről (3), a hang (1). Ugyanannak a felvételnek egy korábbi verzióra
    sorba állított, még el nem kezdett előtöltése (2, 3) elmarad — amit az új is használ, azt az új állítja sorba."""
    try:
        _remember(pid, _stamp(pid), plan)
    except OSError:
        pass
    st = _state()
    _spawn(st, _fill(st, pid, plan, sorted(int(k) for k in dirty or []), float(playhead or 0.0)))


async def _fill(st: _State, pid: str, plan: dict[str, Any], dirty: list[int], playhead: float) -> None:
    segs = plan.get("segments") or []
    n = len(segs)
    for job in set(st.live.values()):        # a régi verzió előtöltése (2, 3): senki sem vár rá (a várók 0–1-re emelik)
        if job.kind == "run" and not job.started and job.prio >= P_NEAR and job.pid != pid \
                and job.plan.get("source_id") == plan.get("source_id"):
            for key in job.keys:
                if st.live.get(key) is job:
                    del st.live[key]
            job.fut.set_exception(_Superseded("superseded by a newer version"))
            job.fut.exception()
    idx = _audio_index(plan)
    if idx is not None and not idx.exists():
        _enqueue_audio(st, pid, plan, P_EDIT)
    if not n:
        return

    def missing(k: int) -> bool:
        h = segs[k]["hash"]
        return h not in st.live and not _have(plan, h)

    for k in dict.fromkeys([0] + dirty[:1]):
        if 0 <= k < n and missing(k):
            _enqueue_run(st, pid, plan, k, P_EDIT)
        elif 0 <= k < n and segs[k]["hash"] in st.live:
            _bump(st, st.live[segs[k]["hash"]], P_EDIT)
    j = kp = _seg_at(plan, playhead)
    while j < min(n, kp + 1 + prefetch_n()):
        if missing(j):
            j = max(_enqueue_run(st, pid, plan, j, P_NEAR).ks + [j]) + 1
        else:
            j += 1
    seen: set[str] = set()
    rest = []
    for k in range(n):
        h = segs[k]["hash"]
        if h not in seen and missing(k):
            rest.append(k)
        seen.add(h)
    for run in _pc().runs(plan, rest) if rest else []:
        run = [k for k in run if missing(k)]
        if run:
            _new_job(st, "run", pid, plan, run, [segs[k]["hash"] for k in run], P_FILL)


# ---------- lejátszási listák ----------

_LEVELS = ((10, 1485, 99), (11, 3000, 396), (12, 6000, 396), (13, 11880, 396), (20, 11880, 396), (21, 19800, 792),
           (22, 20250, 1620), (30, 40500, 1620), (31, 108000, 3600), (32, 216000, 5120), (40, 245760, 8192),
           (42, 522240, 8704), (50, 589824, 22080), (51, 983040, 36864), (52, 2073600, 36864))   # H.264 A-1: (szint, MB/s, MB/kocka)


def codecs(plan: dict[str, Any]) -> str:
    """A CODECS: libx264 ultrafast, yuv420p → Constrained Baseline (42c0), a szint a kockaméretből és -rátából (ahogy az
    x264 maga választja); a prototípusban 960×540@30 → avc1.42c01f. A hang AAC-LC."""
    w, h, fps = int(plan.get("pw") or 960), int(plan.get("ph") or 540), float(plan.get("fps") or 30)
    mbs = math.ceil(w / 16) * math.ceil(h / 16)
    level = next((lv for lv, mbps, fs in _LEVELS if mbs <= fs and mbs * fps <= mbps), 52)
    v = f"avc1.42c0{level:02x}"
    return f"{v},mp4a.40.2" if plan.get("audio_hash") else v


def durations(plan: dict[str, Any]) -> list[float]:
    """Szakaszonként frames/fps, a kumulált kockaszámból kerekítve: Σ = az összes kocka / fps (900 szakaszon sem csúszik)."""
    fps = float(plan["fps"])
    out, f = [], 0
    for s in plan.get("segments") or []:
        a = round(f / fps, 6)
        f += int(s["frames"])
        out.append(round(round(f / fps, 6) - a, 6))
    return out


def _known(pid: str, plan: Optional[dict[str, Any]]) -> dict[str, Any]:
    plan = plan or cached_plan(pid)
    if plan is None:
        raise LookupError("no plan for this preview yet — await plan_of(pid) first")
    return plan


def master_m3u8(pid: str, plan: Optional[dict[str, Any]] = None) -> str:
    """A master: a kép (video.m3u8) + a hang külön, folytonos renditionként (#EXT-X-MEDIA TYPE=AUDIO) — a 10-25-i
    A1 prototípus alakja (hls.js 1.5.13 + Chrome: hézag nélkül, tekeréssel is). plan nélkül: a már ismert terv."""
    plan = _known(pid, plan)
    q = signed(pid)
    audio = bool(plan.get("audio_hash"))
    w, h, fps = int(plan.get("pw") or 960), int(plan.get("ph") or 540), float(plan.get("fps") or 30)
    bw = max(300_000, int(w * h * fps * 0.1)) + (128_000 if audio else 0)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    if audio:
        lines.append(f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="main",DEFAULT=YES,AUTOSELECT=YES,'
                     f'URI="/api/video/{pid}/audio.m3u8{q}"')
    lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={w}x{h},CODECS="{codecs(plan)}"'
                 + (',AUDIO="aud"' if audio else ""))
    lines.append(f"/api/video/{pid}/video.m3u8{q}")
    return "\n".join(lines) + "\n"


def video_m3u8(pid: str, plan: dict[str, Any]) -> str:
    """VOD-lista, benne MINDEN szakasz (kész vagy sem — a hiányzót a szakasz-végpont kérésre rendereli), mindegyik
    a saját EXTINF-jével, és mivel minden fájl 0-tól indul: szakaszonként #EXT-X-DISCONTINUITY."""
    segs = plan.get("segments") or []
    durs = durations(plan)
    _, exp, uid = sign(pid)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{max([1] + [math.ceil(d) for d in durs])}",
             "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-INDEPENDENT-SEGMENTS"]
    for i, (s, d) in enumerate(zip(segs, durs)):
        if i:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f"#EXTINF:{d:.6f},")
        lines.append(f"/api/video/preview/seg/{s['hash']}.ts{_query(pid, s['hash'], exp, uid)}")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def audio_m3u8(pid: str, plan: dict[str, Any]) -> str:
    """A hang-rendition (preview_cache.render_audio: audio_dir/index.m3u8, ffmpeg -f hls) — a darabok neve aláírt
    végpont-címre cserélve. FileNotFoundError: még nem készült el (a végpont előbb request_audio()-t hív)."""
    ah = plan.get("audio_hash")
    idx = _audio_index(plan)
    if idx is None:
        raise LookupError("This preview has no sound track.")
    text = idx.read_text("utf-8")
    q = signed(pid, ah)

    def url(name: str) -> str:
        base = name.split("?", 1)[0].rsplit("/", 1)[-1]
        if not NAME_RE.match(base):
            raise ValueError(f"unexpected name in the audio playlist: {name!r}")
        return f"/api/video/preview/aud/{ah}/{base}{q}"

    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        out.append(re.sub(r'URI="([^"]+)"', lambda m: f'URI="{url(m.group(1))}"', s) if s.startswith("#") else url(s))
    return "\n".join(out) + "\n"


def audio_file(plan: dict[str, Any], name: str) -> Path:
    """A hang-rendition egy darabja (a név a listából; csak egyszerű fájlnév)."""
    if not NAME_RE.match(name) or name == AUDIO_INDEX:
        raise FileNotFoundError(name)
    idx = _audio_index(plan)
    if idx is None:
        raise FileNotFoundError(name)
    return idx.parent / name


AUDIO_TYPES = {".ts": MP2T, ".aac": "audio/aac", ".m4s": "audio/mp4", ".mp4": "audio/mp4", ".m4a": "audio/mp4"}


# ---------- preview.json (a P4 lejátszója) ----------

_NO_ETA = {"eta": None, "eta_s": None, "ready_at": None}


def final_status(pid: str, meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """{status: none|queued|running|done|failed, eta (szöveg), eta_s, ready_at (ISO, render_eta.ready_at), video_url}.
    A végleges render állapota: a jobs-nyilvántartás (video._start_final: kind=render, final=True, project=pid —
    ugyanaz, amit a video(action="status") olvas), újraindítás után a projekt metája (render_file_id, nem vázlat)."""
    from app import jobs
    from app.storage import output_path, public_url, read_meta
    from app.tools import render_eta
    meta = meta if meta is not None else (read_meta(pid) or {})
    mine = [j for j in list(jobs._JOBS.values())
            if j.get("kind") == "render" and j.get("final") and j.get("project") == pid]
    if mine:
        j = max(mine, key=lambda x: x.get("started") or 0)
        if j["status"] in ("running", "queued"):
            left = jobs.remaining_s(j)
            if left > 0:
                return {"status": j["status"], "eta": render_eta.text(left), "eta_s": round(left),
                        "ready_at": render_eta.ready_at(left), "video_url": None}
            eta = "taking longer than estimated — still working" if j.get("eta_s") is not None else None
            return {"status": j["status"], "eta": eta, "eta_s": None, "ready_at": None, "video_url": None}
        if j["status"] == "done":
            res = j.get("result") or {}
            fid = res.get("file_id")
            url = res.get("url") or (public_url(fid) if fid and output_path(fid).exists() else None)
            return {"status": "done", **_NO_ETA, "video_url": url}
        return {"status": "failed", **_NO_ETA, "video_url": None}
    rid = meta.get("render_file_id")
    if rid and not meta.get("render_draft") and output_path(rid).exists():
        return {"status": "done", **_NO_ETA, "video_url": public_url(rid)}
    return {"status": "none", **_NO_ETA, "video_url": None}


def status(pid: str, plan: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """GET /api/video/{pid}/preview.json → {playlist_url, duration, segments_total, segments_ready, markers, final}."""
    from app.storage import read_meta
    plan = _known(pid, plan)
    segs = plan.get("segments") or []
    have = ready_hashes(plan)
    meta = read_meta(pid) or {}
    return {"playlist_url": playlist_url(pid),
            "duration": round(sum(durations(plan)), 3),         # a lista hossza (Σ kocka / fps), nem a kockára nem igazított total
            "segments_total": len(segs),
            "segments_ready": sum(1 for s in segs if s["hash"] in have),
            "markers": list((meta.get("preview") or {}).get("markers") or []),
            "final": final_status(pid, meta)}
