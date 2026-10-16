// Action: convert a file (by URL, e.g. from a previous Zap step) to any supported format; waits up to 5 minutes.
const perform = async (z, bundle) => {
  const base = bundle.authData.baseUrl.replace(/\/$/, "");
  let options = {};
  if (bundle.inputData.options) options = typeof bundle.inputData.options === "string" ? JSON.parse(bundle.inputData.options) : bundle.inputData.options;
  const r = await z.request({ url: `${base}/v1/convert`, method: "POST",
    body: { url: bundle.inputData.file, output_format: bundle.inputData.output_format, options, wait: 300 } });
  const job = r.data || r.json;
  const exp = (job.tasks || []).find((t) => t.operation === "export/url");
  const file = exp && exp.result && exp.result.files && exp.result.files[0];
  if (job.status !== "finished" || !file) {
    const bad = (job.tasks || []).find((t) => t.status === "error");
    throw new z.errors.Error((bad && bad.message) || `job ${job.id} is ${job.status}`, (bad && bad.code) || "JOB_FAILED");
  }
  return { job_id: job.id, filename: file.filename, size: file.size, format: file.format, url: file.url, file: z.dehydrateFile ? z.dehydrateFile(file.url) : file.url };
};

module.exports = {
  key: "convert_file",
  noun: "File",
  display: { label: "Convert File", description: "Converts a file to another format (documents, images, video, audio, e-books, archives, CAD, fonts)." },
  operation: {
    inputFields: [
      { key: "file", label: "File URL", required: true, helpText: "A public URL or a file from a previous step." },
      { key: "output_format", label: "Output format", required: true, helpText: "e.g. pdf, docx, jpg, webp, mp4, mp3, epub, zip" },
      { key: "options", label: "Options (JSON)", required: false, helpText: "e.g. {\"quality\": 85, \"page_range\": \"1-3\"}" },
    ],
    perform,
    sample: { job_id: "abc123", filename: "report.pdf", size: 18342, format: "pdf", url: "https://example.com/files/report.pdf" },
  },
};
