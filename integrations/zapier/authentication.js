// Custom auth: the user's base URL + API key; tested with GET /v1/me.
module.exports = {
  type: "custom",
  fields: [
    { key: "baseUrl", label: "Base URL", required: true, helpText: "Address of the service, e.g. https://your-domain" },
    { key: "apiKey", label: "API key", required: true, type: "password", helpText: "Create one in the app: Connect your AI → API keys (sk_… or sk_test_…)" },
  ],
  test: { url: "{{bundle.authData.baseUrl}}/v1/me", method: "GET" },
  connectionLabel: (z, bundle) => `${bundle.authData.baseUrl} (${bundle.inputData.plan || "account"})`,
};
