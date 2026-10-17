"""three.js 3D-jelenet a videóba (10-18, Kommandant: „three.js grafikákat nem tudunk beletenni? … nyomjuk”).

A kliens AI (ChatGPT, Claude) megírja a jelenetet three.js-ben; mi fej nélküli Chromiumban, szoftveres WebGL-lel
(SwiftShader — a szerveren nincs videókártya) képkockánként lerendereljük, és a klip a videó egy rétege lesz, mint egy
kép vagy egy Lottie: jelenet, saját felvételre tett réteg, osztott kép.

A kód szerződése (a hívó függvény törzse): kapja a THREE-t, a renderert, W, H (px), dur (s), accent (THREE.Color) és a
label(szöveg, {size, color, bg, weight}) segédet (felirat-sprite a saját betűnkkel); VISSZAAD {scene, camera, update?(t)}.
Az idő NEM a fali óra: minden képkockánál update(t) fut (t = 0…dur), aztán egy render — így determinisztikus (a
Math.random is magolt), a lassú gépen is pontosan dur hosszú. Zárt: a lap semmit nem tölt le (a three.js-t mi adjuk,
minden más kérés tiltva), időkorlát van. AMIT EGYSZER LERENDERELTÜNK, NEM RENDERELJÜK ÚJRA: a kód + méret + hossz
hash-e szerint a klip a gyorsítótárban marad (a szerkesztés csak a megváltozott jelenetet rendereli újra).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from app.config import settings

log = logging.getLogger("aice.threejs")

THREE_JS = Path(__file__).parent / "vendor" / "three" / "three.module.min.js"
VERSION = "r170"
FPS = 30
MAX_CODE = 30_000
MAX_SECONDS = 20.0
TIME_LIMIT_S = 300
_ORIGIN = "https://studio.render"
_lock = asyncio.Semaphore(1)        # egyszerre egy 3D-render (CPU-igényes; a többi sorban vár)

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;background:transparent;overflow:hidden}
canvas{display:block}</style></head><body><canvas id="c"></canvas>
<script type="module">
import * as THREE from "%(origin)s/three.module.min.js";
let seed = 1234567;
Math.random = () => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed / 4294967296; };
const W = %(W)d, H = %(H)d, dur = %(dur)s, transparent = %(transparent)s;
const accent = new THREE.Color(%(accent)s);
const canvas = document.getElementById("c");
const renderer = new THREE.WebGLRenderer({canvas, antialias: true, alpha: true, preserveDrawingBuffer: true});
renderer.setPixelRatio(1); renderer.setSize(W, H, false);
renderer.setClearColor(0x000000, transparent ? 0 : 1);
renderer.outputColorSpace = THREE.SRGBColorSpace;
function label(text, o = {}) {           // felirat-sprite: a szöveg vásznon, a saját betűnkkel (Inter), éles méretben
  const size = o.size || 48, pad = Math.round(size * 0.35), font = `${o.weight || 600} ${size}px Inter, "Noto Sans", sans-serif`;
  const c = document.createElement("canvas"), g = c.getContext("2d");
  g.font = font; const w = Math.ceil(g.measureText(String(text)).width) + pad * 2, h = Math.ceil(size * 1.35) + pad;
  c.width = w; c.height = h; g.font = font;
  if (o.bg) { g.fillStyle = o.bg; g.beginPath(); g.roundRect(0, 0, w, h, size * 0.3); g.fill(); }
  g.fillStyle = o.color || "#ffffff"; g.textBaseline = "middle"; g.fillText(String(text), pad, h / 2);
  const tex = new THREE.CanvasTexture(c); tex.colorSpace = THREE.SRGBColorSpace;
  const s = new THREE.Sprite(new THREE.SpriteMaterial({map: tex, transparent: true, depthTest: o.depthTest ?? false}));
  const k = (o.height || 0.5) / h; s.scale.set(w * k, h * k, 1); return s;
}
window.__err = null;
try {
  const user = new Function("THREE", "renderer", "W", "H", "dur", "accent", "label", %(code)s);
  window.__scene = user(THREE, renderer, W, H, dur, accent, label);
  if (!window.__scene || !window.__scene.scene || !window.__scene.camera)
    window.__err = "the code must return {scene, camera, update?(t)}";
} catch (e) { window.__err = String((e && e.stack) || e); }
window.__step = (t) => {
  const s = window.__scene;
  if (s.update) s.update(t);
  renderer.render(s.scene, s.camera);
  return canvas.toDataURL("image/png");
};
window.__ready = true;
</script></body></html>"""


def from_plan(c: dict[str, Any], clean) -> tuple[Optional[dict[str, Any]], list[str]]:
    """A kliens 3D-jelenete → tárolható ábra | None + megjegyzések."""
    code = str(c.get("code") or "")
    title = clean(c.get("title"), 60, "Chart title")
    if not code.strip():
        return None, ["⚠ A 3D scene (threejs) needs \"code\": a function body that returns {scene, camera, update(t)}."]
    if len(code) > MAX_CODE:
        return None, [f"⚠ The 3D scene “{title}” is too long ({len(code)} characters; {MAX_CODE} at most)."]
    out = {"type": "threejs", "title": title, "unit": "", "items": [], "code": code,
           "transparent": bool(c.get("transparent", True))}
    if isinstance(c.get("backdrop"), (int, float)):
        out["backdrop"] = max(0.0, min(1.0, float(c["backdrop"])))
    return out, [f"3D scene “{title or 'three.js'}”: {code.count(chr(10)) + 1} lines of three.js ({VERSION}), rendered in a "
                 "sandbox; text drawn in it is not checked against the source."]


def _key(code: str, W: int, H: int, dur: float, transparent: bool, accent: list[float]) -> str:
    raw = json.dumps([VERSION, code, W, H, round(dur, 2), transparent, [round(a, 3) for a in accent], FPS])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _cache_dir() -> Path:
    d = settings.DATA_DIR / "cache" / "threejs"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def render(code: str, W: int, H: int, dur: float, *, transparent: bool = True, accent: Optional[list[float]] = None,
                 dest: Path) -> tuple[Optional[Path], list[str]]:
    """→ (a klip a dest mappában | None, megjegyzések). Átlátszó: ProRes 4444 .mov (alfa), különben H.264 .mp4."""
    accent = accent or [1.0, 0.35, 0.21]
    W, H = max(16, int(W) // 2 * 2), max(16, int(H) // 2 * 2)
    dur = max(0.5, min(MAX_SECONDS, float(dur)))
    ext = "mov" if transparent else "mp4"
    key = _key(code, W, H, dur, transparent, accent)
    cached = _cache_dir() / f"{key}.{ext}"
    name = f"threejs-{key}.{ext}"
    if cached.exists():
        shutil.copyfile(cached, dest / name)
        return dest / name, [f"3D scene rendered before — reused ({W}×{H}, {dur:.1f} s)."]
    async with _lock:
        try:
            frames, err = await asyncio.wait_for(_frames(code, W, H, dur, transparent, accent), TIME_LIMIT_S)
        except asyncio.TimeoutError:
            return None, [f"⚠ The 3D scene took longer than {TIME_LIMIT_S // 60} minutes to render and was left out — make it "
                          "simpler (fewer objects / lights, smaller textures) or shorter."]
        except Exception as e:                # a böngésző nem indul / összeomlik: a videó elkészül, a 3D kimarad
            log.warning("3D-render nem futott: %s", e)
            return None, [f"⚠ The 3D scene couldn't be rendered on this server ({str(e).splitlines()[0][:200]}) — it was left out."]
    if err:
        return None, [f"⚠ The 3D scene failed: {err[:400]} — fix its code (chart.code) with action=edit."]
    try:
        await _encode(frames, cached, transparent)
    finally:
        shutil.rmtree(frames, ignore_errors=True)
    shutil.copyfile(cached, dest / name)
    return dest / name, [f"3D scene rendered: {int(round(dur * FPS))} frames, {W}×{H}" + (", transparent" if transparent else "") + "."]


async def _frames(code: str, W: int, H: int, dur: float, transparent: bool, accent: list[float]) -> tuple[Path, Optional[str]]:
    from playwright.async_api import async_playwright
    out = Path(tempfile.mkdtemp(prefix="threejs-", dir=str(settings.DATA_DIR / "temp") if (settings.DATA_DIR / "temp").exists() else None))
    page_html = _PAGE % {"origin": _ORIGIN, "W": W, "H": H, "dur": json.dumps(dur), "transparent": "true" if transparent else "false",
                         "accent": ", ".join(f"{a:.4f}" for a in accent), "code": json.dumps(code)}
    lib = THREE_JS.read_bytes()
    pw = await async_playwright().start()
    try:
        # hálózat sehol: a lapkérést a route szolgálja ki, a WebSocketet / WebRTC-t viszont a route nem fogja meg — ezért
        # halott proxy (IP-címre sem jut ki), névfeloldás nincs, UDP-kapcsolat nincs (a kliens kódja nem érhet el semmit)
        browser = await pw.chromium.launch(executable_path=os.getenv("CHROMIUM_EXECUTABLE") or None, args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--use-angle=swiftshader", "--use-gl=angle", "--enable-unsafe-swiftshader",
            "--ignore-gpu-blocklist", "--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=<-loopback>",
            "--host-resolver-rules=MAP * ~NOTFOUND", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
        ctx = await browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1)
        page = await ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        async def route(r):                       # zárt lap: csak a mi lapunk és a three.js — semmi más (hálózat sincs)
            u = r.request.url
            if u == f"{_ORIGIN}/":
                await r.fulfill(status=200, content_type="text/html", body=page_html)
            elif u == f"{_ORIGIN}/three.module.min.js":
                await r.fulfill(status=200, content_type="text/javascript", body=lib)
            else:
                await r.abort()
        await page.route("**/*", route)
        await page.goto(f"{_ORIGIN}/", wait_until="load", timeout=60000)
        await page.wait_for_function("window.__ready === true || window.__err !== null", timeout=60000)
        err = await page.evaluate("window.__err")
        if err or errors:
            return out, err or errors[0]
        n = int(round(dur * FPS))
        for i in range(n):
            data = await page.evaluate(f"window.__step({i / FPS:.5f})")
            if errors:
                return out, errors[0]
            (out / f"f{i:05d}.png").write_bytes(base64.b64decode(data.split(",", 1)[1]))
        return out, None
    finally:
        try:
            await pw.stop()
        except Exception:
            pass


async def _encode(frames: Path, dst: Path, transparent: bool) -> None:
    from app.convert.engines import run
    args = ["ffmpeg", "-v", "error", "-y", "-framerate", str(FPS), "-i", str(frames / "f%05d.png")]
    if transparent:
        args += ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-vendor", "apl0"]
    else:
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p"]
    tmp = dst.with_suffix(".part" + dst.suffix)
    await run(*args, str(tmp), timeout=600)
    tmp.replace(dst)
