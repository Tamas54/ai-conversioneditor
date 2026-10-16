const authentication = require("./authentication");
const convertFile = require("./creates/convert_file");
const runJob = require("./creates/run_job");
const jobFinished = require("./triggers/job_finished");

const addApiKey = (request, z, bundle) => {
  request.headers = request.headers || {};
  if (bundle.authData && bundle.authData.apiKey) request.headers.Authorization = `Bearer ${bundle.authData.apiKey}`;
  return request;
};

const handleErrors = (response, z) => {
  if (response.status >= 400) {
    let code = "ERROR", message = response.content;
    try { const e = JSON.parse(response.content).error; code = e.code; message = e.message; } catch (_) { /* nyers szöveg */ }
    throw new z.errors.Error(message, code, response.status);
  }
  return response;
};

module.exports = {
  version: require("./package.json").version,
  platformVersion: "15.19.0",
  authentication,
  beforeRequest: [addApiKey],
  afterResponse: [handleErrors],
  triggers: { [jobFinished.key]: jobFinished },
  creates: { [convertFile.key]: convertFile, [runJob.key]: runJob },
};
