"""Szakasz-gyorsítótáras HLS-előnézet — a kiszolgálás (app/tools/preview_serve.py, a végpontok: app/routes/video.py).

A preview_cache (P1 sáv) itt a szerződés (plan20261025_preview_cache.md, „API v1”) szerinti álmodul: ugyanazok a
nevek és alakok (plan → {"source_id", "fps", "pw", "ph", "total", "audio_hash", "segments": [{k, out_start, dur, frames,
src, src_a, part, hold, hash}]}), a render csak fájlt ír. A lista alakja (szakaszonként DISCONTINUITY, a hang külön
rendition) a 10-25-i A1 prototípusé (.claude/qa/hlsproto/: Chrome + hls.js 1.5.13, hézag nélkül).
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
import sys
import time
from pathlib import Path

import pytest

from app import tenancy

PID = "0123456789abcdef0123456789abcdef.ecproj"
SRC = "fedcba9876543210fedcba9876543210.mov"
CLIP = "aaaaaaaaaaaaaaaabbbbbbbbbbbbbbbb.mp4"


def _h(*parts) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def make_plan(parts=((None, 0.0, 6.0), (CLIP, 10.0, 14.4), (None, 20.0, 23.0)), hold_s=1.5, fps=30.0, seg_s=2.0, salt=""):
    """A szerződés alakja: részenként a SAJÁT forrásidő n·SEG_S rácsán, a < 0,5 s-os maradék az előzőbe olvad (itt a klip
    utolsó szakasza 2,4 s), a zárókártya (hold) a kimeneti időben, src_a = None."""
    segs, out = [], 0.0
    for part, (src, a, b) in enumerate(parts):
        edges = [a]
        while True:
            nxt = math.floor(edges[-1] / seg_s + 1e-9) * seg_s + seg_s
            if nxt >= b - 1e-9 or b - nxt < 0.5 - 1e-9:
                break
            edges.append(nxt)
        edges.append(b)
        for x, y in zip(edges, edges[1:]):
            fr = round((y - x) * fps)
            segs.append({"k": len(segs), "out_start": round(out, 6), "dur": fr / fps, "frames": fr, "src": src,
                         "src_a": x, "part": part, "hold": False, "hash": _h(salt, src or SRC, x, y, fps)})
            out += fr / fps
    t = 0.0
    while hold_s - t > 1e-9:
        d = min(seg_s, hold_s - t)
        fr = round(d * fps)
        segs.append({"k": len(segs), "out_start": round(out, 6), "dur": fr / fps, "frames": fr, "src": None,
                     "src_a": None, "part": len(parts), "hold": True, "hash": _h(salt, "hold", t, fps)})
        out += fr / fps
        t += d
    return {"version": 1, "source_id": SRC, "recipe": {"adjust": None, "auto": None, "proxy": [960, 540]},
            "fps": fps, "W": 1920, "H": 1080, "pw": 960, "ph": 540, "total": round(out, 6),
            "audio_hash": _h(salt, "audio"), "segments": segs}


class FakePC:
    """A preview_cache szerződése (API v1) — a render csak fájlt ír (tmp + átnevezés, mint P1-é). Mint a P1-é: a context
    verziónként egyszer épül (és a terv onnan jön), a render a video_preview.gate()-et MAGA kéri."""
    SEG_S, PREVIEW_CACHE_VERSION, RUN_MAX = 2.0, 1, 4

    def __init__(self, root: Path, plan: dict):
        self.root, self._plan = root, plan
        self.calls: list[list[int]] = []
        self.audio_calls = 0
        self.contexts = 0
        self.touched: list[str] = []
        self.delay = 0.0
        self.gate: asyncio.Event | None = None          # ha van: a render erre vár (a sor sorrendjének teszteléséhez)
        self.seen: list[dict] = []                      # a render pillanatának környezete (tenancy, heavy, nice, kapu)
        self._ctx: dict = {}

    def enabled(self):
        return True

    def plan(self, spec):
        return self._plan

    def runs(self, plan, ks):
        segs, out = plan["segments"], []
        for k in sorted(set(ks)):
            if out and out[-1][-1] == k - 1 and segs[k]["part"] == segs[k - 1]["part"] and len(out[-1]) < self.RUN_MAX:
                out[-1].append(k)
            else:
                out.append([k])
        return out

    def seg_path(self, source_id, h):
        return self.root / "preview-cache" / source_id / f"{h}.ts"

    def audio_dir(self, source_id, audio_hash):
        return self.root / "preview-cache" / source_id / f"a-{audio_hash}"

    async def context(self, pid):
        key = (pid, id(self._plan))
        if key not in self._ctx:
            self.contexts += 1
            await asyncio.sleep(0)
            self._ctx[key] = {"pid": pid, "plan": self._plan}
        return self._ctx[key]

    def _seen(self, what):
        from app import jobs
        from app.convert import engines
        from app.tools import video_preview
        g = video_preview.gate()
        self.seen.append({"what": what, "uid": tenancy.current_user_id(), "held": jobs._held.get(),
                          "background": engines._BACKGROUND.get(), "heavy_running": jobs._heavy_running,
                          "gate_free": getattr(g, "_value", None)})

    async def render_run(self, ctx, ks):
        from app.tools import video_preview
        self._seen(list(ks))
        self.calls.append(list(ks))
        async with video_preview.gate():                 # mint a P1-é: a kaput maga kéri
            return await self._run(ctx, ks)

    async def _run(self, ctx, ks):
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        out = []
        for k in ks:
            h = ctx["plan"]["segments"][k]["hash"]
            p = self.seg_path(ctx["plan"]["source_id"], h)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f".{p.name}.tmp")
            tmp.write_bytes(b"G" + h.encode() * 4)
            tmp.replace(p)
            out.append(h)
        return out

    async def render_audio(self, ctx):
        from app.tools import video_preview
        self._seen("audio")
        self.audio_calls += 1
        async with video_preview.gate():
            return await self._audio(ctx)

    async def _audio(self, ctx):
        if self.delay:
            await asyncio.sleep(self.delay)
        d = self.audio_dir(ctx["plan"]["source_id"], ctx["plan"]["audio_hash"])
        d.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (d / f"a-{i:03d}.ts").write_bytes(b"A" * 10)
        (d / "index.m3u8").write_text("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:0\n"
                                      "#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:2.005333,\na-000.ts\n#EXTINF:1.984000,\n"
                                      "a-001.ts\n#EXTINF:0.021333,\na-002.ts\n#EXT-X-ENDLIST\n")
        return d

    async def ready(self, hashes):
        return {h for h in hashes if self.seg_path(self._plan["source_id"], h).exists()}

    async def touch(self, hashes, source_id=None):
        assert source_id == self._plan["source_id"]
        self.touched += list(hashes)

    async def sweep(self):
        return {"deleted": 0}


@pytest.fixture()
def pc(monkeypatch, tmp_path):
    """Az álmodul a preview_cache helyén (a valódi még a P1 sávban készül), friss sor, a kapuk ebben a loopban."""
    import app.tools
    from app.tools import preview_serve, video_preview
    fake = FakePC(tmp_path, make_plan())
    monkeypatch.setitem(sys.modules, "app.tools.preview_cache", fake)
    monkeypatch.setattr(app.tools, "preview_cache", fake, raising=False)
    monkeypatch.setattr(video_preview, "_gate", None)
    monkeypatch.setattr(preview_serve, "TOUCH_EVERY_S", 0.05)
    preview_serve._reset()
    yield fake
    preview_serve._reset()


@pytest.fixture()
async def aq(pc):
    """Aszinkron teszt: a végén a dolgozók és a háttértaskok ebben a loopban állnak le (ne maradjon függő task)."""
    yield
    from app.tools import preview_serve as ps
    st = ps._S
    if st is not None and st.loop is asyncio.get_running_loop():
        tasks = st.workers + list(st.tasks) + ([st.touch_task] if st.touch_task else [])
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _drain():
    from app.tools import preview_serve as ps
    st = ps._state()
    for _ in range(1000):
        if not st.queue.qsize() and not st.live:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the preview queue did not drain")


@pytest.fixture()
def project(pc):
    """Egy videóprojekt az admin munkaterületén, a metájában spec-kel (a terv az álmodulé)."""
    from app.storage import output_path, write_meta
    output_path(PID).write_text("{}", "utf-8")
    write_meta(PID, label="clip.ecproj", extra={"spec": {"kind": "overlay", "base": {"file_id": SRC}},
                                                "preview": {"markers": [{"from": 4.0, "to": 8.0, "what": "cut"}]}})
    yield PID
    output_path(PID).unlink(missing_ok=True)


def _q(url: str) -> dict:
    from urllib.parse import parse_qs, urlparse
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def _uris(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln and not ln.startswith("#")]


# ---------- lejátszási listák ----------

@pytest.mark.parametrize("fps", [30.0, 30000 / 1001, 25.0])
def test_video_playlist_shape(pc, fps):
    from app.tools import preview_serve as ps
    plan = make_plan(fps=fps)
    text = ps.video_m3u8(PID, plan)
    lines = text.splitlines()
    segs = plan["segments"]
    assert lines[0] == "#EXTM3U" and lines[-1] == "#EXT-X-ENDLIST"
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in lines and "#EXT-X-MEDIA-SEQUENCE:0" in lines
    assert text.count("#EXT-X-DISCONTINUITY") == len(segs) - 1
    ext = [float(ln[8:].rstrip(",")) for ln in lines if ln.startswith("#EXTINF:")]
    assert len(ext) == len(segs)
    for d, s in zip(ext, segs):                                   # mindegyik a saját hossza: frames/fps
        assert abs(d - s["frames"] / fps) < 2e-6
    total_frames = sum(s["frames"] for s in segs)
    assert abs(sum(ext) - total_frames / fps) < 1e-3 and abs(sum(ext) - plan["total"]) < 1e-3
    target = int(next(ln for ln in lines if ln.startswith("#EXT-X-TARGETDURATION:")).split(":")[1])
    assert target == math.ceil(max(round(s["frames"] / fps, 6) for s in segs))
    # a DISCONTINUITY a szakaszok KÖZÖTT: az első EXTINF előtt nincs, utána minden EXTINF előtt
    i_inf = [i for i, ln in enumerate(lines) if ln.startswith("#EXTINF:")]
    assert all(lines[i - 1] == "#EXT-X-DISCONTINUITY" for i in i_inf[1:]) and lines[i_inf[0] - 1] != "#EXT-X-DISCONTINUITY"
    uris = _uris(text)
    assert [re.match(r"^/api/video/preview/seg/([0-9a-f]{64})\.ts\?", u).group(1) for u in uris] == [s["hash"] for s in segs]
    for u, s in zip(uris, segs):                                  # minden szakasz-URL a saját hash-ére aláírt
        q = _q(u)
        assert q["p"] == PID and ps.verify(PID, s["hash"], q["t"], tenancy.ADMIN_ID)
        assert not ps.verify(PID, None, q["t"], tenancy.ADMIN_ID)


def test_long_playlist_does_not_drift(pc):
    """30 perc, 29,97 fps, 900 szakasz: a kumulált kerekítés miatt Σ EXTINF = összes kocka / fps (1 ms-on belül)."""
    from app.tools import preview_serve as ps
    fps = 30000 / 1001
    plan = make_plan(parts=((None, 0.0, 1800.0),), hold_s=0, fps=fps)
    text = ps.video_m3u8(PID, plan)
    ext = [float(ln[8:].rstrip(",")) for ln in text.splitlines() if ln.startswith("#EXTINF:")]
    assert len(ext) == 900 and text.count("#EXT-X-DISCONTINUITY") == 899
    assert abs(sum(ext) - sum(s["frames"] for s in plan["segments"]) / fps) < 1e-3


def test_master_has_the_separate_audio_rendition(pc):
    from app.tools import preview_serve as ps
    plan = make_plan()
    lines = ps.master_m3u8(PID, plan).splitlines()
    media = next(ln for ln in lines if ln.startswith("#EXT-X-MEDIA:"))
    assert 'TYPE=AUDIO' in media and 'GROUP-ID="aud"' in media and "DEFAULT=YES" in media
    aud_uri = re.search(r'URI="([^"]+)"', media).group(1)
    assert aud_uri.startswith(f"/api/video/{PID}/audio.m3u8?t=")
    inf = next(ln for ln in lines if ln.startswith("#EXT-X-STREAM-INF:"))
    assert 'AUDIO="aud"' in inf and "RESOLUTION=960x540" in inf and 'CODECS="avc1.42c01f,mp4a.40.2"' in inf
    assert lines[lines.index(inf) + 1].startswith(f"/api/video/{PID}/video.m3u8?t=")
    tok = _q(aud_uri)["t"]
    assert ps.verify(PID, None, tok, tenancy.ADMIN_ID) and not ps.verify(PID, plan["audio_hash"], tok, tenancy.ADMIN_ID)
    silent = {**plan, "audio_hash": None}                        # hang nélkül: nincs rendition, csak a kép kodekje
    text = ps.master_m3u8(PID, silent)
    assert "#EXT-X-MEDIA" not in text and 'CODECS="avc1.42c01f"' in text and "AUDIO=" not in text


def test_codecs_level_follows_the_frame_size(pc):
    from app.tools import preview_serve as ps
    assert ps.codecs({"pw": 960, "ph": 540, "fps": 30}) == "avc1.42c01f"           # a prototípus: 3.1
    assert ps.codecs({"pw": 540, "ph": 960, "fps": 30}) == "avc1.42c01f"           # álló
    assert ps.codecs({"pw": 960, "ph": 540, "fps": 60}) == "avc1.42c020"           # 3.2
    assert ps.codecs({"pw": 1920, "ph": 1080, "fps": 30}) == "avc1.42c028"         # 4.0
    assert ps.codecs({"pw": 640, "ph": 360, "fps": 25}) == "avc1.42c01e"           # 3.0


async def test_audio_playlist_signs_its_pieces(aq, pc, project):
    from app.tools import preview_serve as ps
    plan = pc._plan
    d = await ps.request_audio(PID, plan=plan)
    assert (d / "index.m3u8").exists() and pc.audio_calls == 1
    text = ps.audio_m3u8(PID, plan)
    uris = _uris(text)
    assert [u.split("?")[0] for u in uris] == [f"/api/video/preview/aud/{plan['audio_hash']}/a-{i:03d}.ts" for i in range(3)]
    assert all(ps.verify(PID, plan["audio_hash"], _q(u)["t"], tenancy.ADMIN_ID) for u in uris)
    assert "#EXT-X-DISCONTINUITY" not in text and "#EXTINF:2.005333," in text      # a folytonos hang: ahogy az ffmpeg írta
    await ps.request_audio(PID, plan=plan)
    assert pc.audio_calls == 1                                                       # kész: nem készül újra


# ---------- végpontok: aláírás, 404, 503, kiszolgálás ----------

def _seg_url(pid, h, **kw):
    from app.tools import preview_serve as ps
    tok, _, uid = ps.sign(pid, h, **kw)
    return f"/api/video/preview/seg/{h}.ts?p={pid}&t={tok}" + ("" if uid == tenancy.ADMIN_ID else f"&u={uid}")


def test_playlist_endpoints(client, pc, project):
    from app.tools import preview_serve as ps
    q = ps.signed(PID)
    r = client.get(f"/api/video/{PID}/preview.m3u8{q}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert "no-store" in r.headers["cache-control"]
    vurl = _uris(r.text)[0]
    aurl = re.search(r'URI="([^"]+)"', r.text).group(1)
    rv = client.get(vurl)
    assert rv.status_code == 200 and rv.text.count("#EXT-X-DISCONTINUITY") == len(pc._plan["segments"]) - 1
    ra = client.get(aurl)                                       # a hang még nincs: elsőbbséggel elkészül, a lista utána jön
    assert ra.status_code == 200 and pc.audio_calls == 1 and len(_uris(ra.text)) == 3
    piece = client.get(_uris(ra.text)[0])
    assert piece.status_code == 200 and piece.headers["content-type"] == "video/mp2t" and piece.content == b"A" * 10
    assert client.get(_uris(ra.text)[0].replace("a-000.ts", "index.m3u8")).status_code == 404
    assert client.get(_uris(ra.text)[0].replace("a-000.ts", "..%2Fx.ts")).status_code == 404
    seg = client.get(_uris(rv.text)[0])                        # egy szakasz: a végpont rendereli, aztán kiszolgálja
    assert seg.status_code == 200 and seg.headers["content-type"] == "video/mp2t"
    assert seg.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert seg.content.startswith(b"G" + pc._plan["segments"][0]["hash"].encode())


@pytest.mark.parametrize("case", ["wrong_pid", "wrong_hash", "expired", "playlist_token", "tampered", "wrong_user", "no_token"])
def test_segment_signature_is_checked(client, pc, project, case):
    from app.tools import preview_serve as ps
    segs = pc._plan["segments"]
    h, other = segs[1]["hash"], segs[2]["hash"]
    other_pid = "ffffffffffffffffffffffffffffffff.ecproj"
    url = {"wrong_pid": _seg_url(other_pid, h).replace(f"p={other_pid}", f"p={PID}"),
           "wrong_hash": _seg_url(PID, other).replace(f"/seg/{other}.ts", f"/seg/{h}.ts"),
           "expired": _seg_url(PID, h, ttl=-5),
           "playlist_token": f"/api/video/preview/seg/{h}.ts?p={PID}&{ps.signed(PID)[1:]}",
           "tampered": (lambda x: x[:-1] + ("0" if x[-1] != "0" else "1"))(_seg_url(PID, h)),
           "wrong_user": _seg_url(PID, h) + "&u=abcdef123456",
           "no_token": f"/api/video/preview/seg/{h}.ts?p={PID}"}[case]
    r = client.get(url)
    assert r.status_code == (422 if case == "no_token" else 403)
    assert pc.calls == []                                       # aláírás nélkül semmi sem renderelődik


def test_playlist_signature_is_checked(client, pc, project):
    from app.tools import preview_serve as ps
    seg_tok = ps.sign(PID, pc._plan["segments"][0]["hash"])[0]
    file_tok = __import__("app.storage.files", fromlist=["x"]).sign_file_id(PID)[0]
    for t in (seg_tok, file_tok, ps.sign(PID, ttl=-5)[0], "garbage"):
        for name in ("preview.m3u8", "video.m3u8", "audio.m3u8", "preview.json"):
            assert client.get(f"/api/video/{PID}/{name}?t={t}").status_code == 403, (t, name)
    assert client.get(f"/files/{PID}{ps.signed(PID)}").status_code == 403    # a lista tokenje nem tölti le a projektet


def test_other_workspace_signed_link(client, pc):
    """Nem admin munkaterület: az u= a tokenbe aláírva, a végpont annak a munkaterületén keresi a projektet."""
    from app.storage import output_path, write_meta
    from app.tools import preview_serve as ps
    uid = "abcdef123456"
    tok = tenancy.set_user(uid)
    try:
        output_path(PID).write_text("{}", "utf-8")
        write_meta(PID, extra={"spec": {"kind": "overlay", "base": {"file_id": SRC}}})
        q = ps.signed(PID)
    finally:
        tenancy.reset(tok)
    assert q.endswith(f"&u={uid}")
    assert client.get(f"/api/video/{PID}/video.m3u8{q}").status_code == 200
    assert client.get(f"/api/video/{PID}/video.m3u8{q.replace(uid, 'abcdef654321')}").status_code == 403
    assert client.get(f"/api/video/{PID}/video.m3u8{q.split('&u=')[0]}").status_code == 403   # admin-ként nem érvényes


def test_hash_not_in_the_plan_is_404(client, pc, project):
    stranger = _h("not", "in", "plan")
    r = client.get(_seg_url(PID, stranger))
    assert r.status_code == 404 and pc.calls == []
    r = client.get(_seg_url("eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee.ecproj", pc._plan["segments"][0]["hash"]))
    assert r.status_code == 404                                # nincs ilyen projekt (az aláírás jó)


def test_slow_segment_gives_503_with_retry_after_then_serves(client, pc, project, monkeypatch):
    monkeypatch.setenv("VIDEO_PREVIEW_SEG_WAIT_S", "0.2")
    pc.delay = 1.0
    h = pc._plan["segments"][3]["hash"]
    url = _seg_url(PID, h)
    r = client.get(url)
    assert r.status_code == 503 and r.headers["retry-after"] == "2"
    r2 = client.get(url)                                        # az újrapróbálás ugyanarra a renderre vár (nem indít újat)
    assert r2.status_code == 503
    deadline = time.time() + 10
    while time.time() < deadline and not pc.seg_path(SRC, h).exists():
        time.sleep(0.05)
    r3 = client.get(url)
    assert r3.status_code == 200 and r3.content.startswith(b"G" + h.encode())
    assert [ks for ks in pc.calls if 3 in ks] == [[3, 4]]       # a k futama (a klip része: 3–4), egyszer


def test_failed_render_is_500_and_held_briefly(client, pc, project, monkeypatch):
    monkeypatch.setenv("VIDEO_PREVIEW_PREFETCH", "0")

    async def boom(ctx, ks):
        pc.calls.append(list(ks))
        raise RuntimeError("engine said no")
    pc.render_run = boom
    url = _seg_url(PID, pc._plan["segments"][0]["hash"])
    r = client.get(url)
    assert r.status_code == 500 and "engine said no" in r.json()["detail"]
    assert client.get(url).status_code == 500 and len(pc.calls) == 1     # FAIL_HOLD_S alatt nem renderel újra


def test_preview_json_shape_and_final_status(client, pc, project):
    from app import jobs
    from app.storage import output_path, write_meta
    from app.tools import preview_serve as ps
    url = f"/api/video/{PID}/preview.json{ps.signed(PID)}"
    j = client.get(url).json()
    assert set(j) == {"playlist_url", "duration", "segments_total", "segments_ready", "markers", "final"}
    assert j["playlist_url"].startswith(f"http://testserver/api/video/{PID}/preview.m3u8?t=")
    assert j["duration"] == pytest.approx(pc._plan["total"]) and j["segments_total"] == len(pc._plan["segments"])
    assert j["segments_ready"] == 0 and j["markers"] == [{"from": 4.0, "to": 8.0, "what": "cut"}]
    assert j["final"] == {"status": "none", "eta": None, "eta_s": None, "ready_at": None, "video_url": None}
    assert client.get(_seg_url(PID, pc._plan["segments"][0]["hash"])).status_code == 200
    assert client.get(url).json()["segments_ready"] >= 1
    now = time.time()
    job = {"id": "f1nal0000000000a", "kind": "render", "owner": "admin", "status": "running", "done": 0, "total": 1,
           "stage": "", "result": None, "error": None, "started": now, "eta_s": 600, "eta_from": now, "final": True,
           "project": PID}
    jobs._JOBS[job["id"]] = job
    try:
        f = client.get(url).json()["final"]
        assert f["status"] == "running" and isinstance(f["eta"], str) and f["video_url"] is None
        assert 590 <= f["eta_s"] <= 600 and re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d", f["ready_at"])
        vid = "1234567890abcdef1234567890abcdef.mp4"
        output_path(vid).write_bytes(b"mp4")
        job.update(status="done", result={"file_id": vid, "url": f"http://testserver/files/{vid}?t=x"})
        assert client.get(url).json()["final"] == {"status": "done", "eta": None, "eta_s": None, "ready_at": None,
                                                   "video_url": f"http://testserver/files/{vid}?t=x"}
        job.update(status="failed", result=None)
        assert client.get(url).json()["final"]["status"] == "failed"
        jobs._JOBS.pop(job["id"])                               # újraindítás után: a meta dönt
        write_meta(PID, extra={"render_file_id": vid, "render_draft": False})
        f = client.get(url).json()["final"]
        assert f["status"] == "done" and f["video_url"].startswith(f"http://testserver/files/{vid}?t=")
        write_meta(PID, extra={"render_draft": True})            # a gyors vázlat nem a végleges
        assert client.get(url).json()["final"]["status"] == "none"
    finally:
        jobs._JOBS.pop(job["id"], None)
        output_path(vid).unlink(missing_ok=True)


# ---------- a sor: dedupe, elsőbbség, heavy-mentesség ----------

async def test_dedupe_one_render_for_everyone(aq, pc, project):
    from app.tools import preview_serve as ps
    plan = pc._plan
    pc.delay = 0.1
    paths = await asyncio.gather(ps.request(PID, 1, plan=plan), ps.request(PID, 1, plan=plan),
                                 ps.request(PID, 2, plan=plan, priority=ps.P_EDIT))
    assert paths[0] == paths[1] == pc.seg_path(SRC, plan["segments"][1]["hash"])
    assert pc.calls[0] == [1, 2]                                # a k futama: k-tól előre, a rész végéig
    assert sum(1 in ks or 2 in ks for ks in pc.calls) == 1
    again = await ps.request(PID, 2, plan=plan)
    assert again.exists() and sum(2 in ks for ks in pc.calls) == 1


async def test_same_content_twice_in_the_plan_renders_once(aq, pc, project):
    from app.tools import preview_serve as ps
    plan = make_plan(parts=((None, 0.0, 4.0), (None, 0.0, 4.0)), hold_s=0)   # ugyanaz a rész kétszer (pl. ismételt klip)
    pc._plan = plan
    assert plan["segments"][0]["hash"] == plan["segments"][2]["hash"]
    await asyncio.gather(ps.request(PID, 0, plan=plan), ps.request(PID, 2, plan=plan))
    assert sorted(k for ks in pc.calls for k in ks) == [0, 1]


async def test_player_request_overtakes_background_fill(aq, pc, project, monkeypatch):
    from app.tools import preview_serve as ps
    monkeypatch.setenv("VIDEO_PREVIEW_WORKERS", "1")
    monkeypatch.setenv("VIDEO_PREVIEW_PREFETCH", "0")
    plan = make_plan(parts=((None, 0.0, 80.0),), hold_s=0)                    # 40 szakasz, egy rész
    pc._plan = plan
    pc.gate = asyncio.Event()
    ps.schedule(PID, plan, dirty=[], playhead=0.0)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if pc.calls:
            break
    assert pc.calls == [[0, 1, 2, 3]]                          # az egyetlen dolgozó a 0. futamon áll (elsőbbség 1)
    st = ps._state()
    assert st.queue.qsize() >= 9                                # a többi háttér-feltöltésként vár (elsőbbség 3)
    late = asyncio.create_task(ps.request(PID, 33, plan=plan))  # a lejátszó a 33.-at kéri (tekerés)
    await asyncio.sleep(0.05)
    pc.gate.set()
    await late
    assert pc.calls[1] == [32, 33, 34, 35]                     # rögtön a 0. futam után, a sorban várók előtt: a 33.-at már
    #                                                             a feltöltés egy futama tartalmazta — az került előre
    await _drain()
    done = sorted(k for ks in pc.calls for k in ks)
    assert done == list(range(40))                             # mind elkészült, mindegyik egyszer


async def test_schedule_priorities_and_supersede(aq, pc, project, monkeypatch):
    from app.tools import preview_serve as ps
    monkeypatch.setenv("VIDEO_PREVIEW_WORKERS", "1")
    monkeypatch.setenv("VIDEO_PREVIEW_PREFETCH", "4")
    old = make_plan(parts=((None, 0.0, 60.0),), hold_s=0, salt="v1")
    new = make_plan(parts=((None, 0.0, 60.0),), hold_s=0, salt="v1")
    new["audio_hash"] = _h("v2", "audio")                       # a hang is változott (pl. vágás)
    for k in range(20, 24):                                     # az új verzióban a 20–23. szakasz változott
        new["segments"][k]["hash"] = _h("v2", k)
    pc._plan = old
    pc.gate = asyncio.Event()
    ps.schedule(PID, old, dirty=[], playhead=0.0)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if pc.calls:
            break
    st = ps._state()
    old_fill = [j for j in set(st.live.values()) if j.prio == ps.P_FILL]
    assert old_fill
    from app.storage import output_path
    new_pid = "11111111111111111111111111111111.ecproj"         # az új verzió: új projektfájl, ugyanaz a felvétel
    output_path(new_pid).write_text("{}", "utf-8")
    pc._plan = new
    ps.schedule(new_pid, new, dirty=[20, 21, 22, 23], playhead=40.0)
    await asyncio.sleep(0.05)
    assert all(j.fut.done() for j in old_fill)                 # a régi verzió el nem kezdett feltöltése elmaradt
    live = {}
    for j in set(st.live.values()):
        live.setdefault(j.prio, []).append(j.ks)
    assert [20, 21, 22, 23] in live[ps.P_EDIT]                 # az első piszkos: elsőbbség 1 (a kérés ezt várja)
    assert any(20 in ks for ks in live[ps.P_EDIT])
    assert any(ks[0] == 24 for ks in live[ps.P_NEAR])          # a lejátszófej (40 s = 20.) utáni néhány: elsőbbség 2
    assert ("a:" + new["audio_hash"]) in st.live and st.live["a:" + new["audio_hash"]].prio == ps.P_EDIT
    pc.gate.set()
    await _drain()
    order = [ks for ks in pc.calls]
    assert order[0] == [0, 1, 2, 3]                            # ami már futott
    assert order[1] == [20, 21, 22, 23]                        # utána az elsőbbség 1
    assert order[2][0] == 24                                   # aztán a lejátszófej körüli
    hashes = [new["segments"][k]["hash"] for k in range(30)]
    assert all(pc.seg_path(SRC, h).exists() for h in hashes)   # az új verzió minden szakasza kész
    assert sorted(k for ks in pc.calls for k in ks) == list(range(30))     # mindegyik egyszer (a közöseket nem kétszer)


async def test_workers_never_take_the_heavy_gate(aq, pc, project, monkeypatch):
    """PR #41: az előnézet sosem áll be a jobs.heavy() mögé — a dolgozó üres kontextusban indul (a hívó heavy-helye és
    a végleges render nice 10-e nem öröklődik); a video_preview.gate()-et a render maga kéri, a dolgozó nem tartja."""
    from app import jobs
    from app.convert import engines
    from app.tools import preview_serve as ps

    def no_heavy(*a, **k):
        raise AssertionError("a preview worker took jobs.heavy()")
    held = jobs._held.set(True)                                 # a hívó (pl. egy fájleszköz) a heavy kapun belül van
    bg = engines._BACKGROUND.set(True)                          # … és a végleges render háttér-kontextusában
    try:
        monkeypatch.setattr(jobs, "heavy", no_heavy)
        await ps.request(PID, 0, plan=pc._plan, priority=ps.P_EDIT)
        await ps.request_audio(PID, plan=pc._plan)
    finally:
        engines._BACKGROUND.reset(bg)
        jobs._held.reset(held)
    assert [s["what"] for s in pc.seen] == [[0, 1, 2], "audio"]
    assert all(s["held"] is False and s["background"] is False for s in pc.seen), pc.seen
    assert all(s["gate_free"] == 2 for s in pc.seen), pc.seen   # a dolgozó nem foglal a kapuból (a render kéri)


async def test_two_workers_and_a_gate_taken_inside_do_not_deadlock(aq, pc, project, monkeypatch):
    """A preview_cache a render_run / render_audio / context-építés idejére maga kéri a video_preview.gate()-et (2 hely,
    nem újrabelépő). Ha a dolgozó is tartaná, két dolgozó mellett a belső kérés örökre várna."""
    from app.tools import preview_serve as ps
    monkeypatch.setenv("VIDEO_PREVIEW_WORKERS", "2")
    plan = make_plan(parts=((None, 0.0, 40.0),), hold_s=0)
    pc._plan = plan
    pc.delay = 0.05
    await asyncio.wait_for(asyncio.gather(*(ps.request(PID, k, plan=plan, priority=ps.P_EDIT) for k in (0, 4, 8, 12)),
                                          ps.request_audio(PID, plan=plan)), 5)
    assert sorted(k for ks in pc.calls for k in ks) == list(range(16))


async def test_the_job_runs_in_the_callers_workspace(aq, pc, monkeypatch):
    from app.storage import output_path, write_meta
    from app.tools import preview_serve as ps
    uid = "fedcba654321"
    tok = tenancy.set_user(uid)
    try:
        output_path(PID).write_text("{}", "utf-8")
        write_meta(PID, extra={"spec": {"kind": "overlay", "base": {"file_id": SRC}}})
        await ps.request(PID, 0, priority=ps.P_EDIT)
    finally:
        tenancy.reset(tok)
    assert pc.seen[0]["uid"] == uid


async def test_player_hits_are_touched_and_prefetch_ahead(aq, pc, project, monkeypatch):
    from app.tools import preview_serve as ps
    monkeypatch.setenv("VIDEO_PREVIEW_PREFETCH", "3")
    plan = make_plan(parts=((None, 0.0, 40.0),), hold_s=0)
    pc._plan = plan
    await ps.request(PID, 0, plan=plan, priority=ps.P_EDIT)     # nem a lejátszó: nincs előtöltés
    assert pc.calls == [[0, 1, 2, 3]]
    await ps.request(PID, 2, plan=plan)                          # találat a lejátszótól: a touch + előtöltés (4–6)
    st = ps._state()
    await _drain()
    assert pc.calls[1][0] == 4 and sorted(k for ks in pc.calls for k in ks)[:7] == list(range(7))
    await asyncio.sleep(0.15)
    assert plan["segments"][2]["hash"] in pc.touched


async def test_timeout_raises_and_the_render_goes_on(aq, pc, project, monkeypatch):
    from app.tools import preview_serve as ps
    pc.delay = 0.3
    with pytest.raises(TimeoutError):
        await ps.request(PID, 5, plan=pc._plan, timeout=0.05)
    p = await ps.request(PID, 5, plan=pc._plan)                 # ugyanarra vár
    assert p.exists() and sum(5 in ks for ks in pc.calls) == 1


def test_signed_preview_links_need_no_login(client, pc, project, monkeypatch):
    """Belépéses szerveren is: a lejátszó (Safari natív HLS, megosztott link, vendég) süti nélkül kéri — mint a /files/;
    a jog az aláírás. Ami nem aláírt előnézet-cím, az továbbra is belépést kér."""
    from app.config import settings
    from app.tools import preview_serve as ps
    monkeypatch.setattr(settings, "DASHBOARD_PASSWORD", "secret")
    client.cookies.clear()
    q = ps.signed(PID)
    for name in ("preview.m3u8", "video.m3u8", "preview.json"):
        assert client.get(f"/api/video/{PID}/{name}{q}").status_code == 200, name
    assert client.get(_seg_url(PID, pc._plan["segments"][0]["hash"])).status_code == 200
    assert client.get(f"/api/video/{PID}/video.m3u8?t=forged").status_code == 403       # nyitva, de az aláírás dönt
    assert client.post("/api/video/render", json={"file_id": PID}).status_code == 401   # a többi videó-végpont nem nyílt meg
    assert client.get(f"/api/video/{PID}/video.m3u8/x{q}").status_code == 401           # csak a pontos címek


def test_playlist_request_warms_segment_zero_and_the_sound(client, pc, project, monkeypatch):
    """P4: a hls.js mindig a 0. szakasszal indul — a lista kérése a 0. futamát és a hangot elsőbbséggel sorba állítja."""
    from app.tools import preview_serve as ps
    monkeypatch.setenv("VIDEO_PREVIEW_PREFETCH", "0")
    r = client.get(f"/api/video/{PID}/preview.m3u8{ps.signed(PID)}")
    assert r.status_code == 200
    deadline = time.time() + 10
    while time.time() < deadline and not (pc.calls and pc.audio_calls):
        time.sleep(0.02)
    assert pc.calls[0] == [0, 1, 2] and pc.audio_calls == 1
    client.get(f"/api/video/{PID}/video.m3u8{ps.signed(PID)}")       # már kész: nem renderel újra
    time.sleep(0.1)
    assert pc.calls == [[0, 1, 2]] and pc.audio_calls == 1


def test_edit_route_passes_preview_through(client, pc, monkeypatch):
    """preview="mp4" eléri a video.edit-et (P3: a kész szakaszok egy letölthető MP4-ben); nélküle a kulcs nem megy át."""
    from app.storage import output_path
    from app.tools import video
    got = []

    async def edit(fid, changes, **kw):
        got.append(kw)
        return {"files": []}
    monkeypatch.setattr(video, "edit", edit)
    monkeypatch.setattr(video, "check_music", lambda m: None)
    output_path(PID).write_text("{}", "utf-8")
    for body in ({"file_id": PID, "changes": {}, "preview": "mp4"}, {"file_id": PID, "changes": {}}):
        job = client.post("/api/video/edit", json=body).json()
        for _ in range(200):
            st = client.get(f"/api/jobs/{job['id']}").json()
            if st["status"] != "running":
                break
            time.sleep(0.02)
        assert st["status"] == "done", st
    assert got[0]["preview"] == "mp4" and "preview" not in got[1]
