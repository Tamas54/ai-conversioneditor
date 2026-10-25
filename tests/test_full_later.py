"""A hosszú videó projektje a kérés útjában felirat NÉLKÜL, a teljes (feliratos) a háttérben (10-25,
.claude/qa/long30/PROFILE.md: egy szerkesztés 142–163 s volt, ennek ~99 %-a a felirat-ciklus a projekt felépítésében):
- a válasz előtt a projektfájl felirat nélküli, a verzió metája full_project = "pending", az előnézet-projekt asset;
- akinek a teljes kell (render, render scene=…, a /files/ — a szerkesztő), annak megépül, egyszer; utána "ready";
- a végleges render első lépése a teljes projekt;
- rövid videó, final="wait", draft: minden a régi módon (a teljes projekt a kérésben);
- az MP4-előnézeten a felirat a libass-é; a végig látható réteg nem alkot szakaszt.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.tools import video_preview as vp


def _reset_gates():
    from app.tools import video
    video._gate = video._bgate = None
    vp._gate = None


def test_a_persistent_layer_forms_no_span():
    spec = {"story": {"layers": [{"id": "L1", "type": "graphic", "at": 3.5, "dur": 1811.5},
                                 {"id": "L2", "type": "lower_third", "at": 4, "dur": 6},
                                 {"id": "L3", "type": "chapter", "at": 121, "dur": 3}]}, "base": {}}
    spans = vp.spans_new(spec, 1821.0)
    assert [lab for _, _, lab in spans] == ["L2 lower_third", "L3 chapter"]
    picked, left = vp.pick(spans, 1821.0)
    assert picked == [(2.0, 12.0, "L2 lower_third"), (119.0, 126.0, "L3 chapter")] and not left
    # csak a vízjel változott: minták a videó hosszában, nem egy 30 perces szakasz
    edited = vp.spans_edit(spec, spec, {"layers": [{"id": "L1", "opacity": 0.5}]}, 1821.0)
    assert edited and all(b - a <= vp.SAMPLE_S + 1e-6 for a, b, _ in edited)
    assert all(lab == "L1 graphic" for _, _, lab in edited)
    # ha csak vízjel van: mintái
    only = vp.spans_new({"story": {"layers": [spec["story"]["layers"][0]]}, "base": {}}, 1821.0)
    assert only and all(b - a <= vp.SAMPLE_S + 1e-6 for a, b, _ in only)


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


def _clip(path: Path, secs: int = 8) -> Path:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=10", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=48000", "-t", str(secs), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def _captions_in(p: Path) -> int:
    """A projekt fő kompozíciójának felirat-rétegei (a builder „Caption N” nevén)."""
    import re
    return len(set(re.findall(r'"name":\s*"Caption (\d+)"', p.read_text("utf-8"))))


SUBS = [{"start": 0.5, "end": 2.5, "text": "Első sor"}, {"start": 3.0, "end": 5.0, "text": "Második sor"},
        {"start": 5.5, "end": 7.5, "text": "Harmadik sor"}]


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_the_full_project_comes_later_and_is_there_for_whoever_needs_it(tmp_path, put_file, monkeypatch):
    from app.storage import output_path, read_meta
    from app.tools import video
    fid = put_file(_clip(tmp_path / "talk.mp4"))
    finals: list[str] = []
    monkeypatch.setattr(video, "_start_final", lambda pid, eta: finals.append(pid) or {"id": "stub"})
    seen_ass: list[bool] = []
    real_render = vp.render

    async def spy(*a, **k):
        seen_ass.append(bool(k.get("ass")))
        return await real_render(*a, **k)
    monkeypatch.setattr(vp, "render", spy)

    async def main():
        _reset_gates()
        res = await video.overlay({"file_id": fid, "brand": "none", "final": "background", "subtitles": SUBS,
                                   "layers": [{"at": 1, "dur": 1.5, "type": "source", "text": "Forrás: teszt"}]})
        pid = res["file_id"]
        meta = read_meta(pid)
        assert meta["full_project"] == "pending" and meta["full_build"]["input"]
        assert _captions_in(output_path(pid)) == 0                         # a kérés útjában felirat nélkül
        assert video.PREVIEW_ECPROJ in meta["assets"]
        assert seen_ass == [True]                                           # az előnézeten a libass rajzolta
        assert any("3 subtitle(s)" in v for v in res["check"]["verified"])  # a feliratról szóló sor nem veszett el
        assert finals == [pid]
        # a szerkesztő (/files/) a teljeset kapja: most épül meg, egyszer
        from app.routes import files as files_route
        from app.storage.files import sign_file_id
        tok, _ = sign_file_id(pid)
        built = []
        real_build = video._build

        async def count(*a, **k):
            built.append(k.get("out_name"))
            return await real_build(*a, **k)
        monkeypatch.setattr(video, "_build", count)
        from app import tenancy
        await asyncio.gather(files_route.get_file(pid, t=tok, u=tenancy.ADMIN_ID), video.ensure_full(pid))
        assert built == ["full.ecproj"]                                     # a két hívó egy építésen osztozik
        assert _captions_in(output_path(pid)) == 3
        meta = read_meta(pid)
        assert meta["full_project"] == "ready" and not meta.get("full_build")
        assert not output_path(pid).with_name(output_path(pid).name + ".full-tmp").exists()
        # egy szerkesztés: újra felirat nélkül a kérésben, a render (újrarenderelés) előbb teljessé teszi
        ed = await video.edit(pid, {"layers": [{"id": "L1", "text": "Forrás: második"}]}, final="background")
        e_pid = ed["file_id"]
        assert video.full_pending(e_pid) and _captions_in(output_path(e_pid)) == 0
        out = await video.render(e_pid, draft=True)
        assert out["file_id"] and _captions_in(output_path(e_pid)) == 3 and not video.full_pending(e_pid)
        return pid
    asyncio.run(main())


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_the_final_job_builds_the_full_project_first(tmp_path, put_file):
    from app import jobs
    from app.storage import output_path, read_meta
    from app.tools import video
    fid = put_file(_clip(tmp_path / "talk.mp4", secs=4))

    async def main():
        _reset_gates()
        res = await video.overlay({"file_id": fid, "brand": "none", "final": "background", "subtitles": SUBS[:1],
                                   "layers": [{"at": 1, "dur": 1.5, "type": "source", "text": "Forrás: teszt"}]})
        jid = res["final"]["job_id"]
        for _ in range(40):
            st = await jobs.wait_status(jid, 15)
            if st["status"] != "running":
                break
        assert st["status"] == "done", st
        assert _captions_in(output_path(res["file_id"])) == 1 and read_meta(res["file_id"])["full_project"] == "ready"
    asyncio.run(main())


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_short_and_wait_keep_the_full_build_in_the_request(tmp_path, put_file):
    from app.storage import output_path, read_meta
    from app.tools import video
    fid = put_file(_clip(tmp_path / "short.mp4", secs=3))

    async def main():
        _reset_gates()
        for body in ({}, {"final": "wait"}, {"draft": True}):
            res = await video.overlay({"file_id": fid, "brand": "none", "subtitles": SUBS[:1], **body,
                                       "layers": [{"at": 0.5, "dur": 1, "type": "source", "text": "Forrás: teszt"}]})
            meta = read_meta(res["file_id"])
            assert "full_project" not in meta and _captions_in(output_path(res["file_id"])) == 1, body
            assert video.PREVIEW_ECPROJ not in (meta.get("assets") or {})
    asyncio.run(main())
