/* Közös híd az MCP Apps (postMessage JSON-RPC, ui/*) és a ChatGPT Apps SDK (window.openai) között. */
const bridge = (() => {
  let seq = 0;
  const pending = {}, listeners = [];
  const post = (m) => window.parent && window.parent.postMessage(m, "*");
  window.addEventListener("message", (ev) => {
    const m = ev.data;
    if (!m || m.jsonrpc !== "2.0") return;
    if (m.id != null && pending[m.id] && !m.method) {
      const p = pending[m.id]; delete pending[m.id];
      m.error ? p.rej(new Error(m.error.message || "error")) : p.res(m.result);
      return;
    }
    if (m.method === "ui/notifications/tool-result") listeners.forEach((f) => f(m.params || {}));
  });
  const req = (method, params) => new Promise((res, rej) => {
    const id = ++seq; pending[id] = { res, rej };
    post({ jsonrpc: "2.0", id, method, params });
    setTimeout(() => { if (pending[id]) { delete pending[id]; rej(new Error("no answer from the host")); } }, 120000);
  });
  const sc = (r) => (r && (r.structuredContent || (r.content && r.content[0] && r.content[0].text && JSON.parse(r.content[0].text)))) || r;
  return {
    async init() {
      if (window.openai) return;
      try {
        await req("ui/initialize", { appInfo: { name: "forms", version: "1.0" }, appCapabilities: {}, protocolVersion: "2025-06-18" });
        post({ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} });
      } catch (e) { /* önálló megnyitás: nincs gazda */ }
    },
    async callTool(name, args) {
      const r = window.openai ? await window.openai.callTool(name, args) : await req("tools/call", { name, arguments: args });
      return sc(r);
    },
    onResult(f) {
      listeners.push((p) => f(sc(p)));
      if (window.openai) {
        const go = () => window.openai.toolOutput && f(window.openai.toolOutput);
        go(); window.addEventListener("openai:set_globals", go);
      }
    },
    say(text) {
      if (window.openai && window.openai.sendFollowUpMessage) return window.openai.sendFollowUpMessage({ prompt: text });
      return req("ui/message", { role: "user", content: [{ type: "text", text }] }).catch(() => {});
    },
  };
})();

/* Aláírásrajzoló vászon: egér, toll, érintés; átlátszó PNG-t ad. */
function signaturePad(canvas) {
  const ctx = canvas.getContext("2d");
  const ratio = Math.max(1, window.devicePixelRatio || 1);
  const fit = () => { const r = canvas.getBoundingClientRect(); canvas.width = r.width * ratio; canvas.height = r.height * ratio; ctx.scale(ratio, ratio); ctx.lineCap = ctx.lineJoin = "round"; ctx.strokeStyle = "#141a4a"; };
  fit();
  let drawing = false, last = null, empty = true;
  const pt = (e) => { const r = canvas.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top, p: e.pressure || 0.5 }; };
  canvas.addEventListener("pointerdown", (e) => { drawing = true; last = pt(e); canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener("pointermove", (e) => {
    if (!drawing) return;
    const p = pt(e);
    ctx.lineWidth = 1.6 + 2.4 * p.p;
    ctx.beginPath(); ctx.moveTo(last.x, last.y); ctx.lineTo(p.x, p.y); ctx.stroke();
    last = p; empty = false;
  });
  ["pointerup", "pointercancel", "pointerleave"].forEach((t) => canvas.addEventListener(t, () => { drawing = false; }));
  return {
    clear() { ctx.clearRect(0, 0, canvas.width, canvas.height); empty = true; },
    isEmpty: () => empty,
    png: () => canvas.toDataURL("image/png"),
  };
}
