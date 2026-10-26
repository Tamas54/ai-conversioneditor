/* Videóprojekt előnézete (/preview/<projekt>.ecproj, app/routes/preview.py): a szakasz-gyorsítótáras HLS-lista —
   Safari/iOS natívan, máshol hls.js (cdnjs) —, alatta az idővonal a változások jelölőivel (kattintás: oda ugrik és
   játszik; Tab-bal is elérhető, a címke rámutatásra / fókuszra látszik) és a lejátszófejjel. A preview.json-t ~2 s-onként
   olvassa, amíg a szakaszok elkészülnek („x of y segments ready”), és kiírja a végleges render állapotát; ha kész,
   átváltható a végleges MP4-re (és vissza). A beállítás a lapba ágyazott JSON (#pv-cfg). */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const cfg = JSON.parse($("pv-cfg").textContent);
  const v = $("pv"), pl = $("pl"), bar = $("pv-bar"), marksEl = $("pv-marks"), head = $("pv-head"), timeEl = $("pv-time"),
    noteEl = $("pv-note"), which = $("pv-which"), segEl = $("pv-seg"), finEl = $("pv-final"), msgEl = $("pv-msg");
  const st = { dur: +cfg.duration || 0, markers: [], final: cfg.final || null, src: "", hls: null, pending: null,
    tries: 0, media: 0, ready: null, total: null, fails: 0 };

  // a szerver abszolút linket ad (PUBLIC_BASE_URL) — a lap saját eredetéről kérjük, mint a régi lap a /files/ linket
  const local = u => { const m = String(u || "").match(/\/(?:api|files)\/.*/); return m ? m[0] : u; };
  const two = n => String(n).padStart(2, "0");
  const fmt = s => {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60;
    return (h ? h + ":" + two(m) : m) + ":" + two(s % 60);
  };
  const dur = () => (Number.isFinite(v.duration) && v.duration > 0 ? v.duration : st.dur) || 0;
  const label = w => { w = String(w || "change").trim(); return w.charAt(0).toUpperCase() + w.slice(1); };
  const norm = ms => (Array.isArray(ms) ? ms : []).map(m => ({ from: +m.from, to: +m.to, what: String(m.what || "") }))
    .filter(m => Number.isFinite(m.from) && m.from >= 0).map(m => ({ ...m, to: Number.isFinite(m.to) && m.to > m.from ? m.to : m.from }));

  // ---------- idővonal: jelölők + lejátszófej ----------
  function drawMarks() {
    const d = dur();
    marksEl.replaceChildren();
    if (d) st.markers.forEach((m, i) => {
      const a = Math.min(d, m.from), b = Math.min(d, Math.max(a, m.to)), left = a / d * 100;
      const when = b - a >= 1 ? `${fmt(a)}–${fmt(b)}` : fmt(a), what = label(m.what);
      const btn = document.createElement("button"), tip = document.createElement("span");
      btn.type = "button";
      btn.className = "mk";
      btn.style.left = left + "%";
      btn.style.width = (b - a) / d * 100 + "%";
      btn.dataset.i = i;
      btn.setAttribute("aria-label", `${what}, ${when} — play from here`);
      tip.className = "tip"; tip.setAttribute("aria-hidden", "true"); tip.textContent = `${what} · ${when}`;
      btn.append(tip); marksEl.append(btn);
    });
    const n = st.markers.length;
    noteEl.textContent = n ? `${n} change${n > 1 ? "s" : ""} marked — click one to jump there` : "";
    drawHead();
  }
  function drawHead(t = v.currentTime) {
    const d = dur();
    timeEl.textContent = `${fmt(t)} / ${d ? fmt(d) : "–:––"}`;
    if (!d) return;
    head.hidden = false;
    head.style.left = Math.min(100, Math.max(0, t / d * 100)) + "%";
    for (const b of marksEl.children) {
      const m = st.markers[+b.dataset.i];
      b.classList.toggle("on", t >= m.from && t < Math.max(m.to, m.from + 1));
    }
  }
  function placeTip(btn) {      // a címke a sávon belül marad (telefonon a szélső jelölőnél se lógjon ki)
    const tip = btn.querySelector(".tip"); if (!tip) return;
    const W = bar.clientWidth, bl = btn.offsetLeft, tw = tip.offsetWidth;
    tip.style.transform = "none";
    tip.style.left = Math.max(-bl, Math.min(W - bl - tw, (btn.offsetWidth - tw) / 2)) + "px";
  }
  for (const ev of ["pointerover", "focusin"]) marksEl.addEventListener(ev, e => { const b = e.target.closest(".mk"); if (b) placeTip(b); });
  function jump(t, play) {
    if (v.readyState >= 1) v.currentTime = t; else st.pending = t;
    drawHead(t);
    if (play) v.play().catch(() => {});
  }
  marksEl.addEventListener("click", e => {
    const b = e.target.closest(".mk"); if (!b) return;
    e.stopPropagation();
    const m = st.markers[+b.dataset.i]; if (m) jump(m.from, true);
  });
  bar.addEventListener("click", e => {          // a sáv máshol: oda teker (a jelölő a saját kattintását kapja)
    const d = dur(), r = bar.getBoundingClientRect(); if (!d || !r.width) return;
    jump(Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * d, false);
  });
  for (const ev of ["timeupdate", "seeking", "seeked"]) v.addEventListener(ev, () => drawHead());
  v.addEventListener("durationchange", drawMarks);
  v.addEventListener("loadedmetadata", () => {
    if (st.pending != null) { v.currentTime = st.pending; st.pending = null; }
    drawMarks();
  });
  v.addEventListener("playing", () => { st.tries = 0; msg(""); });

  // ---------- források: előnézet (HLS) ↔ végleges MP4 ----------
  const isApple = /Apple/.test(navigator.vendor || "");
  const canNative = () => !!v.canPlayType("application/vnd.apple.mpegurl");
  let hlsLoad = null;
  const loadHls = () => hlsLoad || (hlsLoad = new Promise((ok, no) => {
    if (window.Hls) return ok(window.Hls);
    const s = document.createElement("script");
    s.src = pl.dataset.hlsJs; s.integrity = pl.dataset.hlsSri; s.crossOrigin = "anonymous";
    s.onload = () => (window.Hls ? ok(window.Hls) : no(new Error("hls.js")));
    s.onerror = () => { hlsLoad = null; no(new Error("hls.js")); };
    document.head.append(s);
  }));
  function stopHls() { if (st.hls) { st.hls.destroy(); st.hls = null; } }
  function nativePreview(at, play) {
    v.src = local(cfg.playlist); st.pending = at > 0 ? at : null;
    if (play) v.play().catch(() => {});
  }
  function playPreview(at = 0, play = false) {
    stopHls(); st.src = "preview"; drawFinal(); drawSeg();
    // Safari / iOS: a saját HLS-lejátszója (MSE nélkül is); máshol hls.js — ha az nem jön le, a natív, ha van
    if (canNative() && (isApple || !(window.MediaSource || window.ManagedMediaSource))) return nativePreview(at, play);
    loadHls().then(Hls => {
      if (st.src !== "preview") return;
      if (!Hls.isSupported()) return canNative() ? nativePreview(at, play) : cannot("This browser can't play the preview.");
      // a szakasz-végpont legfeljebb ~20 s-ig vár egy még készülő szakaszra, utána 503 + Retry-After: 2 → ehhez a türelem.
      // NINCS startPosition: a külön hangsáv (cc 0) a kép 0. szakaszának időbélyegére vár — később induló képpel
      // (startPosition > 0) a hls.js 1.5.13 sosem hozza létre a puffereket (Chrome-ban mérve, 10-25). → 0-ról indul,
      // a kért helyre a metaadatok után teker (st.pending).
      st.pending = at > 0 ? at : null;
      // a lista (master / kép / hang) a verzió tervére vár: hideg indításkor egy hosszú videón ez ~10 s is lehet
      const list = { default: { maxTimeToFirstByteMs: 30000, maxLoadTimeMs: 40000,
        timeoutRetry: { maxNumRetry: 2, retryDelayMs: 0, maxRetryDelayMs: 0 },
        errorRetry: { maxNumRetry: 4, retryDelayMs: 1000, maxRetryDelayMs: 4000 } } };
      const h = st.hls = new Hls({
        manifestLoadPolicy: list, playlistLoadPolicy: list,
        fragLoadPolicy: { default: { maxTimeToFirstByteMs: 30000, maxLoadTimeMs: 60000,
          timeoutRetry: { maxNumRetry: 2, retryDelayMs: 0, maxRetryDelayMs: 0 },
          errorRetry: { maxNumRetry: 10, retryDelayMs: 2000, maxRetryDelayMs: 4000 } } },
      });
      h.on(Hls.Events.ERROR, (_, d) => hlsError(h, d));
      h.on(Hls.Events.FRAG_LOADED, () => { st.tries = 0; st.media = 0; });
      h.loadSource(local(cfg.playlist));
      h.attachMedia(v);
      if (play) v.play().catch(() => {});
    }).catch(() => (canNative() ? nativePreview(at, play) : cannot("The player could not load — check the connection.")));
  }
  function hlsError(h, d) {
    if (!d.fatal || h !== st.hls) return;
    if (d.type === window.Hls.ErrorTypes.MEDIA_ERROR && st.media++ < 3) { h.recoverMediaError(); return; }
    if (++st.tries > 6) return cannot("The preview can't play right now.");
    const at = v.currentTime || 0, play = !v.paused;
    msg("Waiting for the preview…");
    setTimeout(() => { if (st.hls === h) playPreview(at, play); }, 2000 * st.tries);
  }
  function playFinal() {
    const url = st.final && st.final.video_url; if (!url) return;
    const at = v.currentTime || 0, play = !v.paused;
    stopHls(); st.src = "final"; msg(""); drawFinal(); drawSeg();
    v.src = local(url); st.pending = at > 0 ? at : null;
    if (play) v.play().catch(() => {});
  }
  v.addEventListener("error", () => {
    if (st.hls) return;                          // hls.js a saját hibáját jelzi (fent)
    const at = v.currentTime || 0;
    if (st.src === "final") {                    // a végleges fájl nem jön: vissza az előnézetre
      playPreview(at, false); msg("The final video didn't load — this is the preview.");
      return;
    }
    if (st.src !== "preview") return;
    if (++st.tries > 6) return cannot("The preview can't play right now.");
    msg("Waiting for the preview…");
    setTimeout(() => { if (st.src === "preview" && !st.hls) nativePreview(at, false); }, 2000 * st.tries);
  });
  which.addEventListener("click", e => {
    const b = e.target.closest("button[data-src]");
    if (!b || b.getAttribute("aria-pressed") === "true") return;
    if (b.dataset.src === "final") playFinal();
    else { const at = v.currentTime || 0, play = !v.paused; msg(""); playPreview(at, play); }
  });

  // ---------- állapot-sorok ----------
  function msg(text, action, fn) {
    msgEl.replaceChildren();
    msgEl.hidden = !text;
    if (!text) return;
    msgEl.append(text);
    if (action) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "act"; b.textContent = action;
      b.addEventListener("click", fn);
      msgEl.append(" ", b);
    }
  }
  function cannot(text) {
    msg(text, "Try again", () => { st.tries = 0; st.media = 0; msg(""); playPreview(v.currentTime || 0, true); });
  }
  function drawSeg() {
    const on = st.src === "preview" && st.total > 0 && st.ready < st.total;
    segEl.hidden = !on;
    if (on) segEl.textContent = `Preview: ${st.ready} of ${st.total} segments ready`;
  }
  const about = s => s < 90 ? `about ${Math.max(5, Math.round(s / 5) * 5)} s`
    : s < 5400 ? `about ${Math.round(s / 60)} min` : `about ${(s / 3600).toFixed(1)} h`;
  function eta(f) {     // „about 25 min (ready around 21:40)” — a szerver szövege, a kész-idő a néző órája szerint
    let secs = f.eta_s != null && Number.isFinite(+f.eta_s) ? +f.eta_s : null, at = f.ready_at ? new Date(f.ready_at) : null, txt = "";
    if (typeof f.eta === "string" && f.eta.trim()) {
      if (/^\d{4}-\d\d-\d\dT/.test(f.eta)) at = at || new Date(f.eta);
      else {
        txt = f.eta.trim();
        const m = txt.match(/([\d.]+)\s*(s|min|h)\b/);
        if (secs == null && m) secs = +m[1] * { s: 1, min: 60, h: 3600 }[m[2]];
      }
    }
    if (!txt && secs != null) txt = about(secs);
    if (!at && secs != null) at = new Date(Date.now() + secs * 1000);
    const hm = at && !isNaN(at) ? `(ready around ${two(at.getHours())}:${two(at.getMinutes())})` : "";
    return [txt, hm].filter(Boolean).join(" ");
  }
  function drawFinal() {
    const f = st.final || {}, done = f.status === "done" && !!f.video_url;
    which.hidden = !done;
    for (const b of which.querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.src === (st.src || "preview")));
    const e = eta(f);
    const text = {
      queued: `Final video: waiting to render${e ? ", " + e : ""}`,
      running: `Final video: rendering${e ? ", " + e : ""}`,
      done: done ? (st.src === "final" ? "Playing the final video — full quality." : "Final video: ready — full quality.") : "",
      failed: "Final video: the render failed. The preview still plays.",
      none: "Final video: not rendered yet.",
    }[f.status] || "";
    if (finEl.textContent !== text) finEl.textContent = text;
    finEl.hidden = !text;
  }

  // ---------- preview.json: amíg a szakaszok készülnek ~2 s-onként, amíg a végleges renderel ~10 s-onként ----------
  function apply(j) {
    if (Array.isArray(j.markers)) {
      const m = norm(j.markers);
      if (JSON.stringify(m) !== JSON.stringify(st.markers)) { st.markers = m; drawMarks(); }
    }
    if (+j.duration > 0 && +j.duration !== st.dur) { st.dur = +j.duration; drawMarks(); }
    if (j.segments_total != null && Number.isFinite(+j.segments_total)) {
      st.total = +j.segments_total; st.ready = Math.min(st.total, +j.segments_ready || 0);
    }
    if (j.final && typeof j.final === "object") st.final = j.final;
    drawSeg(); drawFinal();
  }
  async function poll() {
    if (document.hidden) { document.addEventListener("visibilitychange", poll, { once: true }); return; }
    try {
      const r = await fetch(local(cfg.json), { cache: "no-store", credentials: "same-origin" });
      if (!r.ok) throw new Error(String(r.status));
      apply(await r.json());
      st.fails = 0;
    } catch { st.fails++; }
    if (st.fails > 8) return;
    const filling = st.total != null && st.ready < st.total, rendering = ["queued", "running"].includes((st.final || {}).status);
    if (!st.fails && !filling && !rendering) return;              // minden kész: nincs mit figyelni
    setTimeout(poll, st.fails ? Math.min(30000, 2000 * 2 ** st.fails) : filling ? 2000 : 10000);
  }

  st.markers = norm(cfg.markers);
  drawMarks();
  // már kész a végleges: az szól (a jelölők ugyanott vannak), az előnézet egy kattintás; különben az előnézet
  if (st.final && st.final.status === "done" && st.final.video_url) playFinal(); else playPreview(0, false);
  if (cfg.json) poll();
})();
