import { test } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { Client, ApiError } from "../index.js";

function fakeFetch(routes) {
  const calls = [];
  const f = async (url, init = {}) => {
    calls.push({ url, init });
    const key = `${init.method || "GET"} ${url.replace(/^https:\/\/x\.test\/v1/, "")}`;
    const hit = Object.entries(routes).find(([k]) => key.startsWith(k));
    const [status, body] = hit ? hit[1](init) : [404, { error: { code: "NOT_FOUND", message: "no" } }];
    return { ok: status < 400, status, statusText: "", json: async () => body, text: async () => JSON.stringify(body) };
  };
  f.calls = calls;
  return f;
}

test("convert by URL waits for the job and returns files", async () => {
  const job = { id: "j1", status: "processing", tasks: [] };
  const done = { id: "j1", status: "finished", tasks: [{ name: "export", operation: "export/url", status: "finished", result: { files: [{ filename: "a.pdf", url: "https://x.test/files/a" }] } }] };
  const f = fakeFetch({ "POST /convert": () => [200, job], "GET /jobs/j1": () => [200, done] });
  const c = new Client("https://x.test", { apiKey: "sk_test_1", fetch: f });
  const out = await c.convert({ url: "https://e.com/a.docx", outputFormat: "pdf" });
  assert.equal(out.status, "finished");
  assert.deepEqual(c.jobs.files(out).map((x) => x.filename), ["a.pdf"]);
  assert.equal(f.calls[0].init.headers.Authorization, "Bearer sk_test_1");
  assert.equal(JSON.parse(f.calls[0].init.body).output_format, "pdf");
});

test("errors carry the API code", async () => {
  const f = fakeFetch({ "POST /jobs": () => [422, { error: { code: "UNKNOWN_OPERATION", message: "x" } }] });
  const c = new Client("https://x.test", { apiKey: "k", fetch: f });
  await assert.rejects(c.jobs.create({ a: { operation: "nope" } }), (e) => e instanceof ApiError && e.code === "UNKNOWN_OPERATION");
});

test("webhook signature check", () => {
  const ts = Math.floor(Date.now() / 1000), body = '{"event":"job.finished"}';
  const sig = "sha256=" + createHmac("sha256", "whsec_x").update(`${ts}.${body}`).digest("hex");
  assert.ok(Client.verifyWebhook("whsec_x", body, sig, ts));
  assert.ok(!Client.verifyWebhook("whsec_y", body, sig, ts));
});

test("tool runs one tool on a URL and the profile is read and written", async () => {
  const done = { id: "j2", status: "finished", tasks: [{ name: "out", operation: "export/url", status: "finished", result: { files: [{ filename: "a-signed.pdf" }] } }] };
  const f = fakeFetch({ "POST /jobs": () => [201, { id: "j2", status: "processing", tasks: [] }], "GET /jobs/j2": () => [200, done],
    "PUT /profile": (init) => [200, { data: JSON.parse(init.body) }], "GET /profile": () => [200, { data: { full_name: "Ödön" } }] });
  const c = new Client("https://x.test", { apiKey: "k", fetch: f });
  const job = await c.tool({ tool: "sign-document", url: "https://e.com/a.pdf", options: { signature_id: "sig_x" } });
  assert.deepEqual(c.jobs.files(job).map((x) => x.filename), ["a-signed.pdf"]);
  const tasks = JSON.parse(f.calls[0].init.body).tasks;
  assert.equal(tasks.run.operation, "tool/sign-document");
  assert.equal(tasks.run.input, "in");
  assert.equal((await c.profile.update({ full_name: "Ödön" })).full_name, "Ödön");
  assert.equal((await c.profile.get()).full_name, "Ödön");
});
