"""Effekt-rétegek (app/tools/effect_layers.py, app/tools/fx/): a séma (a regiszterből: villám, tűzgolyó; a horgony), a
kiváltó ideje (after_said / at_speech_end_after → a mondat vége 10 ms-ra a hang energiájából), a rajz determinisztikus
(ugyanarra a bemenetre bájtra ugyanaz — hash), a gyorsítótár kulcsa, és hogy a képre tétel normál keverésű, előre komponált
réteg (az élő lejátszó és a gyors végső render változtatás nélkül viszi) — ha a motor megvan, egy valódi overlay-projekten."""
from __future__ import annotations

import asyncio
import hashlib
import math
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from app.tools import effect_layers as el
from app.tools import fx, video
from app.tools.fx import common


# ---------- a séma ----------

def test_the_registry_has_lightning_and_fireball_with_their_own_schemas():
    assert {"lightning", "fireball"} <= set(fx.names())
    for n in fx.names():
        cls = fx.get(n)
        assert issubclass(cls, common.Effect) and cls.NAME == n and cls.DEFAULT_DUR > 0 and cls.PARAMS
    assert "strands" in fx.get("lightning").PARAMS and "speed" in fx.get("fireball").PARAMS


def test_defaults_bounds_and_the_effects_own_duration():
    notes: list[str] = []
    a, b = video.layers_from([{"type": "effect", "at": 2}, {"type": "effect", "effect": "fireball", "at": 9, "size": 9}], "", notes)
    assert (a["effect"], a["dur"], a["target"], a["strands"], a["intensity"]) == ("lightning", 4.0, "arm", 6, 1.0)
    assert "on" not in a and "position" not in a and "anim" not in a
    assert (b["effect"], b["dur"], b["size"], b["speed"]) == ("fireball", 2.0, 2.5, 1.4)
    c = video.layers_from([{"type": "effect", "at": 1, "strands": 99, "intensity": 0, "target": [0.9, 0.2], "dur": 6,
                            "on": [0.2, 0.5, 0.1, 0.1]}], "", [])[0]
    assert (c["strands"], c["intensity"], c["target"], c["dur"], c["on"]) == (16, 0.3, [0.9, 0.2], 6.0, [0.2, 0.5, 0.1, 0.1])
    assert not [n for n in notes if n.startswith("⚠")]


@pytest.mark.parametrize("bad, words", [
    ({"effect": "laser"}, "lightning | fireball"),
    ({"effect": "flame_jet"}, "planned"),
    ({"target": "sideways"}, "arm (the forearm"),
    ({"on": "L17"}, "aren't drawn yet"),
    ({"strands": "many"}, "a number"),
])
def test_wrong_fields_say_what_is_right(bad, words):
    with pytest.raises(ValueError, match=words):
        video.layers_from([{"type": "effect", "at": 1, **bad}], "", [])


def test_the_sound_triggers_need_footage_and_a_made_video_has_none():
    with pytest.raises(ValueError, match="own footage"):
        video.layers_from([{"type": "effect", "after_said": "now"}], "", [])
    ok = video.layers_from(video._unsaid([{"type": "effect", "after_said": "now"}]), "", [])     # az előzetes ellenőrzés
    assert ok[0]["at"] == 0.0 and video._says([{"type": "effect", "at_speech_end_after": 3}])
    with pytest.raises(ValueError, match="own footage"):
        video._no_effects(ok)


def test_listing_comps_and_edits():
    ly = video.layers_from([{"type": "effect", "at": 6.3, "target": "right"}], "", [])
    assert video.layer_listing(ly)[0]["text"] == "lightning towards the right"
    comps, rows, probs = video.layer_comps(ly, 1080, 1920, 16.5, "en", [1, 0.5, 0], 1800)
    assert comps == [] and rows == [{"id": "L1", "start": 6.3, "end": 10.3, "type": "effect", "layer": True}] and not probs
    ly[0]["trigger"] = {"after_said": "now", "speech_end": 6.3}
    moved = video.apply_layer_changes(ly, {"layers": [{"id": "L1", "at": 3}]}, "", [])[0]
    kept = video.apply_layer_changes(ly, {"layers": [{"id": "L1", "target": "left"}]}, "", [])[0]
    assert "trigger" not in moved and moved["at"] == 3 and kept["trigger"]["after_said"] == "now" and kept["target"] == "left"


# ---------- a kiváltó ideje (10 ms) ----------

def _rms(spans: list[tuple[float, float]], total: float = 6.0) -> list[float]:
    n = round(total / el.HOP_S)
    return [0.2 if any(a <= k * el.HOP_S < b for a, b in spans) else 0.002 for k in range(n)]


def test_the_sentence_end_skips_short_pauses_and_is_at_10ms():
    rms = _rms([(1.0, 1.8), (2.1, 2.47), (4.0, 4.6)])           # 0,3 s szünet a mondaton belül, 1,53 s utána
    assert el.speech_end(rms, 1.2) == 2.47
    assert el.speech_end(rms, 3.0) == 4.6                       # csendre esik: a következő beszéd vége
    assert el.speech_end(rms, 5.0) is None and el.speech_end([], 1.0) is None


def _wav(path: Path, spans: list[tuple[float, float]], total: float = 6.0) -> None:
    sr = 16000
    t = np.arange(int(total * sr)) / sr
    on = np.zeros_like(t, dtype=bool)
    for a, b in spans:
        on |= (t >= a) & (t < b)
    x = np.where(on, 0.5 * np.sin(2 * math.pi * 220 * t), 0.001 * np.sin(2 * math.pi * 50 * t))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(sr)
        w.writeframes((x * 32767).astype(np.int16).tobytes())


def test_after_said_starts_at_the_voices_end_after_the_phrase(tmp_path):
    p = tmp_path / "talk.wav"
    _wav(p, [(1.0, 1.8), (2.1, 2.47), (4.0, 4.6)])
    tr = {"words": [{"w": "Most,", "start": 1.0, "end": 1.4}, {"w": "most", "start": 2.1, "end": 2.2},
                    {"w": "meg", "start": 2.2, "end": 2.25}, {"w": "fogsz", "start": 2.25, "end": 2.3},
                    {"w": "halni.", "start": 2.3, "end": 2.36}],
          "sentences": [{"start": 1.0, "end": 2.36, "text": "Most, most meg fogsz halni."}]}
    heard = []

    async def hear():
        heard.append(1)
        return tr
    notes: list[str] = []
    raw = [{"type": "effect", "after_said": "most meg fogsz halni", "target": "right"},
           {"type": "effect", "after_said": "nem hangzik el"}, {"type": "effect", "at_speech_end_after": 3.0},
           {"type": "title", "at": 1, "text": "x"}]
    out = asyncio.run(el.resolve(raw, p, notes, hear))
    assert len(out) == 3 and heard == [1]
    a, b = out[0], out[1]
    assert abs(a["at"] - 2.47) <= 0.02 and a["trigger"]["word_end"] == 2.36 and "after_said" not in a
    assert abs(b["at"] - 4.6) <= 0.02 and b["trigger"]["at_speech_end_after"] == 3.0
    assert any("is not said" in n and "closest" in n for n in notes)


# ---------- a rajz ----------

W, H, FPS = 180, 320, 24.0


def _frames(n: int) -> list[np.ndarray]:
    yy, xx = np.mgrid[0:H, 0:W]
    out = []
    for i in range(n):                                          # meleg, lassan mozgó kép (BGR)
        b = (30 + 20 * np.sin(xx / 17 + i / 5)).astype(np.uint8)
        g = (60 + 30 * np.cos(yy / 23)).astype(np.uint8)
        r = (110 + 40 * np.sin((xx + yy) / 31 - i / 7)).astype(np.uint8)
        out.append(np.dstack([b, g, r]))
    return out


def _track(n: int) -> list[dict]:
    """A jobb kéz felemelve, nyitott tenyérrel (a póz 33 pontja + a kéz 21 pontja), a kép arányában."""
    pose = [[0.5, 0.5, 0.9] for _ in range(33)]
    pose[11], pose[12] = [0.4, 0.3, 0.95], [0.6, 0.3, 0.95]
    pose[13], pose[14] = [0.35, 0.45, 0.9], [0.68, 0.38, 0.9]
    pose[15], pose[16] = [0.33, 0.6, 0.9], [0.78, 0.4, 0.9]
    pose[18], pose[20], pose[22] = [0.82, 0.38, 0.9], [0.83, 0.4, 0.9], [0.8, 0.42, 0.9]
    pose[23], pose[24] = [0.45, 0.7, 0.9], [0.55, 0.7, 0.9]
    wr = np.array([0.78, 0.4])
    hand = [wr.tolist()]
    for k in range(1, 21):
        ang = -0.8 + 1.6 * ((k - 1) // 4) / 4
        rad = 0.02 + 0.012 * ((k - 1) % 4)
        hand.append((wr + rad * np.array([math.cos(ang), math.sin(ang) * W / H])).tolist())
    return [{"h": [hand], "p": pose} for _ in range(n)]


def _run(name: str, params: dict, seed: int = 7, n: int = 40, start: int = 4, active: int = 20, track=None) -> list[np.ndarray]:
    st = fx.get(name)(W, H, FPS, params, seed)
    tr = track if track is not None else _track(n)
    fr = _frames(n)
    return [st.frame(img, tr[i], (i - start) / FPS, start <= i < start + active) for i, img in enumerate(fr)]


def _h(frames: list[np.ndarray]) -> str:
    return hashlib.sha256(b"".join(np.ascontiguousarray(f).tobytes() for f in frames)).hexdigest()


@pytest.mark.parametrize("name, params", [("lightning", {"target": "right", "strands": 6}),
                                          ("fireball", {"target": [0.2, 0.3], "speed": 2.0, "form": 0.3})])
def test_the_render_is_deterministic_visible_and_fades(name, params):
    a, b = _run(name, params), _run(name, params)
    assert _h(a) == _h(b)                                       # bájtra ugyanaz
    assert _h(_run(name, params, seed=8)) != _h(a)              # más mag: más villám / láng
    src = _frames(40)
    assert all(np.array_equal(a[i], src[i]) for i in range(4))  # az indulás előtt a kép érintetlen
    d = [float(np.abs(a[i].astype(int) - src[i]).mean()) for i in range(40)]
    assert max(d[4:24]) > 15                                     # szól
    assert d[-1] < d[24] or d[-1] < 1.0                         # utána lecseng


def test_lightning_darkens_and_turns_the_scene_blue():
    a, src = _run("lightning", {"target": "right"}), _frames(40)
    f, s = a[20].astype(float), src[20].astype(float)
    # a villámtól távol (bal felső sarok): sötétebb és kékebb (BGR)
    patch, ref = f[:60, :60], s[:60, :60]
    assert patch.mean() < ref.mean() and (patch[..., 0] - patch[..., 2]).mean() > (ref[..., 0] - ref[..., 2]).mean() + 10


def test_a_fixed_anchor_needs_no_tracking():
    empty = [{"h": [], "p": None}] * 40
    assert not common.needs_tracking([0.3, 0.5]) and common.needs_tracking("hand")
    a, src = _run("lightning", {"target": "right", "on": [0.3, 0.5]}, track=empty), _frames(40)
    assert float(np.abs(a[10].astype(int) - src[10]).mean()) > 15
    none = _run("lightning", {"target": "right"}, track=empty)  # kéz nélkül: nincs villám
    assert all(np.array_equal(x, y) for x, y in zip(none, src))


def test_the_anchors_hand_body_point_rectangle():
    tr = _track(1)[0]
    hand = common.Anchors(W, H, "hand")(tr, 0.0, True)
    assert len(hand) == 1 and hand[0].power >= 0.6 and hand[0].center[0] > W * 0.7      # a felemelt jobb kéz, nyitott tenyér
    assert hand[0].d[0] > 0.5                                                            # az alkar iránya: jobbra (könyök → csukló)
    body = common.Anchors(W, H, "body")(tr, 0.0, True)
    assert len(body) == 1 and abs(body[0].center[0] - W * 0.5) < W * 0.05
    pt = common.Anchors(W, H, [0.25, 0.5])({"h": [], "p": None}, 0.0, True)
    rect = common.Anchors(W, H, [0.5, 0.5, 0.4, 0.2])({"h": [], "p": None}, 0.0, True)
    assert pt[0].pts.shape == (1, 2) and rect[0].pts.shape == (6, 2)
    assert common.Anchors(W, H, [0.25, 0.5])({"h": [], "p": None}, 0.0, False) == []      # nem szól: nincs sugárzó
    tgt, d = common.aim("right", hand[0].center, hand[0].d, W, H)
    assert tgt[0] > W and abs(d[1]) < 1e-6


def test_the_clip_key_follows_the_content():
    base = {"file_id": "abc.mov", "width": 1080, "height": 1920, "fps": 24.0}
    ly = video.layers_from([{"type": "effect", "at": 5.86, "target": "right"}], "", [])[0]
    k = lambda l, b=base: el.clip_key(l, b, 24.0, *el.span(l, 24.0, 16.5), False)    # noqa: E731
    same = {**ly, "id": "L9", "trigger": {"after_said": "x"}}
    assert k(ly) == k(same)                                      # az id és a kiváltó nem számít
    assert k(ly) != k({**ly, "target": "left"}) != k({**ly, "strands": 9})
    assert k(ly) != k(ly, {**base, "adjust": {"brightness": 0.1}})        # más szín a felvételen: újra
    assert k(ly) != k({**ly, "at": 6.0})
    f0, active, f1 = el.span(ly, 24.0, 16.5)
    assert (f0, active, f1 - f0 - active) == (141, 96, fx.get("lightning").tail())


def test_blend_screen_draws_only_the_light():
    """blend "screen": a fény fekete alapon (a jelenet színezése nélkül) — a lejátszó / a motor screen keveréssel teszi a
    képre; ahol nincs fény, a klip fekete (a screen ott nem változtat)."""
    ly = video.layers_from([{"type": "effect", "at": 1, "blend": "screen", "target": "right"}], "", [])[0]
    assert ly["blend"] == "screen" and el.fx_params(ly)["blend"] == "screen"
    with pytest.raises(ValueError, match="over .* screen"):
        video.layers_from([{"type": "effect", "at": 1, "blend": "multiply"}], "", [])
    light, src = _run("lightning", {"target": "right", "blend": "screen"}), _frames(40)
    over = _run("lightning", {"target": "right"})
    assert all(not x.any() for x in light[:4])                   # a kezdete előtt: fekete (a screen semmit sem változtat)
    f = light[20].astype(float)
    assert f.max() > 200 and (f.sum(2) < 6).mean() > 0.3         # erős fény, a kép nagy része fekete
    # screen(kép, fény) ≈ az over-rajz fénye a színezés nélkül: a fény helyén világosabb a képnél
    scr = 255 - (255 - src[20].astype(float)) * (255 - f) / 255
    assert (scr >= src[20] - 0.5).all() and _h(light) != _h(over)


def test_the_player_blend_factors_give_screen_and_add():
    """A lejátszó GL-keverése (live-player.js blendOf: src·Fs + dst·Fd, előszorzott rgb·a): screen = 1 − (1 − base)(1 − rgb·a)."""
    js = Path(__file__).resolve().parents[1] / "app" / "static" / "live-player.js"
    t = js.read_text("utf-8")
    assert 'b === "screen") return { src: "ONE", dst: "ONE_MINUS_SRC_COLOR", premul: 1 }' in t
    assert "uPremul == 1 ? vec4(rgb * a, 1.0) : vec4(rgb, a)" in t and "gl.blendFunc(gl[bf.src], gl[bf.dst])" in t
    base, rgb, a = np.array([0.6, 0.3, 0.1]), np.array([0.2, 0.4, 0.8]), 0.75
    src = rgb * a                                                # a shader kimenete (előszorzott)
    gl_screen = src * 1.0 + base * (1 - src)                     # ONE, ONE_MINUS_SRC_COLOR
    assert np.allclose(gl_screen, 1 - (1 - base) * (1 - rgb * a))
    assert np.allclose(src * 1.0 + base * 1.0, base + rgb * a)   # add: ONE, ONE


def test_no_sqlite():
    for p in (el.__file__, common.__file__, fx.__file__):
        assert "sqlite" not in Path(p).read_text("utf-8").lower()


# ---------- a képre tétel: normál keverésű réteg (az élő lejátszó és a gyors render változtatás nélkül) ----------

def _engine_ok() -> bool:
    import shutil
    return video.available() and bool(shutil.which("ffmpeg"))


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_on_a_real_overlay_the_effect_is_an_opaque_normal_layer_over_the_footage(tmp_path, put_file, monkeypatch):
    """Rögzített horgony (követés nélkül): a klip előre komponálva a felvétel fölött, a grafika alatt; a gyors render
    grafikás szakasznak veszi (nem a régi út), az élő előnézetnek támogatott réteg, a szerkesztés újrahasználja."""
    from app.tools import fast_render, live_layers as ll
    ll._reset()
    video._gate = video._bgate = None
    monkeypatch.setattr(video, "_start_final", lambda pid, eta: {"id": "stub"})
    src = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=48000", "-t", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(src)], check=True)
    fid = put_file(src)
    layers = [{"type": "effect", "effect": "lightning", "at": 1.0, "dur": 1.5, "on": [0.25, 0.5], "target": "right"},
              {"at": 1.2, "dur": 2, "type": "source", "text": "Forrás: teszt"}]

    async def main():
        res = await video.overlay({"file_id": fid, "brand": "none", "layers": layers, "render": False})
        pid = res["file_id"]
        assert [x["type"] for x in res["layers"]] == ["effect", "source"]
        (tmp_path / "w").mkdir()
        proj = video.stage(pid, tmp_path / "w")
        info = await fast_render.inspect(proj)
        names = [x["name"] for x in info["layers"]]              # 1 = legfelső
        fxl = next(x for x in info["layers"] if x["name"].startswith("Layer L1"))
        foot = next(x for x in info["layers"] if x["name"] == "Footage")
        src_l = next(x for x in info["layers"] if x["name"].startswith("Layer L2"))
        assert src_l["i"] < fxl["i"] < foot["i"], names           # a felvétel fölött, a grafika alatt
        assert fxl["normal"] and abs(fxl["a"] - 1.0) < 0.05 and fxl["b"] > 2.5
        spec = video.read_meta(pid)["spec"]
        cl = fast_render.classify(info, spec)
        assert cl["ok"], cl["why"]                                # a gyors út viszi (grafikás szakasz)
        sc = ll.scene(pid)
        e = {x["id"]: x for x in sc["layers"]}
        assert e["L1"]["unsupported"] is None and e["L1"]["z"] < e["L2"]["z"]
        again = await video.edit(pid, {"layers": [{"id": "L2", "text": "Forrás: második"}]}, render=False)
        assert any("from the cache" in v for v in again["check"]["verified"])
        # a klip: a horgonynál kék, a kezdete előtt a felvétel
        clip = next(p for n, p in video.assets_of(pid).items() if n.startswith("effect-L1-"))
        assert clip
    asyncio.run(main())
    ll._reset()
