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
    if (res.data) out.append(el("pre", {}, el("code", { text: JSON.stringify(res.data, null, 2) })));
    const first = files[0];
    if (first && first.file_id) out.append(el("a", { href: "/app/file/" + first.file_id + "/edit", class: "small", text: tr("open_editor", "Open in the editor") }));
  }

  async function upload(file) {
    const fd = new FormData();
    fd.append("file", file);
    return api("/api/upload", { method: "POST", body: fd });
  }

  function initWidget(box) {
    const form = $(".cv-form", box), out = $(".cv-out", box), input = $("input[type=file]", box), drop = $(".drop", box);
    const sel = $("select[name=target]", box), tool = box.dataset.tool;
    const scope = tool || (sel && sel.value) || "";
    ["dragenter", "dragover"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((e) => drop.addEventListener(e, () => drop.classList.remove("over")));
    drop.addEventListener("drop", (ev) => { ev.preventDefault(); input.files = ev.dataTransfer.files; input.dispatchEvent(new Event("change")); });
    input.addEventListener("change", async () => {
      $(".drop span", box).textContent = Array.from(input.files).map((f) => f.name).join(", ") || tr("or_drop", "or drop files here");
      if (!sel || box.dataset.target || !input.files[0]) return;
      const ext = extOf(input.files[0].name);
      try {
        const r = await api("/api/convert/targets?all=1&ext=" + encodeURIComponent(ext));
        sel.innerHTML = "";
        r.targets.forEach((x) => sel.append(el("option", { value: x.fmt, text: x.label })));
      } catch (e) { /* marad a régi lista */ }
    });
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
      const files = Array.from(input.files);
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
          for (const f of files) ids.push((await upload(f)).file_id);
          const job = await api("/api/tools/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tool, file_ids: ids, options: opts }) });
          resultRow(li, await poll(job, $(".bar-p i", li)));
        } catch (e) { li.append(el("span", { class: "err", text: e.message })); }
        return;
      }
      for (const [i, f] of files.entries()) {
        const li = el("li", {}, el("span", { text: f.name }), el("span", { class: "bar-p" }, el("i")));
        out.prepend(li);
        try {
          const up = await upload(f);
          const body = tool ? { tool, file_ids: [up.file_id], options: applyAll || i === 0 ? opts : {} }
                            : { file_id: up.file_id, target, options: applyAll || i === 0 ? opts : {} };
          const job = await api(tool ? "/api/tools/run" : "/api/convert/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
          resultRow(li, await poll(job, $(".bar-p i", li)));
        } catch (e) { li.append(el("span", { class: "err", text: e.message })); }
      }
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

  $$(".cv").forEach(initWidget);
  $$(".uc").forEach(initUnits);
  $$(".tz").forEach(initTz);
  $$(".jb").forEach(initJobBuilder);
})();
