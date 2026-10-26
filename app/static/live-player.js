/* Live preview compositor (plan20261026_live_preview.md, lane L1) — plain JS, no build step.

   The server gives assets (app/tools/live_scene.py → /api/video/<pid>/live.json); this file composes them on the GPU:
     · footage: two <video> elements on the working copies (the next range pre-rolled, so a cut plays across without a
       gap), the ACTIVE one is the master clock (mapped to output time), its frames uploaded to a WebGL2 texture;
     · colour: the copy's YUV codes recovered (BT.709 limited range), ffmpeg vf_eq's tables applied (the inverse of a
       baked recipe first, when the copy isn't neutral), back to RGB;
     · transform: the engine's footage-layer keys (anchor, position, scale, rotation, opacity), evaluated with its
       interpolation (hold / linear / temporal Bézier speed+influence) — zoom and split;
     · hold: after footage.hold_from the last frame stands (end card), the clock runs on the wall clock;
     · layers (lane L2): stacked-alpha clips (W×2H H.264: top half straight RGB, bottom half alpha as luma; frame 0 at
       t0; loop {intro, period} in seconds), drawn as out = mix(base, rgb, a), only within a window around the playhead;
       a layer with blend "screen" (an effect's light) as out = 1 − (1 − base)(1 − rgb·a), "add" as out = base + rgb·a
       (LivePlayer.blendOf: the GL blend factors; the shader then writes premultiplied rgb·a);
     · captions (lane L3): window.LiveCaptions on a transparent element above the canvas, setTime(t) every frame;
     · matte (deck layer, placement background — app/tools/deck_layer.py): inside scene.matte.spans the footage is drawn
       AGAIN over the layers flagged `behind` (the deck's backdrop and slides), through a person mask — MediaPipe Tasks
       Vision ImageSegmenter (Apache-2.0, selfie segmenter landscape), loaded on demand, run on each new footage frame,
       smoothed over time (EMA) and sharpened with levels (smoothstep) in the shader. p.stats.matte has its timings.

   API: const p = new LivePlayer(stageEl, {preserve}); await p.load(scene); p.update(scene[, {time}]); p.play();
   p.pause(); await p.seek(t); p.currentTime; p.duration; p.paused; p.addEventListener("firstframe" | "time" | "seeked" |
   "play" | "pause" | "switch" | "stall" | "error", …); p.stats; p.grab() → {w, h, data} (RGBA of the canvas).
   LivePlayer.evalProp(keys, rest, t) — the engine interpolation (also used by node in the QA). */
(function (root) {
  "use strict";

  // ---------- the engine's interpolation (= live_scene.value_at) ----------
  function bez(p1x, p1y, p2x, p2y, u) {
    let lo = 0, hi = 1, s = 0.5;
    for (let i = 0; i < 50; i++) {
      s = (lo + hi) / 2;
      const x = 3 * (1 - s) * (1 - s) * s * p1x + 3 * (1 - s) * s * s * p2x + s * s * s;
      if (x < u) lo = s; else hi = s;
    }
    s = (lo + hi) / 2;
    return 3 * (1 - s) * (1 - s) * s * p1y + 3 * (1 - s) * s * s * p2y + s * s * s;
  }
  function evalProp(ks, rest, t) {
    if (!ks || !ks.length) return rest;
    if (t <= ks[0].t) return ks[0].v;
    const last = ks[ks.length - 1];
    if (t >= last.t) return last.v;
    let i = 0;
    while (i + 1 < ks.length && ks[i + 1].t <= t) i++;
    const a = ks[i], b = ks[i + 1];
    if (a.out === "hold") return a.v;
    const dt = b.t - a.t, u = dt > 0 ? (t - a.t) / dt : 1;
    const scalar = !Array.isArray(a.v), va = scalar ? [a.v] : a.v, vb = scalar ? [b.v] : b.v, res = [];
    for (let d = 0; d < va.length; d++) {
      const x0 = +va[d], x1 = +vb[d], span = x1 - x0;
      if (a.out === "linear" && b.in === "linear") { res.push(x0 + span * u); continue; }
      if (Math.abs(span) < 1e-12) { res.push(x0); continue; }
      const lin = dt ? span / dt : 0;
      const oe = a.out === "bezier" ? (a.oe && a.oe.length ? a.oe[Math.min(d, a.oe.length - 1)] : [0, 1 / 3]) : [lin, 1 / 3];
      const ie = b.in === "bezier" ? (b.ie && b.ie.length ? b.ie[Math.min(d, b.ie.length - 1)] : [0, 1 / 3]) : [lin, 1 / 3];
      const p1x = Math.min(1, Math.max(0, oe[1])), p2x = 1 - Math.min(1, Math.max(0, ie[1]));
      const p1y = oe[0] * oe[1] * dt / span, p2y = 1 - ie[0] * ie[1] * dt / span;
      res.push(x0 + span * bez(p1x, p1y, p2x, p2y, u));
    }
    return scalar ? res[0] : res;
  }
  function groupKeys(tr) {
    const by = {};
    for (const k of (tr && tr.keys) || []) (by[k.prop] = by[k.prop] || []).push(k);
    for (const p in by) by[p].sort((x, y) => x.t - y.t);
    return by;
  }
  // output uv (0…1, top-left) → the copy's texture coordinate, from the layer transform at t
  function footageMatrix(tr, by, t, texW, texH) {
    const R = (tr && tr.rest) || {}, C = (tr && tr.comp) || [texW, texH], S0 = (tr && tr.src) || [texW, texH];
    const A = evalProp(by.anchor, R.anchor || [S0[0] / 2, S0[1] / 2], t), P = evalProp(by.position, R.position || [C[0] / 2, C[1] / 2], t);
    const Sc = evalProp(by.scale, R.scale || [100 * C[0] / S0[0], 100 * C[1] / S0[1]], t), rot = evalProp(by.rotation, R.rotation || 0, t);
    const op = evalProp(by.opacity, R.opacity == null ? 100 : R.opacity, t);
    const th = -(+rot || 0) * Math.PI / 180, cs = Math.cos(th), sn = Math.sin(th);
    const sx = (Sc[0] || 100) / 100, sy = (Sc[1] || 100) / 100;
    // p = A + diag(1/sx, 1/sy) · Rot(−θ) · (c − P),  c = (uv.x·CW, uv.y·CH),  tex = p / S0
    const m00 = cs * C[0] / sx, m01 = -sn * C[1] / sx, m10 = sn * C[0] / sy, m11 = cs * C[1] / sy;
    const m02 = A[0] - (cs * P[0] - sn * P[1]) / sx, m12 = A[1] - (sn * P[0] + cs * P[1]) / sy;
    return { m: [m00 / S0[0], m01 / S0[0], m02 / S0[0], m10 / S0[1], m11 / S0[1], m12 / S0[1]], opacity: (+op) / 100 };
  }

  // ---------- WebGL ----------
  const VS = `#version 300 es
in vec2 aPos; out vec2 vUv;
void main() { vUv = aPos; gl_Position = vec4(aPos.x * 2.0 - 1.0, 1.0 - aPos.y * 2.0, 0.0, 1.0); }`;
  const FS_FOOT = `#version 300 es
precision highp float;
in vec2 vUv; out vec4 o;
uniform sampler2D uVid, uLut; uniform int uMode; uniform vec3 uBg; uniform mat3 uM; uniform float uAlpha, uOpacity;
const vec3 K = vec3(0.2126, 0.7152, 0.0722);
float lut(int row, float code) { return texelFetch(uLut, ivec2(clamp(int(floor(code + 0.5)), 0, 255), row), 0).r * 255.0; }
void main() {
  vec2 tc = (uM * vec3(vUv, 1.0)).xy;
  vec3 c = uBg;
  if (tc.x >= 0.0 && tc.y >= 0.0 && tc.x <= 1.0 && tc.y <= 1.0) {
    vec3 rgb = texture(uVid, tc).rgb;
    if (uMode > 0) {
      float yl = dot(rgb, K);
      float Y = 16.0 + 219.0 * yl, U = 128.0 + 112.0 * (rgb.b - yl) / 0.9278, V = 128.0 + 112.0 * (rgb.r - yl) / 0.7874;
      if (uMode == 2) { Y = lut(0, Y); U = lut(1, U); V = lut(1, V); }
      Y = lut(2, Y); U = lut(3, U); V = lut(3, V);
      float y = (Y - 16.0) / 219.0, cb = (U - 128.0) / 224.0, cr = (V - 128.0) / 224.0;
      rgb = clamp(vec3(y + 1.5748 * cr, y - 0.187324 * cb - 0.468124 * cr, y + 1.8556 * cb), 0.0, 1.0);
    }
    c = mix(uBg, rgb, uOpacity);
  }
  o = vec4(c, uAlpha);
}`;
  // the speaker pass: the footage shader, its alpha from the person mask (in the copy's texture space) through levels
  // (the mask is small — 256×144: five bilinear taps ±0.6 texel round its stair-steps before the levels cut them)
  const FS_SPEAKER = FS_FOOT.replace("uniform float uAlpha, uOpacity;", "uniform float uAlpha, uOpacity; uniform sampler2D uMask; uniform vec2 uLv, uMpx;\n" +
      "float maskAt(vec2 p) { vec2 d = 0.6 * uMpx; return (2.0 * texture(uMask, p).r + texture(uMask, p + d).r + texture(uMask, p - d).r + " +
      "texture(uMask, p + vec2(d.x, -d.y)).r + texture(uMask, p + vec2(-d.x, d.y)).r) / 6.0; }")
    .replace("o = vec4(c, uAlpha);", "float mk = (tc.x >= 0.0 && tc.y >= 0.0 && tc.x <= 1.0 && tc.y <= 1.0) ? " +
      "smoothstep(uLv.x, uLv.y, maskAt(tc)) : 0.0;\n  o = vec4(c, mk * uAlpha);");
  const MP_VER = "1.0.1";
  const MP_CDN = "/static/vendor/mediapipe/tasks-vision";      // self-hosted @mediapipe/tasks-vision@1.0.1 (Apache-2.0)
  const SLOW_MS = 30;
  const MP_SEM = "/static/vendor/mediapipe/selfie_multiclass_256x256.tflite";
  const SEM_FRESH_S = 1.0;           // an older semantic answer isn't used
  const SEM_GATE_R = 2;
  const SEM_FAST_MS = 110;
  const SEM_FILL_MIN = 0.9, SEM_FILL_R = 3;   // (= deck_matte RECIPE.fill: 0.9, 4 texels of 256² ≈ 3 of 256×144)
  const SEM_EVERY_MS = 100;              // texels (of 256×144) around its person: the per-frame mask decides there
  function dilate(src, w, h, r, out, tmp) {   // binary (0/1) square max filter (2r+1), separable
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      let v = 0; const row = y * w;
      for (let d = -r; d <= r; d++) { const xx = x + d; if (xx >= 0 && xx < w && src[row + xx]) { v = 1; break; } }
      tmp[row + x] = v;
    }
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      let v = 0;
      for (let d = -r; d <= r; d++) { const yy = y + d; if (yy >= 0 && yy < h && tmp[yy * w + x]) { v = 1; break; } }
      out[y * w + x] = v;
    }
    return out;
  }
  // the WASM fileset for the page: the bundle would load the ES-module loader with a classic <script> (a syntax error
  // there) — so the loader is imported here, and the fileset gets no loader path (the bundle then takes ModuleFactory)
  // (set again before every task: the bundle deletes the global after use, and a second import() doesn't re-run the module)
  async function mpFileset(cdn) {
    globalThis.ModuleFactory = (await import(cdn + "/wasm/vision_wasm_module_internal.js")).default;
    return { wasmLoaderPath: "", wasmBinaryPath: new URL(cdn + "/wasm/vision_wasm_module_internal.wasm", location.href).href };
  }                // the sync segmentation averaging more than this → the worker
  // the mask's time smoothing: e = k·e + (1 − k)·a, but where it moves (|a − e| from motion[0] to motion[1]) the weight
  // of the old value falls to 0 — a moving head takes its new outline at once, a still edge stays calm (= the worker's
  // and deck_layer's server recipe)
  function emaStep(e, a, u, k, mo) {
    let fl = 0, soft = 0;
    const lo = mo ? +mo[0] : 0, span = mo ? Math.max(1e-6, +mo[1] - lo) : 1;
    for (let i = 0; i < a.length; i++) {
      let w = k;
      if (mo) { const x = Math.min(1, Math.max(0, (Math.abs(a[i] - e[i]) - lo) / span)); w = k * (1 - x * x * (3 - 2 * x)); }
      const v = w * e[i] + (1 - w) * a[i];
      fl += Math.abs(v - e[i]); e[i] = v;
      if (v > 0.05 && v < 0.95) soft++;
      u[i] = v * 255;
    }
    return { fl: 255 * fl / a.length, soft: soft / a.length };
  }
  const MP_MODEL = "/static/vendor/mediapipe/selfie_segmenter_landscape.tflite";
  const FS_LAYER = `#version 300 es
precision highp float;
in vec2 vUv; out vec4 o;
uniform sampler2D uTex; uniform float uTexH; uniform int uPremul;
void main() {
  float vy = min(vUv.y * 0.5, 0.5 - 0.5 / uTexH), va = max(0.5 + vUv.y * 0.5, 0.5 + 0.5 / uTexH);
  vec3 rgb = texture(uTex, vec2(vUv.x, vy)).rgb;
  float a = clamp(texture(uTex, vec2(vUv.x, va)).r, 0.0, 1.0);
  o = uPremul == 1 ? vec4(rgb * a, 1.0) : vec4(rgb, a);
}`;
  // a layer's blend → GL factors (src, dst) + premultiplied output: normal = mix(base, rgb, a); screen = rgb·a + base·(1 − rgb·a)
  // = 1 − (1 − base)(1 − rgb·a); add = base + rgb·a
  function blendOf(spec) {
    const b = spec && spec.blend;
    if (b === "screen") return { src: "ONE", dst: "ONE_MINUS_SRC_COLOR", premul: 1 };
    if (b === "add") return { src: "ONE", dst: "ONE", premul: 1 };
    return { src: "SRC_ALPHA", dst: "ONE_MINUS_SRC_ALPHA", premul: 0 };
  }

  function compile(gl, vs, fs) {
    const p = gl.createProgram();
    for (const [type, src] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]]) {
      const s = gl.createShader(type);
      gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error("shader: " + gl.getShaderInfoLog(s));
      gl.attachShader(p, s);
    }
    gl.bindAttribLocation(p, 0, "aPos");
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error("program: " + gl.getProgramInfoLog(p));
    const u = {};
    const n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
    for (let i = 0; i < n; i++) { const a = gl.getActiveUniform(p, i); u[a.name] = gl.getUniformLocation(p, a.name); }
    return { p, u };
  }
  function tex(gl, filter) {
    const t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  }
  const ident = Array.from({ length: 256 }, (_, i) => i);
  function lutData(color) {        // 256 × 4 rows: undo-Y, undo-C, eq-Y, eq-C (code values / 255)
    const d = new Float32Array(256 * 4), pre = (color && color.pre) || {}, eq = (color && color.eq) || {};
    const rows = [pre.y || ident, pre.c || ident, eq.y || ident, eq.c || ident];
    rows.forEach((r, j) => { for (let i = 0; i < 256; i++) d[j * 256 + i] = r[i] / 255; });
    return d;
  }
  const modeOf = c => !c || c.mode === "none" ? 0 : c.mode === "diff" ? 2 : 1;
  const now = () => performance.now();
  // the next range's video starts this much (source seconds) before the joint, muted and unseen, its rate slaved to the
  // active clock — so at the cut it is already running (a cold play() takes ~100–150 ms to show its 2nd frame)
  const LEAD = 1.0;

  // ---------- the player ----------
  class LivePlayer extends EventTarget {
    constructor(stage, opts) {
      super();
      opts = opts || {};
      this.stage = stage;
      this.canvas = document.createElement("canvas");
      this.canvas.setAttribute("aria-hidden", "true");
      this.caps = document.createElement("div");
      this.caps.className = "caps";
      this.caps.style.cssText = "position:absolute;inset:0;pointer-events:none";
      stage.append(this.canvas, this.caps);
      const gl = this.gl = this.canvas.getContext("webgl2", { alpha: false, antialias: false, premultipliedAlpha: false,
        preserveDrawingBuffer: !!opts.preserve, powerPreference: "high-performance" });
      if (!gl) throw new Error("WebGL2 isn't available in this browser");
      this.pf = compile(gl, VS, FS_FOOT);
      this.pl = compile(gl, VS, FS_LAYER);
      this.ps = compile(gl, VS, FS_SPEAKER);
      this.mk = null;                  // the person matte (background decks): {seg, tex, ema, w, h, mt, …}
      // the segmenter sees a small copy of the frame (the model's own input is 256×144): measured in headless Chrome on
      // the 960×540 working copy, the full frame cost ~95 ms a frame (the 960×540 mask read back from the GPU), which
      // stalled the page to 6–7 fps; the mask is upscaled by the texture's linear filter and sharpened by the levels
      this.matteOpts = { mode: opts.matteMode || "sync", auto: opts.matteAuto !== false, delegate: opts.matteDelegate || "CPU",
        cdn: opts.matteCdn || LivePlayer.MP_CDN || MP_CDN, w: opts.matteW || 256, h: opts.matteH || 144,
        motion: opts.matteMotion, levels: opts.matteLevels, semantic: opts.matteSemantic !== false,
        semDelegate: opts.matteSemDelegate, semEvery: opts.matteSemEvery };
      this.matteLog = null;            // QA: [] → every drawn speaker frame with its mask
      const vb = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, vb);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, 0, 1, 0, 0, 1, 1, 1]), gl.STATIC_DRAW);
      this.vao = gl.createVertexArray();
      gl.bindVertexArray(this.vao);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
      this.lutTex = new Map();         // colour recipe → its table texture
      this.els = [this._mkVideo(), this._mkVideo()];
      this.active = 0;                 // index into els
      this.idx = 0;                    // the active range
      this.playing = false;
      this.hold = null;                // {t, wall}: the clock on the wall clock (end card / a range without a copy)
      this.layers = new Map();         // layer hash → {el, tex, spec}
      this.captions = null;
      this.stats = { drawn: 0, footFrames: 0, stalls: 0, waitMs: 0, seeks: [], switches: [], firstFrame: null, errors: [] };
      this.startLat = 0.15;           // s: a cold play() until its frames run (measured per lead, smoothed)
      this.trace = opts.trace ? [] : null;   // QA: every presented footage frame {w, ed, mt, t, el}
      this.drawLog = opts.trace ? [] : null; // QA: every footage frame drawn on the canvas {w, el, mt, t}
      this._t = 0;
      this._raf = 0;
      this._loop = this._loop.bind(this);
    }

    _mkVideo() {
      const v = document.createElement("video");
      v.preload = "auto"; v.playsInline = true; v.muted = true; v.crossOrigin = "anonymous";
      v.style.display = "none";
      this.stage.append(v);
      const gl = this.gl, t = tex(gl, gl.LINEAR);
      const s = { el: v, tex: t, url: null, copy: null, fresh: false, mt: null, wait0: null, uploaded: false };
      const onFrame = (_n, md) => {
        s.mt = md.mediaTime; s.mtWall = now(); s.fresh = true;
        if (s !== this.els[this.active]) this._slave(s, md);
        if (s === this.els[this.active]) {
          this.stats.footFrames++;
          if (this.trace) {
            const r = this.ranges && this.ranges[this.idx];
            this.trace.push({ w: +now().toFixed(2), ed: +(md.expectedDisplayTime || 0).toFixed(2), mt: md.mediaTime,
              t: r ? +(r.t0 + md.mediaTime - r.src).toFixed(4) : null, el: this.active, idx: this.idx });
            if (this.trace.length > 20000) this.trace.splice(0, 5000);
          }
          this._onFootFrame(s, md);
        }
        v.requestVideoFrameCallback(onFrame);
      };
      if (v.requestVideoFrameCallback) v.requestVideoFrameCallback(onFrame);
      v.addEventListener("waiting", () => { if (s === this.els[this.active] && this.playing && !this.hold) { this.stats.stalls++; s.wait0 = now(); this._emit("stall", {}); } });
      v.addEventListener("playing", () => { if (s.wait0 != null) { this.stats.waitMs += now() - s.wait0; s.wait0 = null; } });
      v.addEventListener("error", () => { this.stats.errors.push(String(v.error && v.error.message)); this._emit("error", { error: v.error }); });
      return s;
    }

    _emit(type, detail) { this.dispatchEvent(new CustomEvent(type, { detail })); }

    get duration() { return this.scene ? +this.scene.total : 0; }
    get paused() { return !this.playing; }
    get currentTime() { return this._clock(); }

    // ---------- the scene ----------
    async load(scene) {
      this.scene = scene;
      this._prepare(scene);
      this.canvas.width = scene.W; this.canvas.height = scene.H;
      this._setupCaptions(scene, null);
      await this.seek(0);
      if (!this._raf) this._raf = requestAnimationFrame(this._loop);
      return this;
    }

    _prepare(scene) {
      const f = scene.footage || {};
      this.ranges = (f.ranges || []).map(r => ({ ...r, copyObj: r.copy == null ? null : f.copies[r.copy] }));
      this.copies = f.copies || [];
      this.fps = +scene.fps || 30;
      this.holdFrom = f.hold_from == null ? Infinity : +f.hold_from;
      this.end = +f.end || 0;
      this.tr = f.transform || null;
      this.byProp = groupKeys(this.tr);
      const gl = this.gl, used = new Set();
      for (const c of this.copies) {                 // one table texture per colour recipe (a colour edit: a new one)
        const key = JSON.stringify([c.color && c.color.mode, c.color && c.color.pre, c.color && c.color.eq]);
        if (!this.lutTex.has(key)) {
          const t = tex(gl, gl.NEAREST);
          gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, 256, 4, 0, gl.RED, gl.FLOAT, lutData(c.color));
          this.lutTex.set(key, t);
        }
        c._lut = this.lutTex.get(key);
        used.add(key);
      }
      for (const [k, t] of this.lutTex) if (!used.has(k)) { gl.deleteTexture(t); this.lutTex.delete(k); }
      this._layerSpecs = (scene.layers || []).filter(l => l && l.url).sort((a, b) => (a.z || 0) - (b.z || 0));
      this.matte = scene.matte && (scene.matte.spans || []).length ? scene.matte : null;
      if (this.matte && !this.mk) this._initMatte();
    }

    // ---------- the person matte (deck placement "background") ----------
    _matteAt(t) {
      if (!this.matte) return false;
      for (const sp of this.matte.spans) if (t >= sp[0] && t < sp[1]) return true;
      return false;
    }
    async _initMatte() {
      const gl = this.gl, t0 = now(), o = this.matteOpts;
      const mk = this.mk = { seg: null, tex: tex(gl, gl.LINEAR), ema: null, w: 0, h: 0, mt: null, ready: false, ms: [], err: null,
        delegate: null, flick: [], soft: [], loadMs: null, worker: null, busy: false, id: 0, lag: [], texMt: null, u8: null };
      // "sync" (the default): the mask is made for the very frame being drawn, on this thread — paired, no lag (measured on
      // S4, 10-26: the worker's mask came ~51 ms after its frame and the moving head left its old outline over the slide);
      // "worker": off the main thread (app/static/live-matte-worker.js), for a machine where one segmentation is slow —
      // the player switches to it on its own when the sync segmentation averages over SLOW_MS
      if (o.mode === "worker" && typeof Worker !== "undefined" && typeof createImageBitmap === "function") {
        if (await this._startWorker(mk, t0)) return;
      }
      try {
        const vision = await import(o.cdn + "/vision_bundle.mjs");
        const files = await mpFileset(o.cdn);
        const make = delegate => vision.ImageSegmenter.createFromOptions(files, {
          baseOptions: { modelAssetPath: MP_MODEL, delegate }, runningMode: "VIDEO",
          outputCategoryMask: false, outputConfidenceMasks: true });
        try { mk.seg = await make(o.delegate); mk.delegate = o.delegate; }
        catch (e) { mk.seg = await make("CPU"); mk.delegate = "CPU"; }
        mk.loadMs = +(now() - t0).toFixed(1);
      } catch (e) { mk.err = String(e && e.message || e); this.stats.errors.push("matte: " + mk.err); }
      if (o.semantic && (this.matte || {}).semantic) this._startSem(mk);
    }
    // the semantic matte (round 3, measured on S4 against a BiRefNet-portrait reference): the multiclass model (hair, face,
    // body, clothes vs background) doesn't take the framed picture beside the head for a person and keeps the light
    // shoulders opaque — but it costs ~380 ms a frame on a CPU (headless Chrome), so it runs in a worker on the newest
    // frame; when its answer is for the drawn frame (paused, or a fast GPU) it IS the matte, otherwise it gates the
    // per-frame landscape mask: nothing outside its person (+2 texels), all of its person's core (−2 texels)
    async _startSem(mk) {
      const t0 = now(), gl = document.createElement("canvas").getContext("webgl2");
      let soft = false;
      try { const d = gl && gl.getExtension("WEBGL_debug_renderer_info"); soft = !!(d && /swiftshader|llvmpipe|software/i.test(gl.getParameter(d.UNMASKED_RENDERER_WEBGL))); } catch (e) { }
      const S = mk.sem = { worker: null, busy: false, data: null, w: 0, h: 0, mt: null, copy: null, n: 0, ms: [], loadMs: null, err: null,
        delegate: this.matteOpts.semDelegate || (soft ? "CPU" : "GPU") };
      try {
        const w = new Worker("/static/live-matte-worker.js?v=" + MP_VER + "s2", { type: "module" });
        await new Promise((res, rej) => {
          const tm = setTimeout(() => rej(new Error("the semantic matte worker didn't start in 30 s")), 30000);
          w.onmessage = ev => { const d = ev.data || {};
            if (d.type === "ready") { clearTimeout(tm); S.delegate = d.delegate; S.loadMs = +(now() - t0).toFixed(1); res(); }
            else if (d.type === "error") { clearTimeout(tm); rej(new Error(d.error)); } };
          w.onerror = e => { clearTimeout(tm); rej(new Error(e.message || "worker error")); };
          w.postMessage({ type: "init", cdn: this.matteOpts.cdn, model: MP_SEM, delegate: S.delegate, invert: true });
        });
        w.onmessage = ev => { const d = ev.data || {};
          S.busy = false;
          if (d.type === "error") { S.err = d.error; return; }
          if (d.type !== "mask" || !d.data) return;
          S.data = d.data; S.w = d.w; S.h = d.h; S.mt = d.mt; S.copy = d.copy; S.n++; S.ms.push(d.ms);
          if (S.ms.length > 300) S.ms.splice(0, 150);
          if (this.mk !== mk) return;
          if (this._semFast()) {                             // a fast semantic answer IS the matte (no per-frame model)
            this._maskTex(d.data, d.w, d.h);
            mk.u8 = d.data; mk.texMt = d.mt; mk.how = "semantic";
            mk.hows = mk.hows || {}; mk.hows.semantic = (mk.hows.semantic || 0) + 1;
          } else mk.mt = null;                               // re-combine the drawn frame with the new answer
        };
        S.worker = w;
      } catch (e) { S.err = String(e && e.message || e); this.stats.errors.push("semantic matte: " + S.err); }
    }
    // the semantic model answers within SEM_FAST_MS (a GPU: measured 63–82 ms on an Intel UHD iGPU in Chrome, against
    // ~380–780 ms on a CPU): its answer alone is the matte, 2–3 frames behind — measured on S4 0.35–0.46 % leak, 3.0–3.4 %
    // loss, better than the per-frame landscape mask gated by it, and the main thread runs no model at all
    _semFast() {
      const S = this.mk && this.mk.sem;
      if (!S || !S.worker || S.ms.length < 5) return false;
      const a = S.ms.slice(-10).sort((x, y) => x - y), med = a[Math.floor(a.length / 2)];
      S.fast = S.fast ? med <= 2 * SEM_FAST_MS : med <= SEM_FAST_MS;   // (sticky: a slow moment doesn't flip the mode back)
      return S.fast;
    }
    _feedSem(s, fmt) {
      const S = this.mk && this.mk.sem;
      if (!S || !S.worker || S.busy || (S.mt === fmt && S.copy === s.copy)) return;
      // while playing, at most one request per SEM_EVERY_MS: on a weak GPU the model and the page's own drawing share it
      // (measured, Intel UHD: back to back the footage fell to 18 fps; paused, at once)
      if (this.playing && S.sentAt && now() - S.sentAt < (this.matteOpts.semEvery != null ? this.matteOpts.semEvery : SEM_EVERY_MS)) return;
      S.busy = true; S.sentAt = now();
      const R = this._recipe(), id = Date.now();
      createImageBitmap(s.el, { resizeWidth: 256, resizeHeight: 256, resizeQuality: "medium" })
        .then(bm => S.worker.postMessage({ type: "frame", id, mt: fmt, copy: s.copy, bitmap: bm, ema: R.k, motion: R.motion }, [bm]))
        .catch(e => { S.busy = false; S.err = String(e && e.message || e); });
    }
    // the per-frame mask (e, w×h, 0–1) with the semantic answer → u (0–255); → how ("exact" | "gate" | "plain")
    _combine(e, w, h, u, fmt, copy) {
      const S = this.mk && this.mk.sem;
      const n = w * h;
      if (!S || !S.data || S.copy !== copy || S.mt == null || Math.abs(S.mt - fmt) > SEM_FRESH_S) {
        for (let i = 0; i < n; i++) u[i] = e[i] * 255;
        return "plain";
      }
      const sw = S.w, sh = S.h, sd = S.data, mk = this.mk;
      if (!mk.semBin || mk.semBin.length !== n) { mk.semBin = new Uint8Array(n); mk.semDil = new Uint8Array(n); mk.semEro = new Uint8Array(n); mk.semTmp = new Uint8Array(n); }
      const exact = Math.abs(S.mt - fmt) < 0.5 / this.fps;
      for (let y = 0; y < h; y++) {                       // the semantic mask on this grid (bilinear)
        const fy = Math.min(sh - 1, Math.max(0, (y + 0.5) * sh / h - 0.5)), y0 = Math.floor(fy), y1 = Math.min(sh - 1, y0 + 1), ty = fy - y0;
        for (let x = 0; x < w; x++) {
          const fx = Math.min(sw - 1, Math.max(0, (x + 0.5) * sw / w - 0.5)), x0 = Math.floor(fx), x1 = Math.min(sw - 1, x0 + 1), tx = fx - x0;
          const v = (sd[y0 * sw + x0] * (1 - tx) + sd[y0 * sw + x1] * tx) * (1 - ty) + (sd[y1 * sw + x0] * (1 - tx) + sd[y1 * sw + x1] * tx) * ty;
          if (exact) u[y * w + x] = v;
          mk.semBin[y * w + x] = v > 127 ? 1 : 0;
        }
      }
      if (exact) {                       // + this frame's landscape mask where it is sure, near the person (a blown-out
        const near = dilate(mk.semBin, w, h, SEM_FILL_R, mk.semDil, mk.semTmp);       // cheek: = deck_matte.fill_holes)
        for (let i = 0; i < n; i++) if (near[i] && e[i] >= SEM_FILL_MIN && e[i] * 255 > u[i]) u[i] = e[i] * 255;
        return "exact";
      }
      const r = SEM_GATE_R, bin = mk.semBin, dil = dilate(bin, w, h, r, mk.semDil, mk.semTmp);
      for (let i = 0; i < n; i++) mk.semTmp[i] = 1 - bin[i];
      const bgd = dilate(mk.semTmp, w, h, r, mk.semEro, new Uint8Array(n));     // the background grown: its complement is the
      for (let i = 0; i < n; i++) u[i] = !bgd[i] ? 255 : dil[i] ? e[i] * 255 : 0;  // person's core (eroded) — opaque
      return "gate";
    }
    async _startWorker(mk, t0) {
      let w = null;
      try {
        w = new Worker("/static/live-matte-worker.js?v=" + MP_VER, { type: "module" });
        await new Promise((res, rej) => {
          const tm = setTimeout(() => rej(new Error("the matte worker didn't start in 30 s")), 30000);
          w.onmessage = ev => { const d = ev.data || {};
            if (d.type === "ready") { clearTimeout(tm); mk.delegate = d.delegate; mk.loadMs = +(now() - t0).toFixed(1); res(); }
            else if (d.type === "error") { clearTimeout(tm); rej(new Error(d.error)); } };
          w.onerror = e => { clearTimeout(tm); rej(new Error(e.message || "worker error")); };
          w.postMessage({ type: "init", cdn: this.matteOpts.cdn, model: MP_MODEL, delegate: this.matteOpts.delegate });
        });
        w.onmessage = ev => this._onMask(ev.data || {});
        mk.worker = w; mk.busy = false; mk.jump = true;
        if (!mk.seg) mk.seg = true;
        return true;
      } catch (e) {
        if (w) w.terminate();
        this.stats.errors.push("matte worker: " + (e && e.message || e) + " — on the main thread");
        return false;
      }
    }
    _recipe() {                      // the scene's matte recipe (= deck_layer.MATTE_RECIPE: the server's final render uses it too)
      const m = this.matte || {}, o = this.matteOpts;
      return { k: m.ema != null ? +m.ema : 0.5, motion: o.motion !== undefined ? o.motion : (m.motion || null),
        levels: o.levels || m.levels || [0.35, 0.75] };
    }
    _onMask(d) {                     // the worker's answer → the mask texture
      const mk = this.mk;
      if (!mk) return;
      mk.busy = false;
      if (d.type === "error") { mk.err = d.error; return; }
      if (d.type !== "mask" || !d.data) return;
      const n = d.w * d.h;                     // the semantic gate here too (the worker's mask is the per-frame one)
      if (!mk.wf || mk.wf.length !== n) mk.wf = new Float32Array(n);
      for (let i = 0; i < n; i++) mk.wf[i] = d.data[i] / 255;
      const u = new Uint8Array(n);
      mk.how = this._combine(mk.wf, d.w, d.h, u, d.mt, d.copy);
      mk.hows = mk.hows || {}; mk.hows[mk.how] = (mk.hows[mk.how] || 0) + 1;
      this._maskTex(u, d.w, d.h);
      mk.texMt = d.mt; mk.u8 = u; mk.n = (mk.n || 0) + 1;
      mk.ms.push(d.ms); mk.flick.push(d.flick); mk.soft.push(d.soft); mk.lag.push(now() - mk.sent);
      if (mk.ms.length > 600) for (const a of [mk.ms, mk.flick, mk.soft, mk.lag]) a.splice(0, 300);
    }
    _maskTex(u, w, h) {
      const gl = this.gl, mk = this.mk;
      gl.bindTexture(gl.TEXTURE_2D, mk.tex);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8, w, h, 0, gl.RED, gl.UNSIGNED_BYTE, u);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
      mk.w = w; mk.h = h; mk.ready = true;
    }
    _updateMask(s) {                 // a new footage frame → the segmenter → (motion-aware) EMA → the mask texture
      const mk = this.mk;
      if (!mk || !mk.seg || s.el.readyState < 2 || s.el.seeking) return;
      const fmt = s.mt != null ? s.mt : s.el.currentTime, key = s.copy + "@" + fmt;
      if (key === mk.mt) return;
      if (this._semFast()) { mk.mt = key; this._feedSem(s, fmt); return; }      // (the semantic answer is the matte)
      const R = this._recipe();
      if (mk.worker) {                         // one frame in flight; the next new frame goes when it answers
        this._feedSem(s, fmt);
        if (mk.busy) return;
        mk.mt = key; mk.busy = true; mk.sent = now();
        if (mk.jump) { mk.worker.postMessage({ type: "reset" }); mk.jump = false; }
        const id = ++mk.id;
        createImageBitmap(s.el, { resizeWidth: this.matteOpts.w, resizeHeight: this.matteOpts.h, resizeQuality: "medium" })
          .then(bm => mk.worker.postMessage({ type: "frame", id, mt: fmt, copy: s.copy, bitmap: bm, ema: R.k, motion: R.motion }, [bm]))
          .catch(e => { mk.busy = false; mk.err = String(e && e.message || e); });
        return;
      }
      mk.mt = key;
      const t0 = now();
      try {
        if (!mk.cv) { mk.cv = document.createElement("canvas"); mk.cv.width = this.matteOpts.w; mk.cv.height = this.matteOpts.h;
          mk.cx = mk.cv.getContext("2d"); }
        mk.cx.drawImage(s.el, 0, 0, mk.cv.width, mk.cv.height);
        mk.seg.segmentForVideo(mk.cv, Math.max(mk.ts ? mk.ts + 1 : 0, now()), res => {
          mk.ts = now();
          const m = res.confidenceMasks && res.confidenceMasks[0];
          if (!m) return;
          const a = m.getAsFloat32Array(), w = m.width, h = m.height, n = w * h;
          if (!mk.ema || mk.ema.length !== n || mk.jump) { mk.ema = new Float32Array(a); mk.u8 = new Uint8Array(n); mk.jump = false; }
          const st = emaStep(mk.ema, a, mk.u8, R.k, R.motion);
          mk.how = this._combine(mk.ema, w, h, mk.u8, fmt, s.copy);
          mk.hows = mk.hows || {}; mk.hows[mk.how] = (mk.hows[mk.how] || 0) + 1;
          this._maskTex(mk.u8, w, h);
          mk.texMt = fmt;
          mk.flick.push(st.fl); mk.soft.push(st.soft);
          if (mk.flick.length > 600) { mk.flick.splice(0, 300); mk.soft.splice(0, 300); }
        });
      } catch (e) { mk.err = String(e && e.message || e); }
      mk.ms.push(now() - t0);
      if (mk.ms.length > 600) mk.ms.splice(0, 300);
      mk.n = (mk.n || 0) + 1;
      this._feedSem(s, fmt);
      if (this.playing) { mk.playMs = mk.playMs || []; mk.playMs.push(now() - t0); if (mk.playMs.length > 60) mk.playMs.shift(); }
      if (this.matteOpts.auto && (mk.playMs || []).length >= 30 && !mk.switching) {   // too slow here (while playing): off the main thread
        const recent = mk.playMs.slice(-30), avg = recent.reduce((x, y) => x + y, 0) / recent.length;
        if (avg > SLOW_MS && typeof Worker !== "undefined") {
          mk.switching = true;
          this._startWorker(mk, now()).then(ok => { mk.switching = !ok ? "failed" : false; if (ok) this.stats.matteSwitched = +avg.toFixed(1); });
        }
      }
    }
    _drawSpeaker(s, r, t) {
      const mk = this.mk;
      if (!mk || !mk.seg) return false;
      this._updateMask(s);
      if (!mk.ready || !r || !r.copyObj || !s.uploaded) return false;
      if (this.matteLog && s.mt !== this._loggedMt && mk.u8) {     // QA: the drawn frame, the frame its mask was made from, the mask
        this._loggedMt = s.mt;
        this.matteLog.push({ mt: s.mt, mmt: mk.texMt, w: mk.w, h: mk.h, data: new Uint8Array(mk.u8) });
      }
      const gl = this.gl, P = this.ps, copy = r.copyObj;
      const fm = footageMatrix(this.tr, this.byProp, t, copy.w, copy.h), m = fm.m, lv = this._recipe().levels;
      gl.useProgram(P.p);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, s.tex); gl.uniform1i(P.u.uVid, 0);
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, copy._lut); gl.uniform1i(P.u.uLut, 1);
      gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, mk.tex); gl.uniform1i(P.u.uMask, 2);
      gl.activeTexture(gl.TEXTURE0);
      gl.uniform1i(P.u.uMode, r.nocolor ? 0 : modeOf(copy.color));
      const bg = (this.tr && this.tr.bg) || [0, 0, 0];
      gl.uniform3f(P.u.uBg, bg[0], bg[1], bg[2]);
      gl.uniformMatrix3fv(P.u.uM, false, [m[0], m[3], 0, m[1], m[4], 0, m[2], m[5], 1]);
      gl.uniform1f(P.u.uAlpha, 1); gl.uniform1f(P.u.uOpacity, fm.opacity);
      gl.uniform2f(P.u.uLv, lv[0], lv[1]);
      gl.uniform2f(P.u.uMpx, 1 / Math.max(1, mk.w), 1 / Math.max(1, mk.h));
      gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      this.stats.speaker = (this.stats.speaker || 0) + 1;
      return true;
    }
    matteStats() {                   // QA: the segmenter's cost and the matte's steadiness
      const mk = this.mk;
      if (!mk) return null;
      const q = xs => { const a = [...xs].sort((x, y) => x - y); return a.length ? +a[Math.min(a.length - 1, Math.floor(a.length * 0.95))].toFixed(2) : null; };
      const mean = xs => xs.length ? +(xs.reduce((x, y) => x + y, 0) / xs.length).toFixed(3) : null;
      const S = mk.sem;
      return { sem: S ? { delegate: S.delegate, load_ms: S.loadMs, answers: S.n, ms_mean: mean(S.ms), ms_p95: q(S.ms), err: S.err } : null,
        hows: mk.hows || null, worker: !!mk.worker, switched_at_ms: this.stats.matteSwitched || null, delegate: mk.delegate, load_ms: mk.loadMs, err: mk.err, frames: mk.n || 0, ms_mean: mean(mk.ms),
        ms_p95: q(mk.ms), lag_mean: mean(mk.lag), lag_p95: q(mk.lag),
        w: mk.w, h: mk.h, flicker_mean: mean(mk.flick.slice(1)), soft_frac: mean(mk.soft), speaker_draws: this.stats.speaker || 0 };
    }

    _setupCaptions(scene, old) {
      const part = scene.captions;
      if (!root.LiveCaptions || !part) { if (this.captions && !part) { try { this.captions.destroy(); } catch (e) { } this.captions = null; } return; }
      const same = old && JSON.stringify(old.captions) === JSON.stringify(part);
      if (same && this.captions) return;
      try {
        if (!this.captions) this.captions = new root.LiveCaptions(this.caps, { width: scene.W, height: scene.H });
        this.captions.load(part);
      } catch (e) { this.stats.errors.push("captions: " + e); }
    }

    // Apply a new scene without reloading unchanged footage: copies by id (a new token alone is the same file), the
    // playhead stays at its output time (or opts.time), layers kept by hash, colour / transform just swapped.
    async update(scene, opts) {
      const old = this.scene, t = opts && opts.time != null ? +opts.time : this._clock(), was = this.playing;
      this.scene = scene;
      this._prepare(scene);
      if (scene.W !== old.W || scene.H !== old.H) { this.canvas.width = scene.W; this.canvas.height = scene.H; }
      this._setupCaptions(scene, old);
      for (const [h, L] of this.layers) if (!this._layerSpecs.some(s => s.hash === h)) this._dropLayer(h);
      const tt = Math.min(t, this.duration);
      const r = this._rangeAt(tt);
      const s = this.els[this.active];
      const want = r && r.copyObj ? r.src + (tt - r.t0) : null;
      if (r && r.copyObj && s.copy === r.copyObj.id && tt < this.holdFrom && Math.abs((s.el.currentTime || 0) - want) < 1.5 / this.fps) {
        this.idx = this.ranges.indexOf(r);          // the same file at the same place: nothing to reload or seek
        this._preroll();
        this._emit("updated", { reloaded: false, t: tt });
        return;
      }
      this._emit("updated", { reloaded: true, t: tt });
      await this.seek(tt);
      if (was && !this.playing) this.play();
    }

    // ---------- the clock ----------
    _rangeAt(t) {                    // the range playing at output t (past the end: the last one)
      const rs = this.ranges;
      for (let i = 0; i < rs.length; i++) if (t < rs[i].t1 - 1e-6) return rs[i];
      return rs[rs.length - 1] || null;
    }
    _clock() {
      if (this.hold) return Math.min(this.duration, this.hold.t + (this.playing ? (now() - this.hold.wall) / 1000 : 0));
      const s = this.els[this.active], r = this.ranges[this.idx];
      if (!r || !s.el) return this._t;
      if (this.playing && s.mt != null && !s.el.seeking) return r.t0 + (s.mt - r.src);
      return r.t0 + ((s.el.currentTime || 0) - r.src);
    }

    _onFootFrame(s, md) {
      if (!this.playing || this.hold) return;
      const r = this.ranges[this.idx];
      if (!r) return;
      const endSrc = r.src + (r.t1 - r.t0), L = this._lead, o = this.els[1 - this.active];
      if (L && L.early && !L.started && endSrc - md.mediaTime <= L.nx.src - L.at + this.startLat && o.el.readyState >= 2 && !o.el.seeking) {
        L.started = true; L.startedAt = now();          // early by the measured start latency of a play()
        o.el.muted = true; o.el.playbackRate = 1;
        o.el.play().catch(e => this.stats.errors.push("lead: " + e));
      }
      if (md.mediaTime >= endSrc - 0.5 / this.fps) this._advance();
    }

    _slave(s, md) {                  // the leading standby: where it should be now, by the active clock → its rate
      const L = this._lead, a = this.els[this.active], r = this.ranges[this.idx];
      if (!L || !L.started || !this.playing || !r || a.mt == null || s.el.paused) return;
      if (L.first == null) { L.first = now() - L.startedAt; this.startLat = 0.7 * this.startLat + 0.3 * Math.min(0.4, L.first / 1000); }
      const aNow = a.mt + (now() - (a.mtWall || now())) / 1000, endSrc = r.src + (r.t1 - r.t0);
      const lag = (L.nx.src - (endSrc - aNow)) - md.mediaTime;          // + = behind
      L.lag = lag;
      s.el.playbackRate = Math.abs(lag) < 0.3 / this.fps ? 1 : Math.min(2, Math.max(0.5, 1 + lag / 0.2));
    }

    _advance() {                     // the active range ends: the pre-rolled one takes over (or the hold)
      const i = this.idx, r = this.ranges[i], nx = this.ranges[i + 1], s = this.els[this.active];
      if (!nx || r.t1 >= this.holdFrom - 0.5 / this.fps) { this._enterHold(r.t1); return; }
      const w0 = now(), o = this.els[1 - this.active];
      this.idx = i + 1;
      if (nx.cont && nx.copyObj && s.copy === nx.copyObj.id) {   // the same file goes on (a clip's edge in the copy)
        this._lead = null; this.stats.switches.push({ t: nx.t0, at: w0, cont: true }); this._preroll(); return;
      }
      if (!nx.copyObj) { this._enterHold(nx.t0, true); s.el.pause(); return; }
      const L = this._lead, om = o.mt != null ? o.mt : o.el.currentTime;
      const running = !!(L && L.started && !o.el.paused && o.copy === nx.copyObj.id && Math.abs(om - nx.src) < 1.5 / this.fps);
      const ready = running || (o.copy === nx.copyObj.id && Math.abs(o.el.currentTime - nx.src) < 1 / this.fps && o.el.readyState >= 2);
      if (!ready) this._load(o, nx.copyObj, nx.src);
      this._lead = null;
      this.active = 1 - this.active;
      if (!running) o.mt = nx.src;
      o.el.muted = false; o.el.volume = 1; o.el.playbackRate = 1;
      const fade = nx.xfade || nx.afade;
      if (fade) {                    // smooth / dissolve: the previous source plays on under the joint, then stops
        this._fade = { from: s, t0: nx.t0, d: nx.xfade || 0, a: nx.afade || 0 };
        setTimeout(() => { if (this.els[this.active] !== s) { s.el.pause(); s.el.muted = true; } this._fade = null; this._preroll(); },
          1000 * Math.max(nx.xfade || 0, nx.afade || 0) + 30);
      } else { s.el.pause(); s.el.muted = true; }
      if (!running) o.el.play().catch(e => this.stats.errors.push("play: " + e));
      this.stats.switches.push({ t: nx.t0, at: w0, ready, running, lag: L && L.lag != null ? +(L.lag * 1000).toFixed(1) : null,
        copy: nx.copyObj.id, src: nx.src, off_ms: +((om - nx.src) * 1000).toFixed(1) });
      this._emit("switch", { t: nx.t0, ready });
      if (!fade) this._preroll();
    }

    _enterHold(t, gap) {
      const s = this.els[this.active];
      if (!gap) { s.el.pause(); }
      this.hold = { t: Math.max(t, this._t), wall: now(), gap: !!gap };
    }

    _preroll() {                     // the other element: on the next range's file, paused LEAD before its start
      const nx = this.ranges[this.idx + 1], o = this.els[1 - this.active], r = this.ranges[this.idx], s = this.els[this.active];
      if (!nx || !nx.copyObj || this._fade || nx.cont) { this._lead = null; return; }
      const left = r ? r.src + (r.t1 - r.t0) - (s.el.currentTime || 0) : 0;
      const at = left > LEAD + 0.3 && nx.src - LEAD >= 0 ? nx.src - LEAD : nx.src;
      this._lead = { nx, at, early: at < nx.src - 1e-6, started: false };
      if (o.copy === nx.copyObj.id && o.el.paused && Math.abs(o.el.currentTime - at) < 0.5 / this.fps) return;
      this._load(o, nx.copyObj, at);
    }

    _load(s, copy, at) {
      if (s.copy !== copy.id) { s.copy = copy.id; s.url = copy.url; s.el.src = copy.url; s.uploaded = false; }
      s.el.pause(); s.el.muted = true;
      try { s.el.currentTime = at; } catch (e) { s.el.addEventListener("loadedmetadata", () => { s.el.currentTime = at; }, { once: true }); }
      s.mt = null;
    }

    _seeked(s) {
      return new Promise(res => {
        const ok = () => s.el.readyState >= 2 && !s.el.seeking;
        if (ok()) return res();
        const f = () => { if (ok()) { s.el.removeEventListener("seeked", f); s.el.removeEventListener("canplay", f); s.el.removeEventListener("loadeddata", f); res(); } };
        s.el.addEventListener("seeked", f); s.el.addEventListener("canplay", f); s.el.addEventListener("loadeddata", f);
      });
    }

    async seek(t) {
      const t0 = now(), seq = this._seq = (this._seq || 0) + 1, was = this.playing;
      t = Math.max(0, Math.min(+t || 0, this.duration));
      this._t = t;
      this.hold = null; this._fade = null;
      if (this.mk) this.mk.jump = true;          // the matte's time smoothing starts over after a jump
      if (t >= this.holdFrom - 1e-6) {            // the end card: the last frame of the footage, the clock on the wall
        const r = this.ranges[this.ranges.length - 1];
        this.idx = this.ranges.length - 1;
        const s = this.els[this.active];
        if (r && r.copyObj) { this._load(s, r.copyObj, Math.max(r.src, r.src + (r.t1 - r.t0) - 0.5 / this.fps)); await this._seeked(s); }
        this.hold = { t, wall: now() };
        s.fresh = true;
      } else {
        const r = this._rangeAt(t), i = this.ranges.indexOf(r);
        this.idx = i;
        if (!r.copyObj) { this.hold = { t, wall: now(), gap: true }; }
        else {
          const want = r.src + (t - r.t0);
          let k = this.active;
          if (this.els[k].copy !== r.copyObj.id && this.els[1 - k].copy === r.copyObj.id) k = 1 - k;
          const s = this.els[k], o = this.els[1 - k];
          if (o !== s) { o.el.pause(); o.el.muted = true; }
          this.active = k;
          this._load(s, r.copyObj, want);
          await this._seeked(s);
          if (seq !== this._seq) return;
          s.mt = want; s.fresh = true;
          s.el.muted = false;
        }
      }
      if (seq !== this._seq) return;
      this._draw(true);
      this._preroll();
      if (was) await this.play();
      const ms = now() - t0;
      this.stats.seeks.push({ t, ms: +ms.toFixed(1) });
      this._emit("seeked", { t, ms });
    }

    async play() {
      this.playing = true;
      this._emit("play", {});
      if (this.hold) { this.hold.wall = now(); return; }
      const s = this.els[this.active];
      s.el.muted = false;
      try { await s.el.play(); } catch (e) {
        s.el.muted = true;                       // no user gesture yet: play muted (the page's Play button unmutes)
        try { await s.el.play(); } catch (e2) { this.stats.errors.push("play: " + e2); }
      }
      this._preroll();
    }

    pause() {
      if (this.hold) this.hold = { t: this._clock(), wall: now(), gap: this.hold.gap };
      this.playing = false;
      this._lead = null;
      for (const s of this.els) { s.el.pause(); s.el.playbackRate = 1; }
      for (const [, L] of this.layers) L.el.pause();
      this._emit("pause", {});
    }

    // ---------- layers (stacked alpha) ----------
    _layerTime(spec, t) {             // the clip's own time at output t (+¼ frame: never ON a frame boundary)
      let lt = t - spec.t0;
      const lp = spec.loop;
      if (lp && lp.still) lt = Math.min(lt, lp.intro);          // egy kockás „kör”: a beúszás után az utolsó kocka áll (L2)
      else if (lp && lp.period > 0 && lt >= lp.intro + lp.period) lt = lp.intro + ((lt - lp.intro) % lp.period);
      return lt + 0.25 / this.fps;
    }
    _ensureLayer(spec) {
      let L = this.layers.get(spec.hash);
      if (L) { L.spec = spec; return L; }
      const v = document.createElement("video");
      v.preload = "auto"; v.playsInline = true; v.muted = true; v.crossOrigin = "anonymous"; v.style.display = "none";
      v.src = spec.url;
      this.stage.append(v);
      L = { el: v, tex: tex(this.gl, this.gl.LINEAR), spec, mt: null, ok: false };
      const f = (_n, md) => { L.mt = md.mediaTime; L.mtWall = now(); L.ok = true; v.requestVideoFrameCallback(f); };
      if (v.requestVideoFrameCallback) v.requestVideoFrameCallback(f);
      v.addEventListener("seeked", () => { L.ok = true; L.mt = v.currentTime; });
      this.layers.set(spec.hash, L);
      return L;
    }
    _dropLayer(h) {
      const L = this.layers.get(h);
      if (!L) return;
      L.el.pause(); L.el.removeAttribute("src"); L.el.load(); L.el.remove();
      this.gl.deleteTexture(L.tex);
      this.layers.delete(h);
    }
    _syncLayers(t) {
      const fps = this.fps, out = [];
      for (const spec of this._layerSpecs || []) {
        const near = t >= spec.t0 - 2 && t < spec.t1 + 1;
        if (!near) { if (this.layers.has(spec.hash) && (t < spec.t0 - 6 || t > spec.t1 + 4)) this._dropLayer(spec.hash); continue; }
        const L = this._ensureLayer(spec);
        const lt = this._layerTime(spec, Math.max(t, spec.t0));
        const on = t >= spec.t0 - 0.5 / fps && t < spec.t1 - 0.5 / fps;
        const cur = L.el.seeking ? null : L.el.currentTime;
        const lp = spec.loop;
        const held = lp && lp.still && t - spec.t0 >= lp.intro;  // (álló kocka: nem játszik, nem tekerget)
        // (a zárókártya alatt — hold: a felvétel áll, az óra a falióra — a rétegek ugyanúgy játszanak: ha itt minden kockánál
        // tekernénk, a tekerés alatt a klip kiesik egy-egy kockára, és a kártya villog; 10-26, a Kommandant látta)
        if (this.playing && on && !held) {
          if (L.el.paused) { L.el.currentTime = lt; L.el.play().catch(() => { }); }
          else if (cur != null && Math.abs(cur - lt) > 3 / fps) L.el.currentTime = lt;
          else if (lp && !lp.still && lp.period > 0 && cur >= lp.intro + lp.period - 0.5 / fps) L.el.currentTime = lt;
          else if (L.mt != null) {        // slaved to the footage clock: its presented frame vs where it should be
            const lag = lt - (L.mt + (now() - (L.mtWall || now())) / 1000);
            L.el.playbackRate = Math.abs(lag) < 0.3 / fps ? 1 : Math.min(2, Math.max(0.5, 1 + lag / 0.2));
          }
        } else {
          L.el.playbackRate = 1;
          if (!L.el.paused) L.el.pause();
          if (cur != null && Math.abs(cur - lt) > 0.5 / fps && !L.el.seeking) L.el.currentTime = lt;
        }
        if (on && (L.el.readyState >= 2 || L.uploaded)) out.push(L);   // tekerés közben az utolsó kirajzolt kockája marad
      }
      return out;
    }

    // ---------- drawing ----------
    _upload(s) {
      const gl = this.gl;
      if (s.el.readyState < 2) return false;
      gl.bindTexture(gl.TEXTURE_2D, s.tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, s.el);
      s.uploaded = true; s.fresh = false;
      return true;
    }
    _drawFoot(s, copy, t, alpha, range) {
      const gl = this.gl, P = this.pf;
      if (!copy || !s.uploaded) return;
      const fm = footageMatrix(this.tr, this.byProp, t, copy.w, copy.h), m = fm.m;
      gl.useProgram(P.p);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, s.tex); gl.uniform1i(P.u.uVid, 0);
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, copy._lut); gl.uniform1i(P.u.uLut, 1);
      gl.uniform1i(P.u.uMode, range && range.nocolor ? 0 : modeOf(copy.color));
      const bg = (this.tr && this.tr.bg) || [0, 0, 0];
      gl.uniform3f(P.u.uBg, bg[0], bg[1], bg[2]);
      gl.uniformMatrix3fv(P.u.uM, false, [m[0], m[3], 0, m[1], m[4], 0, m[2], m[5], 1]);
      gl.uniform1f(P.u.uAlpha, alpha); gl.uniform1f(P.u.uOpacity, fm.opacity);
      if (alpha < 1) { gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA); } else gl.disable(gl.BLEND);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }
    _draw(force) {
      const gl = this.gl;
      if (!this.scene) return;
      const t = this._clock();
      this._t = t;
      gl.viewport(0, 0, this.canvas.width, this.canvas.height);
      gl.bindVertexArray(this.vao);
      const s = this.els[this.active], r = this.ranges[this.idx];
      if (this.hold && this.hold.gap) {
        const bg = (this.tr && this.tr.bg) || [0, 0, 0];
        gl.clearColor(bg[0], bg[1], bg[2], 1); gl.clear(gl.COLOR_BUFFER_BIT);
        if (this.playing && r && t >= r.t1 - 1e-6) { this.hold = null; this._advanceFromGap(); }
      } else {
        if (s.fresh || force || this.playing) this._upload(s);
        const fd = this._fade;
        if (fd && fd.d > 0 && t < fd.t0 + fd.d) {             // dissolve: the previous source under, the new one over
          if (fd.from.fresh || this.playing) this._upload(fd.from);
          const fr = this.ranges[this.idx - 1];
          this._drawFoot(fd.from, fr && fr.copyObj, t, 1, fr);
          this._drawFoot(s, r && r.copyObj, t, Math.max(0, Math.min(1, (t - fd.t0) / fd.d)), r);
        } else this._drawFoot(s, r && r.copyObj, t, 1, r);
        if (this.drawLog && s.mt !== this._lastDrawn) {   // QA: each new footage frame that reached the canvas
          this._lastDrawn = s.mt;
          this.drawLog.push({ w: +now().toFixed(2), el: this.active, mt: s.mt, t: r ? +(r.t0 + s.mt - r.src).toFixed(4) : null });
          if (this.drawLog.length > 20000) this.drawLog.splice(0, 5000);
        }
        if (fd && fd.a > 0) {                                   // the sound crosses over (tri: linear)
          const k = Math.max(0, Math.min(1, (t - fd.t0) / fd.a));
          fd.from.el.volume = 1 - k; s.el.volume = k;
        }
      }
      const P = this.pl, matteOn = this._matteAt(t) && !(this.hold && this.hold.gap);
      let behind = false, spoke = false;
      for (const L of this._syncLayers(t)) {
        if (matteOn && behind && !spoke && !L.spec.behind) { this._drawSpeaker(s, r, t); spoke = true; }
        if (L.spec.behind) behind = true;
        gl.bindTexture(gl.TEXTURE_2D, L.tex);
        if (L.el.readyState >= 2 && !L.el.seeking) {
          // a paused clip on the same frame (a still: the logo, a deck's slide after its fade) is not uploaded again
          if (!(L.uploaded && L.el.paused && L.upAt === L.el.currentTime)) {
            gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, L.el);
            L.upAt = L.el.paused ? L.el.currentTime : null;
          }
          L.uploaded = true;
        } else this.stats.layerStale = (this.stats.layerStale || 0) + 1;
        if (!L.uploaded) continue;                               // (még egy kockája sincs: üres textúrát ne)
        gl.useProgram(P.p);
        gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, L.tex); gl.uniform1i(P.u.uTex, 0);
        gl.uniform1f(P.u.uTexH, L.el.videoHeight || 2 * this.scene.H);
        const bf = blendOf(L.spec);
        gl.uniform1i(P.u.uPremul, bf.premul);
        gl.enable(gl.BLEND); gl.blendFunc(gl[bf.src], gl[bf.dst]);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      }
      if (matteOn && behind && !spoke) this._drawSpeaker(s, r, t);
      gl.disable(gl.BLEND);
      if (this.captions) { try { this.captions.setTime(t); } catch (e) { } }
      this.stats.drawn++;
      if (this.stats.firstFrame == null && s.uploaded) { this.stats.firstFrame = now(); this._emit("firstframe", { at: this.stats.firstFrame }); }
      this._emit("time", { t });
    }
    _advanceFromGap() {
      const r = this.ranges[this.idx], nx = this.ranges[this.idx + 1];
      if (!nx) { this.hold = { t: r.t1, wall: now() }; return; }
      this.seek(nx.t0);
    }

    _loop() {
      this._raf = requestAnimationFrame(this._loop);
      if (this.playing && !this.hold) {                        // a frame drawn as late as the decoder: the rVFC flag or
        const s = this.els[this.active], r = this.ranges[this.idx];   // the clock past the range end (backup)
        if (r && s.el.currentTime >= r.src + (r.t1 - r.t0) - 0.25 / this.fps && !s.el.seeking) this._advance();
      }
      if (this.hold && this.playing && this._clock() >= this.duration - 1e-3) { this.pause(); this._emit("ended", {}); }
      this._draw(false);
    }

    grab() {                       // the canvas pixels (RGBA, top row first) — for the QA's fidelity check
      const gl = this.gl, w = this.canvas.width, h = this.canvas.height, px = new Uint8Array(w * h * 4);
      this._draw(true);
      gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, px);
      const out = new Uint8Array(w * h * 4);
      for (let y = 0; y < h; y++) out.set(px.subarray((h - 1 - y) * w * 4, (h - y) * w * 4), y * w * 4);
      return { w, h, data: out };
    }

    destroy() {
      cancelAnimationFrame(this._raf);
      for (const s of this.els) { s.el.pause(); s.el.removeAttribute("src"); s.el.load(); s.el.remove(); }
      for (const h of [...this.layers.keys()]) this._dropLayer(h);
      if (this.captions) try { this.captions.destroy(); } catch (e) { }
      this.canvas.remove(); this.caps.remove();
    }
  }

  // ---------- the test page (/live/<pid>) ----------
  const fmt = t => { t = Math.max(0, t || 0); const m = Math.floor(t / 60), s = Math.floor(t % 60); return m + ":" + String(s).padStart(2, "0"); };
  LivePlayer.page = async function (cfgEl) {
    const cfg = JSON.parse(cfgEl.textContent), q = new URLSearchParams(location.search);
    const stage = document.getElementById("lp-stage"), btn = document.getElementById("lp-play"),
      bar = document.getElementById("lp-seek"), time = document.getElementById("lp-time"), msg = document.getElementById("lp-msg");
    const p = new LivePlayer(stage, { preserve: q.has("qa"), trace: q.has("qa") });
    root.lp = p;
    let rev = null, dragging = false;
    const get = async () => { const r = await fetch(cfg.scene, { cache: "no-store" }); if (!r.ok) throw new Error(r.status + " " + (await r.text()).slice(0, 200)); return r.json(); };
    try {
      const scene = await get();
      rev = scene.rev;
      bar.max = String(scene.total);
      p.addEventListener("time", e => { if (!dragging) bar.value = String(e.detail.t); time.textContent = fmt(e.detail.t) + " / " + fmt(p.duration); });
      p.addEventListener("play", () => { btn.textContent = "Pause"; });
      p.addEventListener("pause", () => { btn.textContent = "Play"; });
      await p.load(scene);
      msg.textContent = (scene.warnings || []).join(" · ");
      if (q.has("t")) await p.seek(+q.get("t"));
      if (q.has("autoplay")) p.play();
    } catch (e) { msg.textContent = "The live preview couldn't start: " + e.message; return; }
    btn.addEventListener("click", () => { if (p.paused) p.play(); else p.pause(); });
    bar.addEventListener("input", () => { dragging = true; });
    bar.addEventListener("change", () => { dragging = false; p.seek(+bar.value); });
    document.addEventListener("keydown", e => { if (e.code === "Space" && e.target === document.body) { e.preventDefault(); btn.click(); } });
    // the version's scene changes (its own copy got ready, a layer clip got rendered): apply it — every second while a
    // layer is still being made (an edited graphic shows up as soon as it is ready), otherwise every 4 s
    const waiting = sc => (sc.layers || []).some(l => !l.url && (l.status === "queued" || l.status === "rendering"));
    let last = null;
    const poll = async () => {
      if (!document.hidden && !q.has("qa")) {
        try { const s = await get(); last = s; if (s.rev !== rev) { rev = s.rev; await p.update(s); } } catch (e) { }
      }
      setTimeout(poll, last && waiting(last) ? 1000 : 4000);
    };
    setTimeout(poll, 1000);
  };

  LivePlayer.MP_CDN = MP_CDN;
  LivePlayer.emaStep = emaStep;
  LivePlayer.mpFileset = mpFileset;
  LivePlayer.evalProp = evalProp;
  LivePlayer.blendOf = blendOf;
  LivePlayer.footageMatrix = footageMatrix;
  LivePlayer.groupKeys = groupKeys;
  root.LivePlayer = LivePlayer;
  if (typeof module !== "undefined" && module.exports) module.exports = { evalProp, footageMatrix, groupKeys, blendOf, emaStep };
})(typeof window !== "undefined" ? window : globalThis);
