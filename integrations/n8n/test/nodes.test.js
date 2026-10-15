const { test } = require("node:test");
const assert = require("node:assert/strict");
const { Aice } = require("../nodes/Aice/Aice.node.js");
const { AiceTrigger } = require("../nodes/Aice/AiceTrigger.node.js");
const { AiceApi } = require("../credentials/AiceApi.credentials.js");

function ctx(params, responses) {
  const calls = [];
  return {
    calls,
    getInputData: () => [{ json: {} }],
    getNodeParameter: (n, i, d) => (n in params ? params[n] : d),
    getCredentials: async () => ({ baseUrl: "https://x.test/", apiKey: "sk_test_1" }),
    helpers: { httpRequestWithAuthentication: async function (name, req) { calls.push(req); return responses.shift(); } },
  };
}

test("credentials send a bearer key", () => {
  const c = new AiceApi();
  assert.equal(c.authenticate.properties.headers.Authorization, "=Bearer {{$credentials.apiKey}}");
});

test("convert operation posts to /v1/convert and lists files", async () => {
  const node = new Aice();
  const c = ctx({ operation: "convert", url: "https://e/a.docx", outputFormat: "pdf", options: "{\"page_range\":\"1\"}" },
    [{ id: "j", status: "finished", tasks: [{ operation: "export/url", result: { files: [{ url: "https://x/a.pdf" }] } }] }]);
  const [[item]] = await node.execute.call(c);
  assert.equal(c.calls[0].url, "https://x.test/v1/convert");
  assert.deepEqual(c.calls[0].body.options, { page_range: "1" });
  assert.equal(item.json.files[0].url, "https://x/a.pdf");
});

test("trigger registers and removes its webhook", async () => {
  const t = new AiceTrigger();
  const data = {};
  const c = { ...ctx({ events: ["job.finished"] }, [{ id: 9 }, {}]), getWorkflowStaticData: () => data, getNodeWebhookUrl: () => "https://n8n/hook" };
  await t.webhookMethods.default.create.call(c);
  assert.equal(data.hookId, 9);
  await t.webhookMethods.default.delete.call(c);
  assert.equal(c.calls[1].url, "https://x.test/v1/webhooks/9");
  const out = await t.webhook.call({ getBodyData: () => ({ event: "job.finished", job: { id: "j1" } }) });
  assert.equal(out.workflowData[0][0].json.id, "j1");
});
