"""Élő előnézet, L2 (app/tools/live_layers.py): a grafikai rétegek klipjeinek hash-e (időbeli eltolás: ugyanaz; szöveg,
hossz, kézzel átidőzített kulcs, kép tartalma: más), a bejegyzések (z-sorrend, végig látható réteg: kör + kivezetés /
darabok), a klip formátuma (W × 2H, fölül egyenes alfájú szín, alul az alfa luma-ként, BT.709 tv, GOP ≤ 1 s), az aláírt
végpont (Range), a nyilvántartás (Postgres, LRU) — és ha a motor megvan, egy valódi overlay-projekten is."""
from __future__ import annotations

import asyncio
import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.tools import live_layers as ll

FPS = 30.0
T = ll.TPS


def _tk(frames: float) -> int:
    return round(frames / FPS * T)


def _text_comp(text: str, n: int, key_at: int = 10) -> dict:
    """Egy réteg saját kompozíciója: egy szövegréteg, egy belső kulcs (a saját idejében)."""
    return {"type": "Comp", "width": 600, "height": 200, "frame_rate": {"num": 30, "den": 1}, "duration": _tk(n),
            "background": [0, 0, 0], "layers": [
                {"id": 900, "name": "Text", "source": {"type": "Text"}, "label": "Red", "comment": "", "start_time": 0,
                 "in_point": 0, "out_point": _tk(n), "switches": {"video": True}, "blend_mode": "Normal", "track_matte": None,
                 "parent": None, "preserve_transparency": False,
                 "props": {"uid": 901, "children": [
                     {"node": "Prop", "uid": 902, "match": "sourceText", "value": {"t": "Text", "v": {"text": text, "size": 40}}},
                     {"node": "Prop", "uid": 903, "match": "opacity", "value": {"t": "Scalar", "v": 100},
                      "keys": [{"time": 0, "value": {"t": "Scalar", "v": 0}}, {"time": _tk(key_at), "value": {"t": "Scalar", "v": 100}}]}]}}]}


def _main_layer(lid: str, item: int, start: float, n: int, jitter: int = 0, lid_uid: int = 0) -> dict:
    """A fő kompozíció rétegsora: a builder be- / kiúsztatása (0, 13, n − 11, n kocka), jitter: a motor kockára
    kerekítése egy kulccsal odébb."""
    keys = [0, 13 + jitter, n - 11, n]
    return {"id": 1000 + lid_uid, "name": f"Layer {lid} – x", "source": {"type": "Comp", "item": item}, "label": "Sandstone",
            "comment": "", "start_time": _tk(start), "in_point": _tk(start), "out_point": _tk(start + n),
            "switches": {"video": True, "adjustment": False}, "blend_mode": "Normal", "track_matte": None, "parent": None,
            "preserve_transparency": False, "markers": [],
            "props": {"uid": 2000 + lid_uid, "children": [
                {"node": "Prop", "uid": 3000 + lid_uid, "match": "position", "value": {"t": "Vec3", "v": [300, 900, 0]}},
                {"node": "Prop", "uid": 4000 + lid_uid, "match": "opacity", "value": {"t": "Scalar", "v": 100},
                 "keys": [{"time": _tk(k), "value": {"t": "Scalar", "v": v}} for k, v in zip(keys, (0, 100, 100, 0))]}]}}


def _project(layers: list[tuple], total: int = 3000, image: str | None = None) -> dict:
    """layers: (id, start kocka, hossz, szöveg[, jitter]) — alulról fölfelé; + a felvétel legalul."""
    items: dict = {}
    main = []
    for k, ly in enumerate(layers):
        lid, start, n, text = ly[:4]
        items[str(10 + k)] = {"id": 10 + k, "name": f"Layer {lid} – x", "label": "Aqua", "kind": _text_comp(text, n)}
        main.insert(0, _main_layer(lid, 10 + k, start, n, ly[4] if len(ly) > 4 else 0, k))
    if image:
        items["50"] = {"id": 50, "name": image, "kind": {"type": "Footage", "path": image, "has_video": True}}
        items["10"]["kind"]["layers"].append({"id": 951, "name": "Image", "source": {"type": "Footage", "item": 50},
                                              "start_time": 0, "in_point": 0, "out_point": _tk(90), "switches": {"video": True},
                                              "blend_mode": "Normal", "track_matte": None, "parent": None, "props": {}})
    items["2"] = {"id": 2, "name": "footage.mp4", "kind": {"type": "Footage", "path": "footage.mp4"}}
    main.append({"id": 5000, "name": "Footage", "source": {"type": "Footage", "item": 2}, "start_time": 0, "in_point": 0,
                 "out_point": _tk(total), "switches": {"video": True}, "blend_mode": "Normal", "track_matte": None,
                 "parent": None, "props": {}})
    items["1"] = {"id": 1, "name": "Video", "kind": {"type": "Comp", "width": 1920, "height": 1080, "frame_rate": {"num": 30, "den": 1},
                                                     "duration": _tk(total), "background": [0.05, 0.06, 0.08], "layers": main}}
    return {"schema": 2, "settings": {"bit_depth": "Bpc8"}, "items": items}


def _sc(d: dict, assets: dict | None = None) -> dict:
    return ll.scene_from(d, proxy=(960, 540), assets=assets or {})


def _by_id(sc: dict) -> dict:
    return {e["id"]: e for e in sc["layers"]}


# ---------- a hash ----------

def test_a_time_move_keeps_the_hash_even_with_the_engines_frame_rounding():
    a = _by_id(_sc(_project([("L1", 100, 90, "Első"), ("L2", 400, 120, "Második")])))
    # L2 egy nem egész kockányi eltolás után: a motor a beúszás kulcsát egy kockával odébb kerekítette
    b = _by_id(_sc(_project([("L1", 100, 90, "Első"), ("L2", 1237, 120, "Második", 1)])))
    assert a["L2"]["hash"] == b["L2"]["hash"] and a["L1"]["hash"] == b["L1"]["hash"]
    assert (a["L2"]["Fa"], b["L2"]["Fa"]) == (400, 1237) and a["L2"]["N"] == b["L2"]["N"] == 120


def test_a_text_change_changes_only_that_layer():
    a = _by_id(_sc(_project([("L1", 100, 90, "Első"), ("L2", 400, 120, "Második")])))
    b = _by_id(_sc(_project([("L1", 100, 90, "Első"), ("L2", 400, 120, "Második (javítva)")])))
    assert a["L1"]["hash"] == b["L1"]["hash"] and a["L2"]["hash"] != b["L2"]["hash"]


def test_a_retime_or_a_hand_moved_key_changes_the_hash():
    a = _by_id(_sc(_project([("L2", 400, 120, "Második")])))
    longer = _by_id(_sc(_project([("L2", 400, 150, "Második")])))
    hand = _by_id(_sc(_project([("L2", 400, 120, "Második", 6)])))          # 6 kocka: nem kerekítés, hanem szándék
    assert len({a["L2"]["hash"], longer["L2"]["hash"], hand["L2"]["hash"]}) == 3


def test_the_ids_labels_and_the_order_of_other_layers_do_not_count():
    a = _by_id(_sc(_project([("L1", 100, 90, "Első"), ("L2", 400, 120, "Második")])))
    d = _project([("L1", 100, 90, "Első"), ("L2", 400, 120, "Második")])
    for it in d["items"].values():                                    # új azonosítók, más színcímkék (egy újraépítés után)
        it["label"] = "Violet"
    b = _by_id(_sc(d))
    assert a["L1"]["hash"] == b["L1"]["hash"] and a["L2"]["hash"] == b["L2"]["hash"]


def test_the_picture_counts_by_its_content_not_its_file_id(put_file, tmp_path):
    p1, p2 = tmp_path / "a.png", tmp_path / "b.png"
    p1.write_bytes(b"\x89PNG one")
    p2.write_bytes(b"\x89PNG two")
    f1, f1b, f2 = put_file(p1), put_file(p1), put_file(p2)        # f1b: ugyanaz a tartalom, új azonosító (mint egy szerkesztés)
    d = _project([("L1", 100, 90, "Első")], image="gfx-01-L1.png")
    h = lambda fid: _by_id(_sc(d, {"gfx-01-L1.png": fid}))["L1"]["hash"]   # noqa: E731
    assert h(f1) == h(f1b) != h(f2)


def test_unsupported_layers_are_named():
    d = _project([("L1", 100, 90, "Első"), ("L2", 400, 120, "Második")])
    d["items"]["1"]["kind"]["layers"][0]["blend_mode"] = "Screen"
    e = _by_id(_sc(d))
    assert "blend mode" in (e["L2"]["unsupported"] or "") and e["L1"]["unsupported"] is None


# ---------- a bejegyzések ----------

def test_entries_are_bottom_to_top_and_the_persistent_layer_loops():
    d = _project([("L1", 105, 2800, "TV"), ("L2", 400, 120, "Második")], total=3000)
    sc = _sc(d)
    assert [e["id"] for e in sc["layers"]] == ["L1", "L2"] and [e["z"] for e in sc["layers"]] == [0, 1]
    assert sc["layers"][0]["persist"] and not sc["layers"][1]["persist"]
    xs = ll.entries(sc, {})
    assert [(x["id"], x["kind"]) for x in xs] == [("L1", "loop"), ("L1", "outro"), ("L2", "clip")]
    loop, outro = xs[0], xs[1]
    assert loop["f0"] == 105 and loop["f1"] == outro["f0"] == 105 + 2800 - 30 and outro["f1"] == 105 + 2800
    assert loop["loop"] is None                                              # a kör még nem mért
    known = ll.entries(sc, {loop["hash"]: {"intro": 90, "period": 1}})
    assert known[0]["loop"] == {"intro": 3.0, "period": round(1 / 30, 6), "intro_frames": 90, "period_frames": 1, "still": True}
    parts = [x for x in ll.entries(sc, {loop["hash"]: {"intro": 0, "period": 0}}) if x["id"] == "L1"]
    assert [x["kind"] for x in parts] == ["part"] * 2 + ["outro"]          # nem ismétlődik: 60 s-os darabok
    assert parts[0]["f0"] == 105 and parts[0]["f1"] == parts[1]["f0"] and parts[1]["f1"] == parts[2]["f0"]
    # a kör és a kivezetés hash-e is független a réteg kezdetétől
    moved = ll.entries(_sc(_project([("L1", 205, 2700 + 100, "TV"), ("L2", 400, 120, "Második")], total=3200)), {})
    assert moved[1]["hash"] == outro["hash"] and moved[0]["hash"] == loop["hash"]


def test_the_priority_follows_the_playhead():
    x = {"f0": 300, "f1": 390}
    assert ll._prio(x, 11.0, FPS) == 0.0 and ll._prio(x, 5.0, FPS) == pytest.approx(5.0)
    assert ll._prio(x, 20.0, FPS) == pytest.approx(3 * 7.0)               # mögötte: háromszoros távolság


# ---------- a klip formátuma (ffmpeg, motor nélkül) ----------

def _ff() -> bool:
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def _straight_prores(path: Path, n: int = 45) -> None:
    """Átlátszó ProRes 4444, egyenes alfával: a bal fele fehér (Y = 235), az alfája kockánként csökken; a jobb fele
    átlátszó."""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=white:s=64x36:r=30:d={n / 30}",
                    "-vf", "format=yuva444p,geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='if(lt(X,32),255-N*5,0)'",
                    "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", str(path)], check=True)


def _luma(p: Path, w: int, h: int):
    """A klip Y-síkja kockánként, ahogy kódolva van (yuv420p, 16–235 — a gray-re alakítás kiterjesztené a tartományt)."""
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
                         capture_output=True, check=True).stdout
    fr = np.frombuffer(raw, np.uint8).reshape(-1, w * h * 3 // 2)
    return fr[:, :w * h].reshape(-1, h, w).astype(float)


@pytest.mark.skipif(not _ff(), reason="ffmpeg needed")
def test_the_stacked_clip_format(tmp_path):
    import numpy as np
    src, dst = tmp_path / "in.mov", tmp_path / "out.mp4"
    _straight_prores(src)
    subprocess.run(["ffmpeg", "-y", "-v", "error", *ll.stack_args(str(src), str(dst), "30/1")], check=True)
    st = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                    "stream=width,height,pix_fmt,codec_name,r_frame_rate,nb_frames,color_space,color_range,"
                                    "color_transfer,color_primaries,start_time", "-of", "json", str(dst)],
                                   capture_output=True, text=True, check=True).stdout)["streams"][0]
    assert (st["codec_name"], st["width"], st["height"], st["pix_fmt"]) == ("h264", 64, 72, "yuv420p")
    assert (st["color_space"], st["color_primaries"], st["color_transfer"], st["color_range"]) == ("bt709", "bt709", "bt709", "tv")
    assert st["r_frame_rate"] == "30/1" and int(st["nb_frames"]) == 45 and float(st["start_time"]) == 0.0
    keys = [int(f["key_frame"]) for f in json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                                                                    "frame=key_frame", "-of", "json", str(dst)],
                                                                   capture_output=True, text=True, check=True).stdout)["frames"]]
    kf = [i for i, k in enumerate(keys) if k]
    assert kf[0] == 0 and max(b - a for a, b in zip(kf, kf[1:] + [len(keys)])) <= 30          # GOP ≤ 1 s
    Y = _luma(dst, 64, 72)
    for n in (0, 20, 40):
        a = max(0, 255 - n * 5) / 255
        top, bot = Y[n, :36], Y[n, 36:]
        assert abs(bot[4:32, 4:28].mean() - (16 + 219 * a)) < 3, n           # az alfa luma-ként (16–235)
        assert abs(bot[4:32, 36:60].mean() - 16) < 2                          # átlátszó
        assert abs(top[4:32, 4:28].mean() - 235) < 3, n                       # egyenes alfa: a szín nem halványul vele


# ---------- az aláírt végpont, a nyilvántartás ----------

def _put_project(put_file, tmp_path, d: dict) -> str:
    """Egy overlay-verzió: a meta spec-je + az előnézet-projekt (a fenti szintetikus JSON) a médiafájljai között."""
    from app.storage import new_file_id, output_path, write_meta
    pj = tmp_path / "preview.ecproj"
    pj.write_text(json.dumps(d), "utf-8")
    prev = put_file(pj)
    pid = new_file_id("ecproj")
    output_path(pid).write_text(json.dumps(d), "utf-8")
    write_meta(pid, operation="video overlay", extra={
        "spec": {"kind": "overlay", "base": {"file_id": "src.mov", "width": 1920, "height": 1080, "fps": 30, "duration": 100,
                                             "proxy": {"w": 960, "h": 540}}, "story": {"layers": []}, "opts": {}},
        "assets": {"preview.ecproj": prev}})
    return pid


def test_layers_part_signs_ready_clips_and_the_route_serves_ranges(put_file, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.tools import preview_serve as ps
    ll._reset()
    pid = _put_project(put_file, tmp_path, _project([("L1", 100, 90, "Első"), ("L2", 400, 120, "Második")]))
    sc = ll.scene(pid)
    ready = ll.clip_path(_by_id(sc)["L2"]["hash"])
    ready.parent.mkdir(parents=True, exist_ok=True)
    ready.write_bytes(bytes(range(256)) * 40)                               # (egy „kész” klip: a végpont csak kiszolgál)

    async def part():
        return await ll.layers_part(pid, enqueue=False)
    xs = {x["id"]: x for x in asyncio.run(part())}
    assert xs["L2"]["status"] == "ready" and xs["L1"]["status"] == "missing" and xs["L1"]["url"] is None
    assert (xs["L2"]["t0"], xs["L2"]["t1"], xs["L2"]["f0"], xs["L2"]["frames"]) == (round(400 / 30, 6), round(520 / 30, 6), 400, 120)
    assert xs["L1"]["z"] < xs["L2"]["z"]
    url = xs["L2"]["url"]
    assert url.startswith("http://testserver/api/video/live/layer/") and f"p={pid}" in url
    c = TestClient(app)
    path = url.split("testserver", 1)[1]
    r = c.get(path, headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == (bytes(range(256)) * 40)[10:20] and r.headers["content-type"] == "video/mp4"
    assert "immutable" in r.headers["cache-control"]
    assert c.get(path.replace("t=", "t=1")).status_code == 403
    q = ps.signed(pid)
    r = c.get(f"/api/video/{pid}/live/layers.json{q}")
    assert r.status_code == 200 and {x["id"] for x in r.json()["layers"]} == {"L1", "L2"}
    assert c.get(f"/api/video/{pid}/live/layers.json?t=1.bad").status_code == 403
    ll._reset()


def test_the_clips_are_registered_in_postgres_and_swept(pg_url, monkeypatch):
    from app import db
    from app.config import settings
    from app.tools import preview_cache as pc
    settings.DATABASE_URL = pg_url

    async def main():
        await db.start()
        try:
            assert db.enabled()
            h, old = "ab" * 32, "cd" * 32
            for x in (old, h):
                p = ll.clip_path(x)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"x" * 1000)
                await pc.register("v-" + x, "_layers", 1000)
            await db._rows("UPDATE preview_segments SET last_used_at = now() - interval '1 hour' WHERE hash = %s", ("v-" + old,))
            assert await pc.ready(["v-" + h, "v-" + old], "_layers") == {"v-" + h, "v-" + old}
            monkeypatch.setenv("VIDEO_PREVIEW_CACHE_GB", str(1500 / 1024 ** 3))
            await pc.sweep()                                                   # LRU: a régebben használt megy
            assert not ll.clip_path(old).exists() and ll.clip_path(h).exists()
            assert await pc.ready(["v-" + h, "v-" + old], "_layers") == {"v-" + h}
        finally:
            await db.preview_seg_delete(["v-" + h, "v-" + old])              # (a munkamenet közös adatbázisa: tisztán hagyjuk)
            ll.clip_path(h).unlink(missing_ok=True)
            await db.stop()
            settings.DATABASE_URL = ""
    asyncio.run(main())


def test_no_sqlite():
    assert "sqlite" not in Path(ll.__file__).read_text("utf-8").lower()


# ---------- a motorral, egy valódi overlay-projekten ----------

def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and _ff()


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_on_a_real_overlay_a_move_reuses_and_a_text_edit_renders_only_its_layer(tmp_path, put_file, monkeypatch):
    import numpy as np
    from app.tools import video
    ll._reset()
    video._gate = video._bgate = None
    monkeypatch.setattr(video, "_start_final", lambda pid, eta: {"id": "stub"})
    src = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=48000", "-t", "40", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(src)], check=True)
    fid = put_file(src)
    layers = [{"at": 1.0, "until": 39.5, "type": "graphic", "anim": "fade", "backdrop": 0, "elements": [
                  {"shape": "text", "text": "TV", "x": 0.095, "y": 0.06, "size": 34, "weight": "bold", "color": "white"}]},
              {"at": 4, "dur": 3, "type": "source", "text": "Forrás: teszt", "position": "bottom"},
              {"at": 20, "dur": 3, "type": "graphic", "anim": {"in": "fade", "out": "fade"}, "backdrop": 0, "elements": [
                  {"shape": "text", "text": "FADE", "x": 0.5, "y": 0.5, "size": 120, "weight": "bold", "color": "white"}]}]

    async def main():
        res = await video.overlay({"file_id": fid, "brand": "none", "layers": layers, "render": False})
        pid = res["file_id"]
        sc = ll.scene(pid)
        e = {x["id"]: x for x in sc["layers"]}
        assert set(e) == {"L1", "L2", "L3"} and e["L1"]["persist"] and sc["pw"] == 960
        mv = await video.edit(pid, {"layers": [{"id": "L2", "at": 11.37}]}, render=False)     # nem egész kockányi eltolás
        txt = await video.edit(pid, {"layers": [{"id": "L2", "text": "Forrás: második"}]}, render=False)
        m, t = {x["id"]: x for x in ll.scene(mv["file_id"])["layers"]}, {x["id"]: x for x in ll.scene(txt["file_id"])["layers"]}
        assert m["L2"]["Fa"] != e["L2"]["Fa"] and all(m[k]["hash"] == e[k]["hash"] for k in e)
        assert t["L2"]["hash"] != e["L2"]["hash"] and t["L1"]["hash"] == e["L1"]["hash"] and t["L3"]["hash"] == e["L3"]["hash"]
        # a kész bejegyzések: a vízjel köre + kivezetése, a két rövid réteg
        xs = await ll.layers_part(pid, playhead=20.0)
        for x in xs:
            await ll.clip(pid, x["hash"], timeout=300)
        xs = await ll.layers_part(pid, enqueue=False)
        assert all(x["status"] == "ready" and x["url"] for x in xs), xs
        assert ll.stats()["renders"] == 4
        loop = next(x for x in xs if x["kind"] == "loop")
        assert loop["loop"] and loop["loop"]["period_frames"] >= 1
        # az eltolt verzió: semmi sem készül újra
        xs2 = await ll.layers_part(mv["file_id"], playhead=11.0)
        assert all(x["status"] == "ready" for x in xs2) and ll.stats()["renders"] == 4
        # L3 klipje: 960 × 1080, a kockaszám a rétegé; a kiúszás alatt a szín nem halványul (egyenes alfa)
        l3 = next(x for x in xs if x["id"] == "L3")
        p = ll.clip_path(l3["hash"])
        Y = _luma(p, 960, 1080)
        assert Y.shape[0] == l3["frames"]
        mid, last = Y[len(Y) // 2], Y[-3]
        a_mid, a_last = (mid[540:] - 16) / 219, (last[540:] - 16) / 219
        sel = (a_mid > 0.97) & (mid[:540] > 200)
        assert sel.sum() > 200
        assert a_last[sel].mean() < 0.8 and abs(last[:540][sel].mean() - mid[:540][sel].mean()) < 6
    asyncio.run(main())
    ll._reset()
