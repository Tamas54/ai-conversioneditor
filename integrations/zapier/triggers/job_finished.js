// Trigger (REST hook): a job finished or failed. Subscribes via POST /v1/webhooks.
const subscribe = async (z, bundle) => {
  const base = bundle.authData.baseUrl.replace(/\/$/, "");
  const r = await z.request({ url: `${base}/v1/webhooks`, method: "POST", body: { url: bundle.targetUrl, events: ["job.finished", "job.failed"] } });
  return r.data || r.json;
};
const unsubscribe = async (z, bundle) => {
  const base = bundle.authData.baseUrl.replace(/\/$/, "");
  const id = bundle.subscribeData.id;
  const r = await z.request({ url: `${base}/v1/webhooks/${id}`, method: "DELETE" });
  return r.data || r.json;
};
const perform = (z, bundle) => {
  const p = bundle.cleanedRequest || {};
  return [{ id: p.job && p.job.id, event: p.event, ...(p.job || {}) }];
};
const performList = async (z, bundle) => {
  const base = bundle.authData.baseUrl.replace(/\/$/, "");
  const r = await z.request({ url: `${base}/v1/jobs`, params: { limit: 3 } });
  return ((r.data || r.json).data || []).map((j) => ({ ...j, event: j.status === "finished" ? "job.finished" : "job.failed" }));
};

module.exports = {
  key: "job_finished",
  noun: "Job",
  display: { label: "Job Finished", description: "Triggers when a processing job finishes or fails." },
  operation: { type: "hook", performSubscribe: subscribe, performUnsubscribe: unsubscribe, perform, performList,
    sample: { id: "abc123", event: "job.finished", status: "finished", tasks: [] } },
};
