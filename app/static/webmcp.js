/* WebMCP — a honlap eszközei a böngészőben futó AI-ügynököknek (W3C Web Machine Learning CG: document.modelContext).
   Ugyanazokat a végpontokat hívják, mint a felület, a látogató saját munkamenetével: amit ő megtehet a gombokkal,
   azt az ügynöke is — se több, se kevesebb. A régebbi Chrome-előzetes navigator.modelContext-jét is kezeli. */
(() => {
  "use strict";
  const mc = document.modelContext || navigator.modelContext;
  if (!mc || !window.isSecureContext || window.__aiceWebMCP) return;
  window.__aiceWebMCP = true;
  const modern = !!document.modelContext;          // az új szabvány a visszaadott objektumot maga szerializálja
  const wrap = (data) => (modern ? data : { content: [{ type: "text", text: JSON.stringify(data) }] });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const abs = (u) => (u ? new URL(u, location.origin).href : u);

  async function api(path, init = {}) {
    const r = await fetch(path, { credentials: "same-origin", ...init,
      headers: { Accept: "application/json", ...(init.body ? { "Content-Type": "application/json" } : {}), ...(init.headers || {}) } });
    const text = await r.text();
    let data;
    try { data = JSON.parse(text); } catch { data = { detail: text.slice(0, 300) }; }
    if (!r.ok) {
      const msg = (data && (data.detail || data.error)) || `HTTP ${r.status}`;
      throw new Error(r.status === 401 ? `${msg} (sign in at ${location.origin}/app first)` : String(msg));
    }
    return data;
  }

  async function waitJob(job, signal) {
    const id = job && (job.id || job.job_id);
    if (!id) return job;
    const t0 = Date.now();
    while (Date.now() - t0 < 20 * 60 * 1000) {
      if (signal && signal.aborted) throw new Error("Cancelled.");
      const j = await api(`/api/jobs/${encodeURIComponent(id)}`);
      if (j.status === "done") return j.result || {};
      if (j.status === "failed" || j.status === "cancelled") throw new Error(j.error || j.status);
      await sleep(1200);
    }
    throw new Error("Timed out — the job is still running; check list_my_files later.");
  }

  const fileOut = (f) => ({ file_id: f.file_id, name: f.label || f.name || f.original_name, download_url: abs(f.url),
                            size_kb: f.size_kb });

  async function fileFrom(input) {
    if (input.file_id) return input.file_id;
    if (input.url) return (await api("/api/import", { method: "POST", body: JSON.stringify({ provider: "url", url: input.url }) })).file_id;
    throw new Error("Give file_id (see list_my_files) or url (a public link to the file).");
  }

  let catalog;
  const getCatalog = async () => catalog || (catalog = await api("/api/catalog"));

  const tools = [
    {
      name: "list_formats", title: "List file formats",
      description: "Which formats this converter handles. With `from` (a file extension such as docx, heic, mkv) " +
        "it lists every format that file can be converted to; without it, how many formats there are per category.",
      inputSchema: { type: "object", properties: { from: { type: "string", description: "source file extension, e.g. pptx" } } },
      annotations: { readOnlyHint: true },
      async execute(input = {}) {
        if (input.from) {
          const r = await api(`/api/convert/targets?all=1&ext=${encodeURIComponent(String(input.from).replace(/^\./, ""))}`);
          return wrap({ from: input.from, targets: (r.targets || []).map((t) => ({ format: t.fmt, label: t.label, category: t.category })) });
        }
        const c = await getCatalog();
        return wrap({ formats: c.formats, categories: c.categories });
      },
    },
    {
      name: "list_tools", title: "List tools",
      description: "The file tools (compress, merge, split, OCR, watermark, PDF security, image/video/audio editing, " +
        "form recognition and filling, signatures…) with what each accepts and its options. `query` filters by words.",
      inputSchema: { type: "object", properties: { query: { type: "string", description: "e.g. pdf, compress, form" } } },
      annotations: { readOnlyHint: true },
      async execute(input = {}) {
        const c = await getCatalog();
        const q = String(input.query || "").toLowerCase().split(/\s+/).filter(Boolean);
        const hit = (t) => q.every((w) => `${t.key} ${t.name} ${t.description} ${t.group} ${t.accepts.join(" ")}`.toLowerCase().includes(w));
        return wrap({ tools: c.tools.filter(hit).map((t) => ({ key: t.key, name: t.name, description: t.description,
          accepts: t.accepts, files: t.max_files > 1 ? `${t.min_files}–${t.max_files}` : "1", output: t.output,
          needs_account: t.ai, options: t.options })) });
      },
    },
    {
      name: "list_my_files", title: "List my files",
      description: "The files in the visitor's workspace (uploads and results), newest first, with file_id and download URL.",
      inputSchema: { type: "object", properties: { limit: { type: "integer", minimum: 1, maximum: 200, default: 30 } } },
      annotations: { readOnlyHint: true },
      async execute(input = {}) {
        const r = await api("/api/files");
        const all = [...(r.outputs || []), ...(r.inputs || [])].sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
        return wrap({ files: all.slice(0, input.limit || 30).map(fileOut) });
      },
    },
    {
      name: "import_file", title: "Import a file from a link",
      description: "Bring a public file (http/https link, also Dropbox/OneDrive direct links) into the workspace → file_id.",
      inputSchema: { type: "object", properties: { url: { type: "string", format: "uri" }, name: { type: "string" } }, required: ["url"] },
      annotations: { untrustedContentHint: true },
      async execute(input) {
        const r = await api("/api/import", { method: "POST", body: JSON.stringify({ provider: "url", url: input.url, name: input.name }) });
        return wrap(fileOut(r));
      },
    },
    {
      name: "convert_file", title: "Convert a file",
      description: "Convert a file (file_id from list_my_files, or a public url) to another format — documents, " +
        "spreadsheets, slides, e-books, images, vector, CAD, video, audio, archives, fonts (264 formats). " +
        "options: e.g. {\"quality\": 80, \"width\": 1280, \"page_range\": \"1-3\", \"video_codec\": \"h265\"} " +
        "(list_formats shows targets). Returns the result's download_url.",
      inputSchema: { type: "object", properties: {
        file_id: { type: "string" }, url: { type: "string", format: "uri" },
        to: { type: "string", description: "target extension, e.g. pdf, docx, webp, mp4, epub" },
        options: { type: "object", additionalProperties: true } }, required: ["to"] },
      async execute(input, opts = {}) {
        const fid = await fileFrom(input);
        const job = await api("/api/convert/run", { method: "POST",
          body: JSON.stringify({ file_id: fid, target: String(input.to).replace(/^\./, ""), options: input.options || {} }) });
        const r = await waitJob(job, opts.signal);
        return wrap({ result: fileOut(r), files: (r.files || []).map(fileOut), engines: r.engines });
      },
    },
    {
      name: "run_tool", title: "Run a file tool",
      description: "Run one tool from list_tools (e.g. compress-pdf, merge-pdf, ocr-pdf, watermark, protect-pdf, " +
        "fill-form, form-recognize, video-trim) on files given by file_ids and/or urls, with its options.",
      inputSchema: { type: "object", properties: {
        tool: { type: "string", description: "a key from list_tools" },
        file_ids: { type: "array", items: { type: "string" } }, urls: { type: "array", items: { type: "string", format: "uri" } },
        options: { type: "object", additionalProperties: true } }, required: ["tool"] },
      async execute(input, opts = {}) {
        const ids = [...(input.file_ids || [])];
        for (const u of input.urls || []) ids.push(await fileFrom({ url: u }));
        const job = await api("/api/tools/run", { method: "POST", body: JSON.stringify({ tool: input.tool, file_ids: ids, options: input.options || {} }) });
        const r = await waitJob(job, opts.signal);
        return wrap({ files: (r.files || []).map(fileOut), data: r.data });
      },
    },
    {
      name: "open_page", title: "Open a page",
      description: "Show the visitor a page of this site: the converter for a pair (from + to), a tool (tool key), " +
        "or the app (where documents, decks, videos and designs are made).",
      inputSchema: { type: "object", properties: { from: { type: "string" }, to: { type: "string" }, tool: { type: "string" },
        app: { type: "boolean" } } },
      async execute(input = {}) {
        let href = "/app";
        if (input.from && input.to) href = `/convert/${encodeURIComponent(input.from)}-to-${encodeURIComponent(input.to)}`;
        else if (input.tool) href = `/tools/${encodeURIComponent(input.tool)}`;
        setTimeout(() => location.assign(href), 50);
        return wrap({ opened: abs(href) });
      },
    },
  ];

  const ctrl = new AbortController();
  for (const t of tools) {
    try {
      const r = mc.registerTool(t, { signal: ctrl.signal });
      if (r && typeof r.catch === "function") r.catch(() => {});
    } catch (e) {
      if (typeof mc.provideContext === "function") { try { mc.provideContext({ tools }); } catch (_) {} break; }
    }
  }
  window.addEventListener("pagehide", () => ctrl.abort(), { once: true });
})();
