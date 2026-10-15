// Node SDK for the /v1 REST API (jobs and tasks, uploads, webhooks). No dependencies (Node ≥ 18: fetch, FormData, Blob).
import { createHmac, timingSafeEqual } from "node:crypto";
import { readFile, stat, open, writeFile } from "node:fs/promises";
import { basename } from "node:path";

const CHUNK = 50 * 1024 * 1024;

export class ApiError extends Error {
  constructor(status, code, message) {
    super(`${status} ${code}: ${message}`);
    Object.assign(this, { status, code });
  }
}

export class Client {
  constructor(baseUrl, { apiKey = process.env.AICE_API_KEY, fetch: f = globalThis.fetch } = {}) {
    this.base = baseUrl.replace(/\/$/, "") + "/v1";
    this.apiKey = apiKey;
    this.fetch = f;
    this.jobs = {
      create: (tasks, { tag, webhookUrl } = {}) => this.req("POST", "/jobs", { tasks, ...(tag && { tag }), ...(webhookUrl && { webhook_url: webhookUrl }) }),
      get: (id, wait = 0) => this.req("GET", `/jobs/${id}${wait ? `?wait=${wait}` : ""}`),
      list: (q = {}) => this.req("GET", "/jobs?" + new URLSearchParams(q)).then((r) => r.data),
      cancel: (id) => this.req("DELETE", `/jobs/${id}`),
      wait: async (id, timeoutMs = 600000) => {
        const end = Date.now() + timeoutMs;
        for (;;) {
          const job = await this.jobs.get(id, Math.max(1, Math.min(60, Math.floor((end - Date.now()) / 1000))));
          if (["finished", "error", "cancelled"].includes(job.status)) {
            if (job.status !== "finished") {
              const bad = job.tasks.find((t) => t.status === "error") || {};
              throw new ApiError(422, bad.code || "JOB_FAILED", bad.message || "the job failed");
            }
            return job;
          }
          if (Date.now() > end) throw new Error(`job ${id} still ${job.status}`);
        }
      },
      files: (job, task) => job.tasks
        .filter((t) => (task ? t.name === task : t.operation.startsWith("export/")))
        .flatMap((t) => (t.result && t.result.files) || []),
    };
    this.webhooks = {
      list: () => this.req("GET", "/webhooks"),
      create: (url, events = ["job.finished", "job.failed"]) => this.req("POST", "/webhooks", { url, events }),
      delete: (id) => this.req("DELETE", `/webhooks/${id}`),
    };
  }

  url(p) { return p.startsWith("http") ? p : this.base + p; }

  async req(method, path, body) {
    const r = await this.fetch(this.url(path), {
      method,
      headers: { Authorization: `Bearer ${this.apiKey}`, ...(body && { "Content-Type": "application/json" }) },
      body: body ? JSON.stringify(body) : undefined,
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new ApiError(r.status, (j.error && j.error.code) || "ERROR", (j.error && j.error.message) || r.statusText);
    return j;
  }

  me() { return this.req("GET", "/me"); }
  targets(fmt) { return this.req("GET", `/catalog/conversions?from=${encodeURIComponent(fmt)}`).then((r) => r.targets); }

  async convert({ url, path, outputFormat, options = {}, wait = true }) {
    let job;
    if (url) job = await this.req("POST", "/convert", { url, output_format: outputFormat, options, wait: wait ? 60 : 0 });
    else if (path) {
      job = await this.jobs.create({
        up: { operation: "import/upload" },
        convert: { operation: "convert", input: "up", output_format: outputFormat, options },
        export: { operation: "export/url", input: "convert" },
      });
      await this.upload(job, "up", path);
    } else throw new Error("give url or path");
    return wait && !["finished", "error"].includes(job.status) ? this.jobs.wait(job.id) : job;
  }

  async upload(job, taskName, path) {
    let task = job.tasks.find((t) => t.name === taskName);
    for (let i = 0; !(task.result && task.result.form); i++) {
      if (i > 150) throw new Error("the upload task didn't become ready");
      await new Promise((r) => setTimeout(r, 200));
      task = await this.req("GET", `/tasks/${task.id}`);
    }
    const form = task.result.form;
    const { size } = await stat(path);
    if (size <= CHUNK) {
      const fd = new FormData();
      fd.append("file", new Blob([await readFile(path)]), basename(path));
      const r = await this.fetch(this.url(form.url), { method: "POST", body: fd });
      if (!r.ok) throw new ApiError(r.status, "UPLOAD_FAILED", await r.text());
      return r.json();
    }
    const fh = await open(path, "r");
    let n = 0;
    try {
      for (let off = 0; off < size; off += CHUNK, n++) {
        const buf = Buffer.alloc(Math.min(CHUNK, size - off));
        await fh.read(buf, 0, buf.length, off);
        const r = await this.fetch(this.url(form.chunks.put.replace("{n}", n)), { method: "PUT", body: buf });
        if (!r.ok) throw new ApiError(r.status, "UPLOAD_FAILED", await r.text());
      }
    } finally { await fh.close(); }
    const r = await this.fetch(this.url(form.chunks.complete), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ filename: basename(path), chunks: n }) });
    if (!r.ok) throw new ApiError(r.status, "UPLOAD_FAILED", await r.text());
    return r.json();
  }

  async download(url, dest) {
    const r = await this.fetch(url);
    if (!r.ok) throw new ApiError(r.status, "DOWNLOAD_FAILED", r.statusText);
    await writeFile(dest, Buffer.from(await r.arrayBuffer()));
    return dest;
  }

  static verifyWebhook(secret, body, signature, timestamp, toleranceS = 300) {
    if (Math.abs(Date.now() / 1000 - Number(timestamp)) > toleranceS) return false;
    const expect = "sha256=" + createHmac("sha256", secret).update(`${timestamp}.`).update(body).digest("hex");
    const a = Buffer.from(expect), b = Buffer.from(String(signature));
    return a.length === b.length && timingSafeEqual(a, b);
  }
}
