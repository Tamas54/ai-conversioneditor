/* The live preview's person matte, off the main thread (app/static/live-player.js, deck placement "background").

   MediaPipe Tasks Vision ImageSegmenter (Apache-2.0; the selfie segmenter landscape model, vendored under
   /static/vendor/mediapipe/) on small frames (256×144 ImageBitmaps) sent by the player; the confidence mask is smoothed
   over time (EMA, reset after a jump) and sent back as 8-bit (transferred). One frame in flight: the player sends the
   next one only after the answer, so the mask rate adapts to the machine and the page's own frame rate never waits.

   in:  {type: "init", cdn, model, delegate} · {type: "frame", id, bitmap, ema} · {type: "reset"}
   out: {type: "ready", delegate, loadMs} · {type: "error", error} · {type: "mask", id, w, h, data, ms, flick, soft} */
let seg = null, ema = null, ts = 0, jump = true;

self.onmessage = async (ev) => {
  const m = ev.data || {};
  if (m.type === "init") {
    const t0 = performance.now();
    try {
      const vision = await import(m.cdn + "/vision_bundle.mjs");
      const files = await vision.FilesetResolver.forVisionTasks(m.cdn + "/wasm", true);   // the ES-module loader (no importScripts here)
      const make = delegate => vision.ImageSegmenter.createFromOptions(files, {
        baseOptions: { modelAssetPath: new URL(m.model, self.location.origin).href, delegate }, runningMode: "VIDEO",
        outputCategoryMask: false, outputConfidenceMasks: true });
      let delegate = m.delegate || "CPU";
      try { seg = await make(delegate); } catch (e) { delegate = "CPU"; seg = await make("CPU"); }
      self.postMessage({ type: "ready", delegate, loadMs: +(performance.now() - t0).toFixed(1) });
    } catch (e) { self.postMessage({ type: "error", error: String(e && e.message || e) }); }
    return;
  }
  if (m.type === "reset") { jump = true; return; }
  if (m.type !== "frame" || !seg) { if (m.bitmap) m.bitmap.close(); return; }
  const t0 = performance.now(), k = m.ema != null ? +m.ema : 0.5;
  let out = null;
  try {
    ts = Math.max(ts + 1, Math.round(performance.now()));
    seg.segmentForVideo(m.bitmap, ts, res => {
      const mk = res.confidenceMasks && res.confidenceMasks[0];
      if (!mk) return;
      const a = mk.getAsFloat32Array(), w = mk.width, h = mk.height, n = w * h;
      if (!ema || ema.length !== n || jump) { ema = new Float32Array(a); jump = false; }
      const u = new Uint8Array(n);
      let fl = 0, soft = 0;
      for (let i = 0; i < n; i++) {
        const v = k * ema[i] + (1 - k) * a[i];
        fl += Math.abs(v - ema[i]); ema[i] = v;
        if (v > 0.05 && v < 0.95) soft++;
        u[i] = v * 255;
      }
      out = { type: "mask", id: m.id, w, h, data: u, flick: 255 * fl / n, soft: soft / n };
    });
  } catch (e) { out = { type: "error", error: String(e && e.message || e), id: m.id }; }
  m.bitmap.close();
  if (!out) out = { type: "mask", id: m.id, w: 0, h: 0, data: null };
  out.ms = +(performance.now() - t0).toFixed(2);
  self.postMessage(out, out.data ? [out.data.buffer] : []);
};
