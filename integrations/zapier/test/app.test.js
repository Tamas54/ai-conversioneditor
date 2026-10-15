const { test } = require("node:test");
const assert = require("node:assert/strict");
const app = require("../index.js");

function zMock(responses) {
  const calls = [];
  return {
    calls,
    request: async (req) => { calls.push(req); return { data: responses.shift() }; },
    errors: { Error: class extends Error { constructor(m, c) { super(m); this.code = c; } } },
  };
}

test("app shape", () => {
  assert.ok(app.creates.convert_file && app.creates.run_job && app.triggers.job_finished);
  const req = app.beforeRequest[0]({ headers: {} }, null, { authData: { apiKey: "sk_x" } });
  assert.equal(req.headers.Authorization, "Bearer sk_x");
});

test("convert_file returns the exported file", async () => {
  const z = zMock([{ id: "j", status: "finished", tasks: [{ operation: "export/url", status: "finished", result: { files: [{ filename: "a.pdf", url: "https://x/a.pdf", size: 3, format: "pdf" }] } }] }]);
  const out = await app.creates.convert_file.operation.perform(z, { authData: { baseUrl: "https://x.test/" }, inputData: { file: "https://e/a.docx", output_format: "pdf", options: "{\"page_range\":\"1\"}" } });
  assert.equal(out.url, "https://x/a.pdf");
  assert.equal(z.calls[0].url, "https://x.test/v1/convert");
  assert.deepEqual(z.calls[0].body.options, { page_range: "1" });
});

test("convert_file surfaces the failing task", async () => {
  const z = zMock([{ id: "j", status: "error", tasks: [{ operation: "convert", status: "error", code: "CONVERSION_NOT_POSSIBLE", message: "no" }] }]);
  await assert.rejects(app.creates.convert_file.operation.perform(z, { authData: { baseUrl: "https://x" }, inputData: { file: "u", output_format: "zzz" } }),
    (e) => e.code === "CONVERSION_NOT_POSSIBLE");
});

test("hook subscribe/unsubscribe", async () => {
  const z = zMock([{ id: 7 }, { deleted: 7 }]);
  const op = app.triggers.job_finished.operation;
  const sub = await op.performSubscribe(z, { authData: { baseUrl: "https://x" }, targetUrl: "https://hooks.zapier.com/1" });
  assert.equal(sub.id, 7);
  await op.performUnsubscribe(z, { authData: { baseUrl: "https://x" }, subscribeData: { id: 7 } });
  assert.equal(z.calls[1].url, "https://x/v1/webhooks/7");
  assert.equal(op.perform(z, { cleanedRequest: { event: "job.finished", job: { id: "j1" } } })[0].id, "j1");
});
