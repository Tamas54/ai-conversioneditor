/* Nyilvános oldalak: konverter-widget, eszközök, presetek, átváltók, Job Builder. Függőség nélkül. */
(() => {
  "use strict";
  const T = JSON.parse((document.getElementById("t-js") || {}).textContent || "{}");
  const tr = (k, d) => T[k] || d || k;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const el = (tag, attrs = {}, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) k === "text" ? (e.textContent = v) : e.setAttribute(k, v);
    kids.forEach((c) => e.append(c));
    return e;
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function api(url, opts = {}) {
    const r = await fetch(url, { credentials: "same-origin", ...opts });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || j.error || r.statusText);
    return j;
  }
  const extOf = (n) => { const m = n.toLowerCase().match(/\.(tar\.(gz|bz2|xz|zst|7z|lzo|bz|z))$|\.([a-z0-9]+)$/); return m ? (m[1] || m[3]) : ""; };

  // ---------- opciók a űrlapból ----------
  function readOptions(form) {
    const o = {};
    $$(".opts [name]", form).forEach((i) => {
      if (i.name.startsWith("_")) return;
      if (i.type === "checkbox") { if (i.checked !== i.defaultChecked) o[i.name] = i.checked; return; }
      const v = i.value.trim();
      if (!v) return;
      if (i.dataset.list) o[i.name] = v.split(",").map((x) => x.trim()).filter(Boolean);
      else if (i.type === "number") o[i.name] = Number(v);
      else o[i.name] = v;
    });
    return o;
  }
  function writeOptions(form, o) {
    $$(".opts [name]", form).forEach((i) => {
      if (!(i.name in o)) return;
      if (i.type === "checkbox") i.checked = !!o[i.name];
      else i.value = Array.isArray(o[i.name]) ? o[i.name].join(", ") : o[i.name];
    });
  }

  async function poll(job, bar) {
    for (let i = 0; i < 3600; i++) {
      if (job.status === "done") return job.result;
      if (job.status === "failed" || job.status === "cancelled") throw new Error(job.error || tr("failed", "Failed"));
      if (bar && job.total) bar.style.width = Math.round((100 * job.done) / job.total) + "%";
      await sleep(i < 10 ? 600 : 1500);
      job = await api("/api/jobs/" + job.id);
    }
    throw new Error(tr("timeout", "Timed out"));
  }

  function resultRow(out, res) {
    out.innerHTML = "";
    const files = res.files && res.files.length ? res.files : [res];
    files.forEach((f) => out.append(el("a", { href: f.url, download: f.label || "", class: "btn primary small", text: tr("download", "Download") + " " + (f.label || "") })));
    if (res.data && res.data.values && res.data.provenance) {      // ügynöki kitöltés: mező · érték · forrás
      const tb = el("tbody");
      Object.entries(res.data.values).forEach(([k, v]) => tb.append(el("tr", {}, el("th", { scope: "row", text: k }),
        el("td", { text: String(v) }), el("td", { class: "small muted", text: res.data.provenance[k] || "" }))));
      out.append(el("table", { class: "facts review" }, tb));
    }
    if (res.data) out.append(el("details", { class: "data" }, el("summary", { text: tr("details", "Details") }),
      el("pre", {}, el("code", { text: JSON.stringify(res.data, null, 2) }))));
    const first = files[0];
    if (first && first.file_id) out.append(el("a", { href: "/app/file/" + first.file_id + "/edit", class: "small", text: tr("open_editor", "Open in the editor") }));
    saveButtons(out, files.filter((f) => f.file_id));
  }

  // ---------- mentés a felhőbe: Dropbox Saver, OneDrive, Google Drive (a böngésző tokenjével) ----------
  function saveButtons(out, files) {
    const c = importCfg;
    if (!c || !files.length) return;
    const add = (label, fn) => { const b = el("button", { type: "button", class: "btn ghost small", text: tr("save_to", "Save to") + " " + label }); b.addEventListener("click", () => fn(b)); out.append(b); };
    if (c.dropbox) add("Dropbox", async () => {
      await loadScript("https://www.dropbox.com/static/api/2/dropins.js", { id: "dropboxjs", "data-app-key": c.dropbox.app_key });
      window.Dropbox.save({ files: files.map((f) => ({ url: f.url, filename: f.label })) });
    });
    if (c.onedrive) add("OneDrive", async () => {
      await loadScript("https://js.live.net/v7.2/OneDrive.js");
      files.forEach((f) => window.OneDrive.save({ clientId: c.onedrive.client_id, action: "save", sourceUri: f.url, fileName: f.label,
        advanced: { redirectUri: location.origin + location.pathname } }));
    });
    if (c.google) add("Google Drive", async (b) => {
      await loadScript("https://accounts.google.com/gsi/client");
      window.google.accounts.oauth2.initTokenClient({ client_id: c.google.client_id, scope: "https://www.googleapis.com/auth/drive.file",
        callback: async (tok) => {
          if (!tok.access_token) return;
          try {
            await api("/api/export", { method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ provider: "google-drive", access_token: tok.access_token, file_ids: files.map((f) => f.file_id) }) });
            b.textContent = "✓ Google Drive";
          } catch (e) { b.after(el("span", { class: "err", text: " " + e.message })); }
        } }).requestAccessToken();
    });
  }

  const CHUNKED_FROM = 20 * 1024 * 1024;   // ennél nagyobb fájl darabokban megy (proxy-korlátok, megszakadás)
  async function upload(file, bar) {
    if (file.remote) return file.remote;                  // URL-ből / felhőből már behozott fájl
    if (file.size <= CHUNKED_FROM) {
      const fd = new FormData();
      fd.append("file", file);
      return api("/api/upload", { method: "POST", body: fd });
    }
    const st = await api("/api/upload/chunks", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: file.name, size: file.size }) });
    for (let i = 0; i < st.chunks; i++) {
      const part = file.slice(i * st.chunk_size, (i + 1) * st.chunk_size);
      for (let k = 0; ; k++) {
        try { await api(`/api/upload/chunks/${st.upload_id}/${i}`, { method: "PUT", body: part }); break; }
        catch (e) { if (k >= 2) throw e; await sleep(1000 * (k + 1)); }
      }
      if (bar) bar.style.width = Math.round((100 * (i + 1)) / st.chunks) + "%";
    }
    return api(`/api/upload/chunks/${st.upload_id}/complete`, { method: "POST" });
  }

  // ---------- import: URL, Google Drive, Dropbox, OneDrive ----------
  let importCfg = null;
  const loadScript = (src, attrs = {}) => new Promise((ok, bad) => {
    if ($(`script[src="${src}"]`)) return ok();
    const s = el("script", { src, ...attrs });
    s.onload = ok; s.onerror = bad;
    document.head.append(s);
  });
  async function importRemote(box, body) {
    const chip = el("li", { class: "chip" }, el("span", { text: body.name || body.url || "…" }));
    $(".cv-remote", box).append(chip);
    try {
      const r = await api("/api/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      chip.firstChild.textContent = r.original_name;
      box._remote.push({ name: r.original_name, size: (r.size_kb || 0) * 1024, remote: r });
      box.dispatchEvent(new Event("cv:files"));
    } catch (e) { chip.append(el("span", { class: "err", text: " " + e.message })); }
  }
  async function initImport(box) {
    box._remote = [];
    const bar = $(".cv-import", box);
    if (!bar) return;
    $("[data-src=url]", bar).addEventListener("click", () => {
      const url = prompt(tr("paste_url", "Paste a link to the file (https://…)"));
      if (url) importRemote(box, { provider: "url", url: url.trim() });
    });
    try { importCfg = importCfg || await api("/api/import/config"); } catch (e) { return; }
    const show = (k) => { const b = $(`[data-src=${k}]`, bar); if (b) b.hidden = false; return b; };
    if (importCfg.dropbox) show("dropbox").addEventListener("click", async () => {
      await loadScript("https://www.dropbox.com/static/api/2/dropins.js", { id: "dropboxjs", "data-app-key": importCfg.dropbox.app_key });
      window.Dropbox.choose({ linkType: "direct", multiselect: true,
        success: (files) => files.forEach((f) => importRemote(box, { provider: "url", url: f.link, name: f.name })) });
    });
    if (importCfg.onedrive) show("onedrive").addEventListener("click", async () => {
      await loadScript("https://js.live.net/v7.2/OneDrive.js");
      window.OneDrive.open({ clientId: importCfg.onedrive.client_id, action: "download", multiSelect: true,
        advanced: { redirectUri: location.origin + location.pathname },
        success: (r) => r.value.forEach((f) => importRemote(box, { provider: "url", url: f["@microsoft.graph.downloadUrl"], name: f.name })) });
    });
    if (importCfg.google) show("google-drive").addEventListener("click", async () => {
      const g = importCfg.google;
      await loadScript("https://accounts.google.com/gsi/client");
      await loadScript("https://apis.google.com/js/api.js");
      await new Promise((ok) => window.gapi.load("picker", ok));
      const tc = window.google.accounts.oauth2.initTokenClient({ client_id: g.client_id,
        scope: "https://www.googleapis.com/auth/drive.readonly",
        callback: (tok) => {
          if (!tok.access_token) return;
          const P = window.google.picker;
          new P.PickerBuilder().addView(new P.DocsView().setIncludeFolders(false)).enableFeature(P.Feature.MULTISELECT_ENABLED)
            .setOAuthToken(tok.access_token).setDeveloperKey(g.api_key).setAppId(g.app_id)
            .setCallback((d) => {
              if (d.action !== P.Action.PICKED) return;
              d.docs.forEach((doc) => importRemote(box, { provider: "google-drive", file_id: doc.id, name: doc.name, access_token: tok.access_token }));
            }).build().setVisible(true);
        } });
      tc.requestAccessToken();
    });
  }

  function initWidget(box) {
    const form = $(".cv-form", box), out = $(".cv-out", box), input = $("input[type=file]", box), drop = $(".drop", box);
    const sel = $("select[name=target]", box), tool = box.dataset.tool;
    const scope = tool || (sel && sel.value) || "";
    ["dragenter", "dragover"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((e) => drop.addEventListener(e, () => drop.classList.remove("over")));
    drop.addEventListener("drop", (ev) => { ev.preventDefault(); input.files = ev.dataTransfer.files; input.dispatchEvent(new Event("change")); });
    initImport(box);
    const allFiles = () => Array.from(input.files).concat(box._remote || []);
    const onFiles = async () => {
      const all = allFiles();
      $(".drop span", box).textContent = Array.from(input.files).map((f) => f.name).join(", ") || tr("or_drop", "or drop files here");
      if (!sel || box.dataset.target || !all[0]) return;
      const ext = extOf(all[0].name);
      try {
        const r = await api("/api/convert/targets?all=1&ext=" + encodeURIComponent(ext));
        sel.innerHTML = "";
        r.targets.forEach((x) => sel.append(el("option", { value: x.fmt, text: x.label })));
      } catch (e) { /* marad a régi lista */ }
    };
    input.addEventListener("change", onFiles);
    box.addEventListener("cv:files", onFiles);
    loadPresets(box, scope);
    $("[data-act=save-preset]", box)?.addEventListener("click", async () => {
      const name = prompt(tr("preset_name", "Preset name"));
      if (!name) return;
      try {
        await api("/api/presets", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, scope: tool || (sel && sel.value) || "any", options: readOptions(form) }) });
        loadPresets(box, tool || (sel && sel.value));
      } catch (e) { alert(e.message); }
    });
    $("[data-act=load-preset]", box)?.addEventListener("change", (ev) => {
      const o = ev.target.selectedOptions[0];
      if (o && o.dataset.options) writeOptions(form, JSON.parse(o.dataset.options));
    });
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const files = allFiles();
      const min = Number(box.dataset.min || (tool ? 1 : 1));
      if (files.length < min && min > 0) { input.click(); return; }
      const opts = readOptions(form);
      const applyAll = !$("[name=_apply_all]", form) || $("[name=_apply_all]", form).checked;
      const target = sel ? sel.value : "";
      if (tool && (Number(box.dataset.max || 1) !== 1 || files.length === 0)) {
        const li = el("li", {}, el("span", { text: files.map((f) => f.name).join(", ") || tool }), el("span", { class: "bar-p" }, el("i")));
        out.prepend(li);
        try {
          const ids = [];
          for (const f of files) ids.push((await upload(f, $(".bar-p i", li))).file_id);
          const job = await api("/api/tools/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tool, file_ids: ids, options: opts }) });
          const res = await poll(job, $(".bar-p i", li));
          resultRow(li, res);
          afterResult(box, li, res, ids, opts);
        } catch (e) { li.append(el("span", { class: "err", text: e.message })); }
        return;
      }
      for (const [i, f] of files.entries()) {
        const li = el("li", {}, el("span", { text: f.name }), el("span", { class: "bar-p" }, el("i")));
        out.prepend(li);
        try {
          const up = await upload(f, $(".bar-p i", li));
          const body = tool ? { tool, file_ids: [up.file_id], options: applyAll || i === 0 ? opts : {} }
                            : { file_id: up.file_id, target, options: applyAll || i === 0 ? opts : {} };
          const job = await api(tool ? "/api/tools/run" : "/api/convert/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
          const res = await poll(job, $(".bar-p i", li));
          resultRow(li, res);
          if (tool) afterResult(box, li, res, [up.file_id], body.options);
        } catch (e) { li.append(el("span", { class: "err", text: e.message })); }
      }
      box._remote = [];
      $(".cv-remote", box).innerHTML = "";
    });
  }

  async function loadPresets(box, scope) {
    const s = $("[data-act=load-preset]", box);
    if (!s) return;
    try {
      const r = await api("/api/presets" + (scope ? "?scope=" + encodeURIComponent(scope) : ""));
      s.querySelectorAll("option[data-options]").forEach((o) => o.remove());
      r.presets.forEach((p) => { const o = el("option", { value: p.id, text: p.name }); o.dataset.options = JSON.stringify(p.options); s.append(o); });
    } catch (e) { /* vendég: nincs preset */ }
  }

  // ---------- mértékegység, időzóna ----------
  function initUnits(f) {
    const go = async () => {
      const v = $("[name=value]", f).value, a = $("[name=from]", f).value, b = $("[name=to]", f).value;
      if (v === "") return;
      try {
        const r = await api(`/api/units/convert?value=${encodeURIComponent(v)}&from=${a}&to=${b}`);
        $("output", f).textContent = new Intl.NumberFormat(document.documentElement.lang, { maximumSignificantDigits: 10 }).format(r.result);
      } catch (e) { $("output", f).textContent = e.message; }
    };
    f.addEventListener("input", go);
    go();
  }
  function initTz(f) {
    const go = async () => {
      const time = $("[name=time]", f).value, from = $("[name=from]", f).value;
      const to = $$("[name=to]:checked", f).map((x) => x.value).join(",");
      try {
        const r = await api(`/api/timezones/convert?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}${time ? "&time=" + encodeURIComponent(time) : ""}`);
        const ol = $(".tz-out", f);
        ol.innerHTML = "";
        r.to.forEach((z) => ol.append(el("li", { text: `${z.zone}: ${z.time.replace("T", " ").slice(0, 16)} (${z.abbr}, ${z.offset})` })));
      } catch (e) { $(".tz-out", f).textContent = e.message; }
    };
    f.addEventListener("input", go);
    go();
  }

  // ---------- Job Builder ----------
  function initJobBuilder(root) {
    const base = root.dataset.base, list = $(".jb-tasks", root);
    let n = 0;
    const tasks = [];
    const render = () => {
      const json = { tasks: {} };
      tasks.forEach((t) => { json.tasks[t.name] = t.data; });
      $(".jb-json", root).textContent = JSON.stringify(json, null, 2);
      $(".jb-curl", root).textContent = `curl -X POST ${base}/v1/jobs -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '${JSON.stringify(json)}'`;
      return json;
    };
    const add = (kind) => {
      n += 1;
      const prev = tasks.length ? tasks[tasks.length - 1].name : "";
      const t = { name: `${kind}-${n}`, data: {} };
      if (kind === "import") t.data = { operation: "import/url", url: "https://example.com/file.docx" };
      if (kind === "convert") t.data = { operation: "convert", input: prev, output_format: "pdf" };
      if (kind === "tool") t.data = { operation: "tool/compress-pdf", input: prev };
      if (kind === "export") t.data = { operation: "export/url", input: prev };
      tasks.push(t);
      const box = el("div", { class: "jb-task" });
      Object.entries(t.data).forEach(([k, v]) => {
        const inp = el("input", { value: v, "aria-label": k });
        inp.addEventListener("input", () => { t.data[k] = inp.value; render(); });
        box.append(el("label", { text: k + " " }, inp));
      });
      list.append(box);
      render();
    };
    $$("[data-jb]", root).forEach((b) => b.addEventListener("click", async () => {
      const a = b.dataset.jb;
      if (a.startsWith("add-")) return add(a.slice(4));
      const key = $(".jb-key", root).value.trim();
      const res = $(".jb-result", root);
      try {
        const r = await fetch(base + "/v1/jobs", { method: "POST", headers: { Authorization: "Bearer " + key, "Content-Type": "application/json" }, body: JSON.stringify(render()) });
        res.textContent = JSON.stringify(await r.json(), null, 2);
      } catch (e) { res.textContent = e.message; }
    }));
    add("import"); add("convert"); add("export");
  }


  // ---------- űrlapok: mező-ellenőrzés, kérdések; aláírásrajzoló ----------
  async function runTool(tool, file_ids, options) {
    const job = await api("/api/tools/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tool, file_ids, options }) });
    return poll(job);
  }
  function fieldTable(fields, values, editable) {
    const tb = el("tbody");
    fields.filter((f) => f.type !== "signature").forEach((f) => {
      const inp = el("input", { type: "text", "data-name": f.name, placeholder: f.type === "checkbox" ? tr("yes_no", "yes / no") : f.type === "date" ? "YYYY-MM-DD" : "" });
      const v = values && values[f.name];
      inp.value = v === true ? tr("yes", "yes") : v == null || v === false ? "" : v;
      const lab = editable ? el("input", { type: "text", value: f.label || f.name, "data-label": f.name, "aria-label": tr("label", "Label") }) : el("span", { text: f.label || f.name });
      tb.append(el("tr", {}, el("th", { scope: "row" }, lab), el("td", { class: "small muted", text: f.type }), el("td", {}, inp)));
    });
    return el("table", { class: "facts review" }, tb);
  }
  const collect = (root, sel, attr) => { const o = {}; $$(sel, root).forEach((i) => { if (i.value.trim()) o[i.getAttribute(attr)] = i.value.trim(); }); return o; };
  function afterResult(box, li, res, ids, opts) {
    const tool = box.dataset.tool, data = res && res.data;
    if (!data) return;
    if (tool === "form-recognize" && data.fields) {
      const panel = el("div", { class: "review-panel" }, el("p", { class: "small", text: tr("review_hint", "Check the labels, type the values, then fill. Wrong field? Clear its value.") }));
      panel.append(fieldTable(data.fields, null, true));
      const btn = el("button", { type: "button", class: "btn primary small", text: tr("fill_now", "Fill the form") });
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        const labels = collect(panel, "input[data-label]", "data-label");
        const schema = { ...data, fields: data.fields.map((f) => ({ ...f, label: labels[f.name] || f.label })) };
        try { const r = await runTool("fill-form", ids, { values: collect(panel, "input[data-name]", "data-name"), schema }); resultRow(panel, r); }
        catch (e) { panel.append(el("span", { class: "err", text: e.message })); btn.disabled = false; }
      });
      panel.append(btn);
      li.append(panel);
    }
    if (tool === "ai-form-filler" && data.questions && data.questions.length) {
      const panel = el("div", { class: "review-panel" }, el("p", { class: "small", text: tr("questions_hint", "A few details are missing:") }));
      const tb = el("tbody");
      data.questions.forEach((q) => tb.append(el("tr", {}, el("th", { scope: "row", text: q.label + (q.error ? " — " + q.error : "") }),
        el("td", {}, el("input", { type: "text", "data-name": q.name, value: q.given || "" })))));
      panel.append(el("table", { class: "facts review" }, tb));
      const btn = el("button", { type: "button", class: "btn primary small", text: tr("continue", "Continue") });
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        const answers = { ...((opts && opts.answers) || {}), ...collect(panel, "input[data-name]", "data-name") };
        const nopts = { ...(opts || {}), answers };
        try { const r = await runTool("ai-form-filler", ids, nopts); panel.innerHTML = ""; resultRow(panel, r); afterResult(box, panel, r, ids, nopts); }
        catch (e) { panel.append(el("span", { class: "err", text: e.message })); btn.disabled = false; }
      });
      panel.append(btn);
      li.append(panel);
    }
    if (data.signature_id) loadSignatures();
  }

  function signaturePad(canvas) {
    const ctx = canvas.getContext("2d");
    const ratio = Math.max(1, window.devicePixelRatio || 1);
    const r0 = canvas.getBoundingClientRect();
    canvas.width = r0.width * ratio; canvas.height = r0.height * ratio; ctx.scale(ratio, ratio);
    ctx.lineCap = ctx.lineJoin = "round"; ctx.strokeStyle = "#141a4a";
    let drawing = false, last = null, empty = true;
    const pt = (e) => { const r = canvas.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top, p: e.pressure || 0.5 }; };
    canvas.addEventListener("pointerdown", (e) => { drawing = true; last = pt(e); canvas.setPointerCapture(e.pointerId); });
    canvas.addEventListener("pointermove", (e) => {
      if (!drawing) return;
      const p = pt(e); ctx.lineWidth = 1.6 + 2.4 * p.p;
      ctx.beginPath(); ctx.moveTo(last.x, last.y); ctx.lineTo(p.x, p.y); ctx.stroke(); last = p; empty = false;
    });
    ["pointerup", "pointercancel"].forEach((t) => canvas.addEventListener(t, () => { drawing = false; }));
    return { clear() { ctx.clearRect(0, 0, canvas.width, canvas.height); empty = true; }, isEmpty: () => empty, png: () => canvas.toDataURL("image/png") };
  }
  async function loadSignatures() {
    const lists = $$(".sig-list"), dl = $("#sig-ids");
    if (!lists.length && !dl) return;
    let sigs = [];
    try { sigs = (await api("/api/signatures")).signatures || []; } catch (e) { return; }
    lists.forEach((ul) => {
      ul.innerHTML = "";
      sigs.forEach((s) => {
        const del = el("button", { type: "button", class: "btn ghost small", text: tr("delete", "Delete") });
        del.addEventListener("click", async () => { await api("/api/signatures/" + s.id, { method: "DELETE" }); loadSignatures(); });
        ul.append(el("li", {}, el("img", { src: "/api/signatures/" + s.id + "/image", alt: s.name, height: "48" }),
          el("span", { text: ` ${s.name} · ` }), el("code", { text: s.id }), del));
      });
    });
    if (dl) { dl.innerHTML = ""; sigs.forEach((s) => dl.append(el("option", { value: s.id, text: s.name }))); }
  }
  function initSigPad(root) {
    const pad = signaturePad($("canvas", root));
    $("[data-act=clear]", root).addEventListener("click", () => pad.clear());
    $("[data-act=save]", root).addEventListener("click", async (ev) => {
      if (pad.isEmpty()) return;
      ev.target.disabled = true;
      const out = $(".sig-out", root);
      try {
        const r = await runTool("create-signature", [], { image_base64: pad.png(), name: $("[name=sig_name]", root).value || "Signature",
          kind: $("[name=sig_kind]", root).value });
        out.innerHTML = ""; resultRow(out, r); pad.clear(); loadSignatures();
      } catch (e) { out.textContent = e.message; }
      ev.target.disabled = false;
    });
  }
  $$(".sigpad").forEach(initSigPad);
  $$("input[name=signature_id]").forEach((i) => { i.setAttribute("list", "sig-ids"); if (!$("#sig-ids")) document.body.append(el("datalist", { id: "sig-ids" })); });
  loadSignatures();

  // „← Back”: ha az oldalunkról jött, egy lépés vissza (mint a böngésző); különben a szülőoldal (a morzsamenüből)
  $$("[data-back]").forEach((a) => a.addEventListener("click", (e) => {
    try {
      if (document.referrer && new URL(document.referrer).origin === location.origin && history.length > 1) { e.preventDefault(); history.back(); }
    } catch (err) { /* marad a link */ }
  }));
  $$(".cv").forEach(initWidget);
  $$(".uc").forEach(initUnits);
  $$(".tz").forEach(initTz);
  $$(".jb").forEach(initJobBuilder);
})();
