"""A szakasz-gyorsítótáras HLS-előnézet bekötése az overlay / edit útba (10-25, P3 sáv) — a P1/P2 modulok helyén a
szerződés (plan20261025_preview_cache.md, „API v1”) szerinti csonkokkal:
- a válasz preview mezője: playlist_url, segments_total/dirty/ready, first_ready_s, reused, markers;
- a kérés csak a 0. és az első piszkos szakaszt várja (P_EDIT), a többi a schedule-ön; a meta preview-ja a markerekkel;
- szerkesztésnél a szülő terve szerint csak a változott szakaszok piszkosak;
- preview="mp4": a kész szakaszok -c copy egy MP4-be, a projekt vázlat-renderje (a files preview at=… erről mutat);
- VIDEO_PREVIEW_HLS=0: a PR #40-es MP4-előnézet;
- színkorrekció: a munkamásolat a háttérben (spec.base.proxy_pending), a szerkesztés nem várja meg; a teljes projekt
  építése előbb a munkamásolatot készíti el, és a verzió az újat kapja.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

SEG = 2.0


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


def _clip(path: Path, secs: int = 8) -> Path:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=10", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=48000", "-t", str(secs), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def _fakes(root: Path, calls: dict):
    """preview_cache + preview_serve csonk: 2 s-os rács, a szakasz hash-e a rá eső rétegekből; a „render” egy valódi
    TS-darab (ffmpeg), a hang egy valódi HLS-hang."""
    pc = types.ModuleType("app.tools.preview_cache")
    ps = types.ModuleType("app.tools.preview_serve")

    def plan(spec):
        base = spec["base"]
        total = float(base["duration"])
        segs, k, t = [], 0, 0.0
        while t < total - 1e-6:
            d = min(SEG, total - t)
            lys = [ly for ly in (spec.get("story") or {}).get("layers") or []
                   if float(ly["at"]) < t + d and float(ly["at"]) + float(ly["dur"]) > t]
            fp = {"src": [t, d], "recipe": base.get("adjust"), "layers": [{**ly, "at": float(ly["at"]) - t} for ly in lys]}
            h = hashlib.sha256(json.dumps(fp, sort_keys=True).encode()).hexdigest()
            segs.append({"k": k, "out_start": t, "dur": d, "frames": round(d * 10), "hash": h})
            k, t = k + 1, t + d
        return {"source_id": base["file_id"], "segments": segs, "total": total, "audio_hash": "a1"}

    def diff(old, new):
        have = {s["hash"] for s in (old or {}).get("segments") or []}
        dirty = [s["k"] for s in new["segments"] if s["hash"] not in have]
        return {"dirty": dirty, "reused": len(new["segments"]) - len(dirty), "total": len(new["segments"])}

    def seg_path(source_id, h):
        return root / source_id / f"{h}.ts"

    async def ready(hashes, source_id=None):
        return {h for h in hashes if seg_path(source_id, h).exists()}
    pc.enabled, pc.plan, pc.diff, pc.seg_path, pc.ready = (lambda: True), plan, diff, seg_path, ready

    def plan_of(pid):
        from app.storage import read_meta
        return plan(read_meta(pid)["spec"])

    async def request(pid, k, *, priority=0, plan=None, timeout=None):
        plan = plan or plan_of(pid)
        sg = plan["segments"][k]
        p = seg_path(plan["source_id"], sg["hash"])
        calls.setdefault("request", []).append((pid, k, priority))
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=480x270:rate=10", "-t",
                            f"{sg['dur']:.3f}", "-c:v", "libx264", "-preset", "ultrafast", "-g", "100", "-pix_fmt", "yuv420p",
                            "-f", "mpegts", str(p)], check=True)
        return p

    async def request_audio(pid, *, priority=0, plan=None, timeout=None):
        plan = plan or plan_of(pid)
        d = root / plan["source_id"] / "a-a1"
        if not (d / "index.m3u8").exists():
            d.mkdir(parents=True, exist_ok=True)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t",
                            f"{plan['total']:.3f}", "-c:a", "aac", "-b:a", "128k", "-f", "hls", "-hls_time", "2",
                            "-hls_playlist_type", "vod", str(d / "index.m3u8")], check=True)
        return d

    def schedule(pid, plan, dirty, *, playhead=0.0):
        calls.setdefault("schedule", []).append((pid, list(dirty), playhead))
    ps.P_EDIT, ps.plan_of, ps.request, ps.request_audio, ps.schedule = 1, plan_of, request, request_audio, schedule
    ps.playlist_url = lambda pid: f"https://studio.test/api/video/{pid}/preview.m3u8?t=sig"
    return pc, ps


@pytest.fixture
def hls(tmp_path, monkeypatch):
    import app.tools as tools
    calls: dict = {}
    pc, ps = _fakes(tmp_path / "cache", calls)
    for name, mod in (("preview_cache", pc), ("preview_serve", ps)):
        monkeypatch.setitem(sys.modules, f"app.tools.{name}", mod)
        monkeypatch.setattr(tools, name, mod, raising=False)
    monkeypatch.delenv("VIDEO_PREVIEW_HLS", raising=False)
    return calls


def _reset_gates():
    from app.tools import video, video_preview
    video._gate = video._bgate = None
    video_preview._gate = None


LAYERS = [{"at": 1, "dur": 1.5, "type": "source", "text": "Forrás: teszt"},
          {"at": 5, "dur": 1.5, "type": "source", "text": "Második"}]


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_the_answer_has_the_playlist_and_waits_only_for_the_first_change(tmp_path, put_file, hls, monkeypatch):
    from app.storage import output_path, read_meta
    from app.tools import video
    fid = put_file(_clip(tmp_path / "talk.mp4"))
    monkeypatch.setattr(video, "_start_final", lambda pid, eta: {"id": "stub"})

    async def main():
        _reset_gates()
        res = await video.overlay({"file_id": fid, "brand": "none", "final": "background", "layers": LAYERS})
        pv, pid = res["preview"], res["file_id"]
        assert set(pv) >= {"playlist_url", "segments_total", "segments_dirty", "segments_ready", "first_ready_s", "reused",
                           "markers"}
        assert pv["playlist_url"].endswith(f"{pid}/preview.m3u8?t=sig") and pv["page_url"].endswith(f"/app/file/{pid}/preview")
        assert pv["segments_total"] == 4 and pv["segments_dirty"] == 4 and pv["reused"] == 0     # új videó: mind új
        assert [k for p, k, prio in hls["request"]] == [0] and all(prio == 1 for *_, prio in hls["request"])
        assert hls["schedule"] == [(pid, [0, 1, 2, 3], 0.0)]
        assert pv["first_ready_s"] is not None and pv["segments_ready"] == 1
        assert [m["what"] for m in pv["markers"]] == ["L1 source", "L2 source"]
        assert read_meta(pid)["preview"]["markers"] == pv["markers"]
        assert res["files"] == [res["project"]]                                   # nincs MP4 (csak ha kérik)
        assert any(v.startswith("Preview of the whole video") for v in res["check"]["verified"])
        # szerkesztés: a szülőhöz mérve csak a változott réteg szakasza piszkos; az első piszkos elsőbbséggel
        hls["request"].clear()
        ed = await video.edit(pid, {"layers": [{"id": "L2", "text": "Második (javítva)"}]}, final="background", preview="mp4")
        e = ed["preview"]
        assert e["segments_dirty"] == 2 and e["reused"] == 2, e                    # 4–6 s és 6–8 s (5–6,5 s a réteg)
        assert [k for _, k, _ in hls["request"][:2]] == [0, 2]
        assert [m["what"] for m in e["markers"]] == ["L2 source"]
        mp4 = e["mp4"]
        assert mp4["whole"] and mp4["to"] == pytest.approx(8.0) and mp4 in ed["files"]
        info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
                                          str(output_path(mp4["file_id"]))], capture_output=True, text=True).stdout)
        assert float(info["format"]["duration"]) == pytest.approx(8.0, abs=0.2)
        assert {s["codec_type"] for s in info["streams"]} == {"video", "audio"}
        meta = read_meta(ed["file_id"])
        assert meta["render_file_id"] == mp4["file_id"] and meta["render_draft"] is True   # a files preview at=… erről
        vid, t, _ = video.frame_time(ed["file_id"], at=5.5)
        assert vid == mp4["file_id"] and t == 5.5
    asyncio.run(main())


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_hls_off_keeps_the_old_mp4_preview(tmp_path, put_file, hls, monkeypatch):
    from app.tools import video
    fid = put_file(_clip(tmp_path / "talk.mp4"))
    monkeypatch.setattr(video, "_start_final", lambda pid, eta: {"id": "stub"})
    monkeypatch.setenv("VIDEO_PREVIEW_HLS", "0")

    async def main():
        _reset_gates()
        res = await video.overlay({"file_id": fid, "brand": "none", "final": "background", "layers": LAYERS})
        assert res["preview"]["parts"] and res["preview"]["file_id"].endswith(".mp4") and not hls.get("request")
    asyncio.run(main())


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_a_colour_change_does_not_wait_for_the_working_copy(tmp_path, put_file, hls, monkeypatch):
    from app.storage import output_path, read_meta
    from app.tools import footage, video
    fid = put_file(_clip(tmp_path / "talk.mp4"))
    monkeypatch.setattr(video, "_start_final", lambda pid, eta: {"id": "stub"})
    made = []
    real = footage._build_proxy

    async def spy(base, src):
        made.append(base.get("adjust"))
        return await real(base, src)
    monkeypatch.setattr(footage, "_build_proxy", spy)

    async def main():
        _reset_gates()
        res = await video.overlay({"file_id": fid, "brand": "none", "final": "background", "layers": LAYERS})
        b0 = read_meta(res["file_id"])["spec"]["base"]
        assert made == [None] and b0.get("media_id") and not b0.get("proxy_pending")
        ed = await video.edit(res["file_id"], {"adjust": {"brightness": 0.1, "saturation": 1.2}}, final="background")
        pid = ed["file_id"]
        b1 = read_meta(pid)["spec"]["base"]
        assert made == [None], "the edit must not wait for the new working copy"
        assert b1["proxy_pending"] is True and b1["adjust"] == {"brightness": 0.1, "saturation": 1.2}
        assert b1["media_id"] == b0["media_id"] and b1["asset"] == b0["asset"]   # a helyén a szülőé (ugyanaz a méret)
        assert video.full_pending(pid) and read_meta(pid)["full_build"]["proxy_base"]["adjust"] == b1["adjust"]
        assert any("being made in the background" in v for v in ed["check"]["verified"])
        await video.ensure_full(pid)                                             # (a végleges render első lépése)
        b2 = read_meta(pid)["spec"]["base"]
        assert made == [None, {"brightness": 0.1, "saturation": 1.2}] and "proxy_pending" not in b2
        assert b2["media_id"] != b0["media_id"] and read_meta(pid)["assets"][b2["asset"]] == b2["media_id"]
        assert b2["asset"] in output_path(pid).read_text("utf-8")                # a projekt az új munkamásolatot látja
        assert not video.full_pending(pid)
    asyncio.run(main())


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_if_the_playlist_fails_the_old_mp4_preview_comes(tmp_path, put_file, hls, monkeypatch):
    import app.tools as tools
    from app.tools import video
    fid = put_file(_clip(tmp_path / "talk.mp4"))
    monkeypatch.setattr(video, "_start_final", lambda pid, eta: {"id": "stub"})

    def broken(pid):
        raise LookupError("no plan")
    monkeypatch.setattr(tools.preview_serve, "plan_of", broken)

    async def main():
        _reset_gates()
        res = await video.overlay({"file_id": fid, "brand": "none", "final": "background", "layers": LAYERS})
        assert res["preview"]["parts"] and res["preview"]["file_id"].endswith(".mp4")
    asyncio.run(main())
