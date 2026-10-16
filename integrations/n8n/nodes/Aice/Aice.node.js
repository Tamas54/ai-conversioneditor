// n8n action node: Convert, Run job, Get job, Convert units.
class Aice {
  constructor() {
    this.description = {
      displayName: "AICE",
      name: "aice",
      group: ["transform"],
      version: 1,
      subtitle: '={{$parameter["operation"]}}',
      description: "Convert and process files with the AICE API",
      defaults: { name: "AICE" },
      inputs: ["main"],
      outputs: ["main"],
      credentials: [{ name: "aiceApi", required: true }],
      properties: [
        { displayName: "Operation", name: "operation", type: "options", noDataExpression: true, default: "convert",
          options: [
            { name: "Convert File", value: "convert", action: "Convert a file" },
            { name: "Run Job", value: "runJob", action: "Run a processing job" },
            { name: "Get Job", value: "getJob", action: "Get a job" },
            { name: "Convert Units", value: "units", action: "Convert a value between units" },
          ] },
        { displayName: "File URL", name: "url", type: "string", default: "", required: true, displayOptions: { show: { operation: ["convert"] } } },
        { displayName: "Output Format", name: "outputFormat", type: "string", default: "pdf", required: true, displayOptions: { show: { operation: ["convert"] } } },
        { displayName: "Options (JSON)", name: "options", type: "json", default: "{}", displayOptions: { show: { operation: ["convert"] } } },
        { displayName: "Tasks (JSON)", name: "tasks", type: "json", default: "{}", required: true, displayOptions: { show: { operation: ["runJob"] } } },
        { displayName: "Job ID", name: "jobId", type: "string", default: "", required: true, displayOptions: { show: { operation: ["getJob"] } } },
        { displayName: "Value", name: "value", type: "number", default: 1, displayOptions: { show: { operation: ["units"] } } },
        { displayName: "From Unit", name: "fromUnit", type: "string", default: "mi", displayOptions: { show: { operation: ["units"] } } },
        { displayName: "To Unit", name: "toUnit", type: "string", default: "km", displayOptions: { show: { operation: ["units"] } } },
        { displayName: "Wait (Seconds)", name: "wait", type: "number", default: 60, displayOptions: { show: { operation: ["convert", "runJob", "getJob"] } } },
      ],
    };
  }

  async execute() {
    const items = this.getInputData();
    const out = [];
    const creds = await this.getCredentials("aiceApi");
    const base = String(creds.baseUrl).replace(/\/$/, "") + "/v1";
    const call = (method, url, body, qs) => this.helpers.httpRequestWithAuthentication.call(this, "aiceApi",
      { method, url: base + url, body, qs, json: true });
    for (let i = 0; i < items.length; i++) {
      const op = this.getNodeParameter("operation", i);
      let res;
      if (op === "convert") {
        const opts = this.getNodeParameter("options", i, "{}");
        res = await call("POST", "/convert", { url: this.getNodeParameter("url", i), output_format: this.getNodeParameter("outputFormat", i),
          options: typeof opts === "string" ? JSON.parse(opts || "{}") : opts, wait: this.getNodeParameter("wait", i, 60) });
      } else if (op === "runJob") {
        const tasks = this.getNodeParameter("tasks", i);
        const job = await call("POST", "/jobs", { tasks: typeof tasks === "string" ? JSON.parse(tasks) : tasks });
        res = await call("GET", `/jobs/${job.id}`, undefined, { wait: this.getNodeParameter("wait", i, 60) });
      } else if (op === "getJob") {
        res = await call("GET", `/jobs/${this.getNodeParameter("jobId", i)}`, undefined, { wait: this.getNodeParameter("wait", i, 0) });
      } else {
        res = await call("GET", "/units/convert", undefined, { value: this.getNodeParameter("value", i), from: this.getNodeParameter("fromUnit", i),
          to: this.getNodeParameter("toUnit", i) });
      }
      if (res && res.tasks) {
        res.files = res.tasks.filter((t) => t.operation.startsWith("export/")).flatMap((t) => (t.result && t.result.files) || []);
      }
      out.push({ json: res });
    }
    return [out];
  }
}
module.exports = { Aice };
