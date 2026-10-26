"""A végleges render ütemezése egy szerkesztés után (N1, 10-26 — S4-en öt egymás utáni szerkesztés válasza 2,1 → 5,4 s
volt, a terhelés 7 → 31, mert minden szerkesztés azonnal indította a saját végleges renderjének első lépését, a régi
változatoké pedig futott tovább):
- a végleges render csendre vár (VIDEO_FINAL_QUIET_S), addig semmit sem csinál, és a becslés a várakozással együtt;
- az új változat (és már a szerkesztés eleje) leváltja az ősök végleges renderjét — a futó munka leáll, a régi job
  „superseded”, és megmondja, melyik job folytatja; egy elbukott szerkesztés után a szülőé újraindul;
- a testvérek (A/B-változatok, egy másik ág) nem váltják le egymást;
- a végleges rendernek nem kell a színes munkamásolat: elég egy kész másolat a verzió idővonalával (_copy_plan) —
  színváltozásnál semmi sem készül, vágás + színnél egy smart cut a meglévőből, és a becslés is ehhez igazodik.
Motor nélkül: a végleges render lépései és a munkamásolat-készítés helyén csonkok.
"""
from __future__ import annotations

import asyncio
import json

import pytest


def _version(src: str | None, spec: dict | None = None, **extra) -> str:
    from app.storage import new_file_id, output_path, write_meta
    pid = new_file_id("ecproj")
    output_path(pid).write_text("{}", "utf-8")
    write_meta(pid, label="v.ecproj", operation="video edit", source_file_id=src,
               extra={**({"spec": spec} if spec else {}), **extra})
    return pid


@pytest.fixture()
def stub_final(monkeypatch):
    """A végleges render két lépése csonk: a prepare / render hívásait rögzíti, a render egy eseményig vár (leállítható)."""
    from app.tools import video
    calls: list[tuple[str, str]] = []
    gate = {"ev": None}

    async def prepare(pid):
        calls.append(("prepare", pid))

    async def render(pid, progress=None, **kw):
        calls.append(("render", pid))
        try:
            await gate["ev"].wait()
        except asyncio.CancelledError:
            calls.append(("killed", pid))
            raise
        return {"file_id": f"{pid}.mp4"}
    monkeypatch.setattr(video, "_final_prepare", prepare)
    monkeypatch.setattr(video, "render", render)
    return calls, gate


async def _until(cond, secs: float = 3.0) -> None:
    for _ in range(int(secs / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


def test_a_newer_version_supersedes_its_ancestors_final_not_its_siblings(stub_final, monkeypatch):
    from app import jobs
    from app.tools import video
    calls, gate = stub_final
    monkeypatch.setenv("VIDEO_FINAL_QUIET_S", "0")
    a = _version(None)
    b = _version(a)
    c = _version(b)
    b2 = _version(a)                                             # egy másik ág A-ból

    async def main():
        gate["ev"] = asyncio.Event()
        ja = video._start_final(a, 100.0)
        await _until(lambda: ("render", a) in calls)           # A végleges renderje fut
        jb2 = video._start_final(b2, 100.0)                    # a testvér-ág: A-t leváltja (az őse), egymást nem
        await _until(lambda: ("killed", a) in calls)
        st = jobs._JOBS[ja["id"]]
        assert st["status"] == "superseded" and st["superseded_by"] == b2 and st["next_job"] == jb2["id"]
        jc = video._start_final(c, 100.0)                      # C (B gyereke, A unokája): B2-t nem váltja le
        await _until(lambda: ("render", c) in calls)
        assert jobs._JOBS[jb2["id"]]["status"] == "running"
        tok = video._KEEP_FINALS.set(True)                     # A/B-változatok: senkit sem váltanak le
        try:
            jd = video._start_final(_version(c), 100.0)
        finally:
            video._KEEP_FINALS.reset(tok)
        await asyncio.sleep(0.05)
        assert jobs._JOBS[jc["id"]]["status"] == "running"
        gate["ev"].set()
        for j in (jb2, jc, jd):
            await _until(lambda j=j: jobs._JOBS[j["id"]]["status"] == "done")
        public = await jobs.job_status(ja["id"])                # a régi job is megmondja, hova lett a munkája
        assert public["status"] == "superseded" and public["next_job"] == jb2["id"] and "newer version" in public["error"]
    asyncio.run(main())


def test_the_final_waits_for_a_pause_and_a_superseded_wait_costs_nothing(stub_final, monkeypatch):
    from app import jobs
    from app.tools import video
    calls, gate = stub_final
    monkeypatch.setenv("VIDEO_FINAL_QUIET_S", "0.4")
    a = _version(None)
    b = _version(a)

    async def main():
        gate["ev"] = asyncio.Event()
        ja = video._start_final(a, 100.0)
        assert ja["eta_s"] == 100 and ja["quiet_s"] == pytest.approx(0.4)   # (a becslés a várakozással: 100 + 0,4 → 100)
        await asyncio.sleep(0.15)
        st = await jobs.job_status(ja["id"])
        assert st["status"] == "running" and st["stage"].startswith("waiting for a pause in editing") and not calls
        jb = video._start_final(b, 100.0)                      # újabb szerkesztés a várakozás alatt: A semmit sem csinált
        await _until(lambda: jobs._JOBS[ja["id"]]["status"] == "superseded")
        await asyncio.sleep(0.5)
        assert ("prepare", a) not in calls and ("render", a) not in calls
        await _until(lambda: ("render", b) in calls)          # B a saját csendje után indul: előbb az első lépés
        assert calls.index(("prepare", b)) < calls.index(("render", b))
        gate["ev"].set()
        await _until(lambda: jobs._JOBS[jb["id"]]["status"] == "done")
    asyncio.run(main())


def test_the_quiet_wait_is_in_the_eta(stub_final, monkeypatch):
    from app import jobs
    from app.tools import video
    monkeypatch.setenv("VIDEO_FINAL_QUIET_S", "30")

    async def main():
        j = video._start_final(_version(None), 600.0)
        assert j["eta_s"] == 630 and 625 <= j["remaining_s"] <= 630
        jobs.cancel(j["id"])
        await _until(lambda: jobs._JOBS[j["id"]]["status"] == "cancelled")
    asyncio.run(main())


def test_an_edit_stops_the_parents_final_at_once_and_restarts_it_if_the_edit_fails(stub_final, monkeypatch):
    from app import jobs
    from app.tools import video
    calls, gate = stub_final
    monkeypatch.setenv("VIDEO_FINAL_QUIET_S", "0")
    p = _version(None)                                          # spec nélkül: az edit ValueError-ral áll meg

    async def main():
        gate["ev"] = asyncio.Event()
        j = video._start_final(p, 50.0)
        await _until(lambda: ("render", p) in calls)
        with pytest.raises(ValueError):
            await video.edit(p, {"layers": [{"id": "L1", "text": "x"}]})
        await _until(lambda: ("killed", p) in calls)          # (a futó munka leállt)
        old = jobs._JOBS[j["id"]]
        assert ("killed", p) in calls and old["status"] == "superseded" and old["next_job"]
        new = jobs._JOBS[old["next_job"]]
        assert new["project"] == p and new["status"] == "running"
        await _until(lambda: calls.count(("render", p)) == 2)  # újra fut
        gate["ev"].set()
        await _until(lambda: new["status"] == "done")
    asyncio.run(main())


def test_final_status_tells_a_superseded_final(monkeypatch):
    from app import jobs
    from app.tools import preview_serve
    pid = _version(None)

    async def main():
        async def work(progress):
            await asyncio.sleep(10)
        j = jobs.start("render", work, eta_s=10, final=True, project=pid)
        await asyncio.sleep(0)
        jobs.cancel(j["id"], status="superseded", error="Replaced", superseded_by="x.ecproj", next_job="abc")
        await _until(lambda: jobs._JOBS[j["id"]]["status"] == "superseded")
        st = preview_serve.final_status(pid)
        assert st["status"] == "superseded" and st["superseded_by"] == "x.ecproj" and st["next_job"] == "abc"
    asyncio.run(main())


def test_a_job_cancelled_before_it_started_is_settled():
    from app import jobs

    async def main():
        async def work(progress):
            return {}
        j = jobs.start("render", work, final=True, project="p.ecproj")
        assert jobs.cancel(j["id"], status="superseded", error="Replaced")     # még el sem indult
        await asyncio.sleep(0.01)
        assert jobs._JOBS[j["id"]]["status"] == "superseded" and jobs._JOBS[j["id"]]["finished"]
    asyncio.run(main())


# ---------- a végleges rendernek elég az idővonal másolata (nem a színes munkamásolat) ----------

KEEP_A = [[0.0, 100.0], [110.0, 300.0]]
KEEP_B = [[0.0, 100.0], [110.0, 200.0], [210.0, 300.0]]          # egy újabb vágás


def _registry(entries: list[tuple[dict, list | None]]) -> tuple[str, list[str]]:
    """Egy „eredeti” felvétel és a kész munkamásolatai (proxies) → (file_id, a másolatok media_id-jai, sorban)."""
    from app.storage import new_file_id, output_path, write_meta
    from app.tools import footage
    fid0 = new_file_id("mov")
    output_path(fid0).write_bytes(b"orig")
    prox, ids = {}, []
    for adjust, keep in entries:
        mid = new_file_id("mp4")
        output_path(mid).write_bytes(b"copy")
        prox[footage._key({"adjust": adjust, "keep": keep})] = {"media_id": mid, "asset": f"footage-{mid[:8]}.mp4",
                                                               "proxy": {"w": 960, "h": 540}}
        ids.append(mid)
    write_meta(fid0, extra={"proxies": prox})
    return fid0, ids


def _pending_base(fid0: str, stand: str, adjust: dict, keep: list | None, card: bool = False) -> dict:
    """Egy függő verzió spec.base-e (mint a _proxy_later után): a recept a verzióé, a helyén a meglévő másolat."""
    b = {"file_id": fid0, "adjust": adjust, "keep": keep, "width": 1920, "height": 1080, "fps": 30.0, "duration": 290.0,
         "audio": True, "media_id": stand, "asset": f"footage-{stand[:8]}.mp4", "proxy": {"w": 960, "h": 540}, "hdr": "hlg",
         "proxy_pending": True}
    if card:
        b.update(duration=295.0, end_card={"title": "T", "dur": 5.0},
                 plain={"asset": b["asset"], "media_id": stand, "duration": 290.0})
    return b


def test_copy_plan_a_colour_change_needs_no_new_copy():
    from app.tools import video
    old_colour, new_colour = {"brightness": 0.05}, {"brightness": 0.03, "saturation": 1.25}
    fid0, (neutral, parent) = _registry([({}, KEEP_A), (old_colour, KEEP_A)])
    base = _pending_base(fid0, parent, new_colour, KEEP_A, card=True)
    kind, (kd, hit) = video._copy_plan(base)
    assert kind == "same" and hit["media_id"] == parent           # a helyén álló másolat elöl (a semleges is jó lenne)
    recipe = video._copy_recipe(base)
    assert recipe["adjust"] == new_colour and recipe["keep"] == KEEP_A and "media_id" not in recipe and "end_card" not in recipe
    assert recipe["duration"] == 290.0                            # (a zárókártya nélkül, mint a _proxy_later receptje)


def test_copy_plan_cut_and_colour_is_a_smart_cut_in_the_stand_ins_colour(monkeypatch):
    from app.tools import footage, video
    old_colour, new_colour = {"brightness": 0.05}, {"brightness": 0.03}
    fid0, (parent,) = _registry([(old_colour, KEEP_A)])
    monkeypatch.setattr(footage, "donors", lambda b: [(KEEP_A, {"media_id": parent})] if b.get("adjust") == old_colour else [])
    base = _pending_base(fid0, parent, new_colour, KEEP_B)
    kind, tl = video._copy_plan(base)
    assert kind == "smart" and tl["adjust"] == old_colour and tl["keep"] == KEEP_B
    monkeypatch.setattr(footage, "donors", lambda b: [])            # nincs miből vágni: a verzió saját másolata (teljes út)
    assert video._copy_plan(base)[0] == "own"


def test_copy_plan_cut_only_makes_the_versions_own_copy_and_a_made_one_is_used():
    from app.tools import footage, video
    colour = {"brightness": 0.05}
    fid0, (parent,) = _registry([(colour, KEEP_A)])
    base = _pending_base(fid0, parent, colour, KEEP_B)
    kind, recipe = video._copy_plan(base)
    assert kind == "own" and recipe["keep"] == KEEP_B                 # ugyanaz a szín: a saját másolat (smart cut)
    fid1, _ = _registry([(colour, KEEP_B)])                           # ha már elkészült: azt kapja
    assert video._copy_plan({**base, "file_id": fid1})[0] == "cached"
    assert video._copy_plan({**base, "proxy_pending": False}) == ("none", None)
    assert footage.timeline_id(KEEP_B) != footage.timeline_id(KEEP_A) and footage.timeline_id(None) is None


def test_the_final_builds_with_the_existing_copy_and_the_editor_gets_its_own_later(monkeypatch):
    """ensure_full(copy="timeline") — a végleges render első lépése — színváltozás után: a teljes projekt a helyén álló
    másolattal épül, a színes munkamásolat NEM készül (a verzió függő marad); a szerkesztő (copy=True) kéri: akkor készül,
    és a projektben a helyére kerül."""
    from app.storage import new_file_id, output_path, read_meta
    from app.tools import footage, video
    fid0, (parent,) = _registry([({"brightness": 0.05}, KEEP_A)])
    base = _pending_base(fid0, parent, {"brightness": 0.03}, KEEP_A)
    D = {"W": 1920, "H": 1080, "base": {"file": base["asset"], "scale": 2.0}, "captions": [{"t": 1}]}
    inp = new_file_id("json")
    output_path(inp).write_text(json.dumps(D), "utf-8")
    pid = _version(None, {"kind": "overlay", "base": base}, full_project="pending",
                   full_build={"input": inp, "lotties": []}, assets={base["asset"]: parent})
    built, made, swapped = [], [], []

    async def fake_build(D_, work, lotties, out_name="project.ecproj", gate=None):
        built.append(D_["base"]["file"])
        (work / out_name).write_text(json.dumps({"built": D_["base"]["file"]}), "utf-8")
        return "{}"

    async def fake_finish(pid_, spec, fb):
        made.append(fb.get("proxy_base"))
        b = {k: v for k, v in spec["base"].items() if k != "proxy_pending"}
        b.update(media_id="own.mp4", asset="footage-own.mp4")
        sp = {**spec, "base": b}
        video.write_meta(pid_, extra={"spec": sp})
        return sp

    async def fake_swap(project, comp, asset, file, name, start=None):
        swapped.append((asset, file))
        (project.parent / f"{name}.ecproj").write_text(json.dumps({"swapped": file}), "utf-8")
        return f"{name}.ecproj"
    monkeypatch.setattr(video, "_build", fake_build)
    monkeypatch.setattr(video, "_finish_proxy", fake_finish)
    def fake_stage(p, w):
        (w / "project.ecproj").write_text("{}", "utf-8")
        return w / "project.ecproj"
    monkeypatch.setattr(video, "stage", fake_stage)
    monkeypatch.setattr(video, "_main_comp", lambda p: "Video")
    monkeypatch.setattr(footage, "swap", fake_swap)

    async def main():
        assert await video.ensure_full(pid, background=True, copy="timeline")
        m = read_meta(pid)
        assert built == [base["asset"]] and not made                  # a meglévő másolattal, színes újrakódolás nélkül
        assert m["full_project"] == "ready" and m["full_footage"] == base["asset"] and m["spec"]["base"]["proxy_pending"]
        assert video.copy_pending(pid) and not video.full_pending(pid)
        assert not await video.ensure_full(pid, copy="timeline")       # a végleges rendernek nincs több dolga
        assert await video.ensure_full(pid)                            # a szerkesztő: most a saját másolat
        m = read_meta(pid)
        assert made and made[0]["adjust"] == {"brightness": 0.03} and swapped == [(base["asset"], "footage-own.mp4")]
        assert not m.get("full_footage") and not m["spec"]["base"].get("proxy_pending")
        assert json.loads(output_path(pid).read_text("utf-8")) == {"swapped": "footage-own.mp4"}
    asyncio.run(main())


def test_the_eta_counts_the_copy_the_final_really_makes(monkeypatch):
    from app.tools import footage, render_eta, video
    old_colour = {"brightness": 0.05}
    fid0, (parent,) = _registry([(old_colour, KEEP_A)])
    D = {"captions": [], "captionStyle": "box"}

    async def full_copy(m, length):
        return 2400.0                                                 # (a 39 perces színes újrakódolás)
    monkeypatch.setattr(render_eta, "proxy", full_copy)

    async def main():
        colour = await video._full_build_s(D, _pending_base(fid0, parent, {"brightness": 0.03}, KEEP_A))
        assert colour == 0.0                                          # színváltozás: semmi sem készül
        monkeypatch.setattr(footage, "donors", lambda b: [(KEEP_A, {"media_id": parent})])
        cut = await video._full_build_s(D, _pending_base(fid0, parent, {"brightness": 0.03}, KEEP_B))
        assert cut == pytest.approx(video.SMART_COPY_S[0] + video.SMART_COPY_S[1] * 290.0)   # smart cut, nem 2400 s
        monkeypatch.setattr(footage, "donors", lambda b: [])
        assert await video._full_build_s(D, _pending_base(fid0, parent, {"brightness": 0.03}, KEEP_B)) == 2400.0
    asyncio.run(main())
