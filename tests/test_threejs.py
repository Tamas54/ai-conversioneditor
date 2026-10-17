"""three.js 3D-jelenet a videóban (10-18): a kliens kódja zárt böngészőben, képkockánként, determinisztikusan
renderelve; a klip jelenetként és rétegként is a videóba kerül; hiba a check-ben; ami egyszer lerenderelődött, újra nem."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

BOX = r"""
const scene = new THREE.Scene(); const camera = new THREE.PerspectiveCamera(40, W / H, 0.1, 100); camera.position.z = 4;
const box = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshStandardMaterial({color: accent}));
scene.add(box); scene.add(new THREE.AmbientLight(0xffffff, 1)); scene.add(label("A", {height: 0.4}));
return {scene, camera, update(t) { box.rotation.y = t * 2; box.position.x = Math.random() - 0.5; }};
"""


def test_plan_is_checked_and_the_scene_becomes_a_placeholder_until_rendered():
    from app.tools import video
    ch, notes = video._chart_from_plan({"type": "threejs", "title": "Kocka", "code": BOX}, "")
    assert ch["type"] == "threejs" and ch["transparent"] is True and "sandbox" in notes[0]
    assert video._chart_from_plan(ch, "")[0] == ch                                  # a tárolt terv újra beolvasva ugyanaz
    gone, why = video._chart_from_plan({"type": "threejs", "code": ""}, "")
    assert gone is None and why[0].startswith("⚠")
    els = video.chart_elements(ch, 1920, 1080, "hu", [1, 0.5, 0.2], dur=6)
    assert els == [{"k": "threejs", "name": "3D scene", "code": BOX, "transparent": True, "size": [1920, 1080], "pos": [960.0, 540.0]}]


async def test_render_pass_swaps_the_placeholder_for_the_clip_in_scenes_and_layers(tmp_path, monkeypatch):
    from app.tools import threejs, video
    seen = []

    async def render(code, W, H, dur, *, transparent=True, accent=None, dest):
        seen.append((W, H, dur))
        p = dest / f"threejs-{len(seen)}.mov"
        p.write_bytes(b"x")
        return p, ["3D scene rendered."]
    monkeypatch.setattr(threejs, "render", render)
    mark = lambda w, h: {"k": "threejs", "name": "3D scene", "code": BOX, "transparent": True, "size": [w, h], "pos": [w / 2, h / 2]}  # noqa: E731
    D = {"scenes": [{"dur": 6.0, "chart": {"elements": [mark(1920, 1080)]}}], "layers": [{"dur": 4.0, "elements": [mark(800, 450)]}]}
    assert video._has_3d(D)
    names, notes = await video._render_3d(D, tmp_path, [1, 0.5, 0.2])
    assert names == ["threejs-1.mov", "threejs-2.mov"] and seen == [(1920, 1080, 6.0), (800, 450, 4.0)]
    el = D["scenes"][0]["chart"]["elements"][0]
    assert el["k"] == "image" and el["file"] == "threejs-1.mov" and el["fit"] == "contain" and not video._has_3d(D)


def _chromium() -> bool:
    exe = os.getenv("CHROMIUM_EXECUTABLE")
    if exe:
        return Path(exe).exists()
    try:
        from playwright._impl._driver import compute_driver_executable  # noqa: F401
        return any(Path.home().joinpath(".cache/ms-playwright").glob("chromium-*")) or Path("/ms-playwright").exists()
    except Exception:
        return False


@pytest.mark.engines
async def test_a_real_render_is_deterministic_cached_and_errors_are_said(tmp_path):
    if not _chromium() or shutil.which("ffmpeg") is None:
        pytest.skip("Chromium / ffmpeg not installed")
    from app.tools import threejs
    a, n1 = await threejs.render(BOX, 160, 90, 1.0, transparent=False, dest=tmp_path)
    if a is None and "Executable doesn't exist" in " ".join(n1):
        pytest.skip("the Playwright Chromium build is missing (set CHROMIUM_EXECUTABLE)")
    assert a and a.exists() and "rendered" in n1[0]
    b, n2 = await threejs.render(BOX, 160, 90, 1.0, transparent=False, dest=tmp_path / "..")
    assert "reused" in n2[0] and b.read_bytes() == a.read_bytes()                   # ugyanaz a kód: a gyorsítótárból
    bad, n3 = await threejs.render("throw new Error('nope')", 160, 90, 1.0, dest=tmp_path)
    assert bad is None and "nope" in n3[0]
