# aice — Python SDK

```bash
pip install ./sdk/python
```

```python
from aice import Client

c = Client("https://your-domain", api_key="sk_…")          # sk_test_… = sandbox, no credits

# one call: URL in, converted file out
job = c.convert(url="https://example.com/report.docx", output_format="pdf", options={"page_range": "1-3"})
for f in c.jobs.files(job):
    c.download(f["url"], f["filename"])

# local file, any pipeline
job = c.jobs.create({
    "up":  {"operation": "import/upload"},
    "jpg": {"operation": "convert", "input": "up", "output_format": "jpg", "options": {"quality": 85}},
    "out": {"operation": "export/url", "input": "jpg"},
})
c.upload(job, "up", "photo.heic")                         # chunked automatically above 50 MB
job = c.jobs.wait(job["id"])

# webhooks
c.webhooks.create("https://example.com/hook")
ok = Client.verify_webhook(secret, request_body, headers["X-Signature"], headers["X-Webhook-Timestamp"])
```
