class AiceApi {
  constructor() {
    this.name = "aiceApi";
    this.displayName = "AICE API";
    this.documentationUrl = "https://your-domain/developers/docs";
    this.properties = [
      { displayName: "Base URL", name: "baseUrl", type: "string", default: "", placeholder: "https://your-domain", required: true },
      { displayName: "API Key", name: "apiKey", type: "string", typeOptions: { password: true }, default: "", required: true },
    ];
    this.authenticate = { type: "generic", properties: { headers: { Authorization: "=Bearer {{$credentials.apiKey}}" } } };
    this.test = { request: { baseURL: "={{$credentials.baseUrl}}/v1", url: "/me" } };
  }
}
module.exports = { AiceApi };
