"""3.7 (OMLETT, 10-19): a Lottie-keresés a választott animáció nevét és előnézetét visszaadja, az átlátszó és a doboz
alakjához illő animációt veszi előre, `pick` indexszel másik választható, és a check mintát vesz a Lottie-elemekből
(10-19: a „confetti” lila háromszögfoltként ült a szövegen). Hálózat nélkül: a keresés és a letöltés helyettesítve."""
from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest


def lottie_json(w: int, h: int, rw: int, rh: int, color: list, solid: bool = False) -> dict:
    tr = {"ty": "tr", "p": {"a": 0, "k": [0, 0]}, "a": {"a": 0, "k": [0, 0]}, "s": {"a": 0, "k": [100, 100]}, "r": {"a": 0, "k": 0},
          "o": {"a": 0, "k": 100}, "sk": {"a": 0, "k": 0}, "sa": {"a": 0, "k": 0}}
    layers = [{"ddd": 0, "ind": 1, "ty": 4, "nm": "shape", "sr": 1,
               "ks": {"o": {"a": 0, "k": 100}, "r": {"a": 0, "k": 0}, "p": {"a": 0, "k": [w / 2, h / 2, 0]},
                      "a": {"a": 0, "k": [0, 0, 0]}, "s": {"a": 0, "k": [100, 100, 100]}},
               "ao": 0, "shapes": [{"ty": "gr", "nm": "g", "it": [
                   {"ty": "rc", "d": 1, "s": {"a": 0, "k": [rw, rh]}, "p": {"a": 0, "k": [0, 0]}, "r": {"a": 0, "k": 0}, "nm": "r"},
                   {"ty": "fl", "c": {"a": 0, "k": color + [1]}, "o": {"a": 0, "k": 100}, "r": 1, "nm": "f"}, tr]}],
               "ip": 0, "op": 60, "st": 0, "bm": 0}]
    if solid:
        layers.append({"ddd": 0, "ind": 2, "ty": 1, "nm": "bg", "sw": w, "sh": h, "sc": "#ffffff", "ks": layers[0]["ks"],
                       "ip": 0, "op": 60, "st": 0, "bm": 0})
    return {"v": "5.7.4", "fr": 30, "ip": 0, "op": 60, "w": w, "h": h, "nm": "x", "ddd": 0, "assets": [], "layers": layers}


CANDS = {"blob": lottie_json(200, 200, 200, 200, [0.6, 0.2, 0.8]),           # a 10-19-i lila folt: az egész vásznat fedi
         "wide": lottie_json(400, 100, 30, 30, [0.9, 0.8, 0.2]),             # átlátszó, de fekvő csík
         "square": lottie_json(200, 200, 40, 40, [0.2, 0.8, 0.3]),           # átlátszó, négyzetes
         "solidbg": lottie_json(200, 200, 40, 40, [0.2, 0.3, 0.9], solid=True)}


@pytest.fixture()
def fake_lottiefiles(monkeypatch):
    from app.tools import images, lottie
    async def search(q, n=8):
        return [{"name": k.capitalize(), "json_url": f"https://lottie.test/{k}.json", "thumb_url": f"https://lottie.test/{k}.png",
                 "gif_url": None, "page": f"https://lottie.test/{k}", "author": "Tester", "background": None,
                 "credit": f"Animation “{k.capitalize()}” by Tester (LottieFiles)"} for k in CANDS]
    async def fetch(url):
        return lottie.check(dict(CANDS[url.rsplit("/", 1)[1][:-5]]))
    async def rank(query, context, cands, prompt=None, medium="video"):
        return list(range(len(cands)))                     # a látómodell sorrendje: blob, wide, square, solidbg
    monkeypatch.setattr(lottie, "search", search)
    monkeypatch.setattr(lottie, "fetch", fetch)
    monkeypatch.setattr(images, "rank", rank)


def test_pick_prefers_a_transparent_animation_of_the_boxes_shape(fake_lottiefiles):
    from app.tools import lottie
    assert lottie.opaque_background(CANDS["blob"]) and lottie.opaque_background(CANDS["solidbg"])
    assert not lottie.opaque_background(CANDS["square"]) and not lottie.opaque_background(CANDS["wide"])
    data, c = asyncio.run(lottie.pick("confetti", "news", aspect=1.0))
    assert c["name"] == "Square" and c["pick"] == 1 and c["transparent"] and c["thumb_url"].endswith("square.png")
    assert [a["name"] for a in c["alternatives"]] == ["Wide", "Blob", "Solidbg"]       # átlátszó előre, az alak számít
    _, c2 = asyncio.run(lottie.pick("confetti", "news", aspect=4.0))
    assert c2["name"] == "Wide"
    _, c3 = asyncio.run(lottie.pick("confetti", "news", aspect=1.0, n=3))
    assert c3["pick"] == 3 and c3["name"] == "Blob" and not c3["transparent"]


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI and ffmpeg needed")
def test_result_names_the_animation_and_the_check_samples_it(tmp_path, put_file, fake_lottiefiles):
    from app.tools import video
    clip = tmp_path / "c.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:size=640x360:rate=10", "-t", "4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True)
    text = {"shape": "text", "text": "Omlett", "x": 0.5, "y": 0.45, "size": 60}
    # pick 3 = a lila folt (a 10-19-i eset): a szövegen ül, és tömör a háttere
    layers = [{"id": "K", "at": 0.2, "dur": 3.5, "type": "graphic", "elements": [
        text, {"shape": "lottie", "lottie": {"search": "confetti", "pick": 3}, "x": 0.5, "y": 0.5, "w": 0.3, "h": 0.5}]}]
    res = asyncio.run(video.overlay({"file_id": put_file(clip), "layers": layers, "render": False}))
    pk = res["lottie_picks"][0]
    assert pk["where"] == "Layer K, element 2" and pk["name"] == "Blob" and pk["thumb_url"].endswith("blob.png")
    assert pk["pick"] == 3 and pk["alternatives"][0]["name"] == "Square"
    f = [x for x in res["check"]["findings"] if x.startswith("Layer K, element 2")]
    assert any("sits on element 1" in x for x in f) and any("opaque background" in x for x in f), res["check"]
    # az alapválasztás (átlátszó, négyzetes) a szöveg mellé téve: mintát vett, tiszta
    layers[0]["elements"][1] = {"shape": "lottie", "lottie": {"search": "confetti"}, "x": 0.85, "y": 0.5, "w": 0.2, "h": 0.35}
    res = asyncio.run(video.overlay({"file_id": put_file(clip), "layers": layers, "render": False}))
    assert res["lottie_picks"][0]["name"] == "Square"
    assert not [x for x in res["check"]["findings"] if x.startswith("Layer K, element 2")], res["check"]
    assert "Animation “Square” by Tester (LottieFiles)" in res["credits"]
