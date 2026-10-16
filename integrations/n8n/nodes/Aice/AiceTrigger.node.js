// n8n trigger node: a job finished or failed (registers a webhook with POST /v1/webhooks).
class AiceTrigger {
  constructor() {
    this.description = {
      displayName: "AICE Trigger",
      name: "aiceTrigger",
      group: ["trigger"],
      version: 1,
      description: "Starts the workflow when a job finishes or fails",
      defaults: { name: "AICE Trigger" },
      inputs: [],
      outputs: ["main"],
      credentials: [{ name: "aiceApi", required: true }],
      webhooks: [{ name: "default", httpMethod: "POST", responseMode: "onReceived", path: "webhook" }],
      properties: [{ displayName: "Events", name: "events", type: "multiOptions", default: ["job.finished", "job.failed"],
        options: [{ name: "Job Finished", value: "job.finished" }, { name: "Job Failed", value: "job.failed" }] }],
    };
    this.webhookMethods = {
      default: {
        async checkExists() { return Boolean(this.getWorkflowStaticData("node").hookId); },
        async create() {
          const creds = await this.getCredentials("aiceApi");
          const r = await this.helpers.httpRequestWithAuthentication.call(this, "aiceApi", { method: "POST", json: true,
            url: String(creds.baseUrl).replace(/\/$/, "") + "/v1/webhooks",
            body: { url: this.getNodeWebhookUrl("default"), events: this.getNodeParameter("events") } });
          this.getWorkflowStaticData("node").hookId = r.id;
          return true;
        },
        async delete() {
          const data = this.getWorkflowStaticData("node");
          if (!data.hookId) return true;
          const creds = await this.getCredentials("aiceApi");
          await this.helpers.httpRequestWithAuthentication.call(this, "aiceApi", { method: "DELETE", json: true,
            url: String(creds.baseUrl).replace(/\/$/, "") + `/v1/webhooks/${data.hookId}` });
          delete data.hookId;
          return true;
        },
      },
    };
  }

  async webhook() {
    const body = this.getBodyData();
    return { workflowData: [[{ json: { event: body.event, ...(body.job || {}) } }]] };
  }
}
module.exports = { AiceTrigger };
