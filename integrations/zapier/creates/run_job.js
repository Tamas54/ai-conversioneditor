// Action: run any job (pipeline of named tasks, JSON), optionally waiting for it.
const perform = async (z, bundle) => {
  const base = bundle.authData.baseUrl.replace(/\/$/, "");
  const tasks = typeof bundle.inputData.tasks === "string" ? JSON.parse(bundle.inputData.tasks) : bundle.inputData.tasks;
  const r = await z.request({ url: `${base}/v1/jobs`, method: "POST", body: { tasks, tag: bundle.inputData.tag || undefined } });
  let job = r.data || r.json;
  if (bundle.inputData.wait !== false && bundle.inputData.wait !== "false") {
    const w = await z.request({ url: `${base}/v1/jobs/${job.id}`, params: { wait: 60 } });
    job = w.data || w.json;
  }
  const files = (job.tasks || []).filter((t) => t.operation.startsWith("export/")).flatMap((t) => (t.result && t.result.files) || []);
  return { ...job, files, first_file_url: files[0] && files[0].url };
};

module.exports = {
  key: "run_job",
  noun: "Job",
  display: { label: "Run Job", description: "Runs a processing job (import → convert/tool → export) built in the Job Builder." },
  operation: {
    inputFields: [
      { key: "tasks", label: "Tasks (JSON)", required: true, helpText: "The \"tasks\" object from the Job Builder." },
      { key: "tag", label: "Tag", required: false },
      { key: "wait", label: "Wait for the result", type: "boolean", default: "true" },
    ],
    perform,
    sample: { id: "abc123", status: "finished", tasks: [], files: [], first_file_url: "https://example.com/files/x.pdf" },
  },
};
