"""Python SDK for the /v1 REST API (jobs and tasks, uploads, webhooks).

    from aice import Client
    c = Client("https://example.com", api_key="sk_…")
    job = c.convert(url="https://…/file.docx", output_format="pdf")
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Union

import httpx

__all__ = ["Client", "ApiError"]
__version__ = "1.0.0"

CHUNK = 50 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class _Jobs:
    def __init__(self, c: "Client"):
        self._c = c

    def create(self, tasks: dict[str, dict[str, Any]], tag: Optional[str] = None, webhook_url: Optional[str] = None) -> dict:
        body: dict[str, Any] = {"tasks": tasks}
        if tag:
            body["tag"] = tag
        if webhook_url:
            body["webhook_url"] = webhook_url
        return self._c._req("POST", "/jobs", json=body)

    def get(self, job_id: str, wait: int = 0) -> dict:
        return self._c._req("GET", f"/jobs/{job_id}", params={"wait": wait} if wait else None)

    def list(self, status: Optional[str] = None, tag: Optional[str] = None, limit: int = 50) -> list[dict]:
        params = {k: v for k, v in {"status": status, "tag": tag, "limit": limit}.items() if v}
        return self._c._req("GET", "/jobs", params=params)["data"]

    def cancel(self, job_id: str) -> dict:
        return self._c._req("DELETE", f"/jobs/{job_id}")

    def wait(self, job_id: str, timeout: float = 600) -> dict:
        """Long-poll until the job is finished or failed (raises ApiError on failure)."""
        end = time.time() + timeout
        while True:
            job = self.get(job_id, wait=min(60, max(1, int(end - time.time()))))
            if job["status"] in ("finished", "error", "cancelled"):
                if job["status"] != "finished":
                    bad = next((t for t in job["tasks"] if t["status"] == "error"), {})
                    raise ApiError(422, bad.get("code") or "JOB_FAILED", bad.get("message") or "the job failed")
                return job
            if time.time() > end:
                raise TimeoutError(f"job {job_id} still {job['status']}")

    @staticmethod
    def files(job: dict, task: Optional[str] = None) -> list[dict]:
        """Files of the export tasks (or of the named task)."""
        out = []
        for t in job["tasks"]:
            if (task and t["name"] == task) or (not task and t["operation"].startswith("export/")):
                out += (t.get("result") or {}).get("files") or []
        return out


class _Webhooks:
    def __init__(self, c: "Client"):
        self._c = c

    def list(self) -> dict:
        return self._c._req("GET", "/webhooks")

    def create(self, url: str, events: Iterable[str] = ("job.finished", "job.failed")) -> dict:
        return self._c._req("POST", "/webhooks", json={"url": url, "events": list(events)})

    def delete(self, hook_id: int) -> dict:
        return self._c._req("DELETE", f"/webhooks/{hook_id}")


class _Signatures:
    """Saved signatures (stored encrypted for your account)."""

    def __init__(self, c: "Client"):
        self._c = c

    def list(self) -> list[dict]:
        return self._c._req("GET", "/signatures")["data"]

    def image(self, signature_id: str) -> bytes:
        r = self._c.http.get(self._c._url(f"/signatures/{signature_id}/image"), headers={"Authorization": f"Bearer {self._c.api_key}"})
        if r.status_code >= 400:
            raise ApiError(r.status_code, "NOT_FOUND", r.text[:200])
        return r.content

    def delete(self, signature_id: str) -> dict:
        return self._c._req("DELETE", f"/signatures/{signature_id}")


class _Profile:
    """Your data profile for agentic form filling (stored encrypted)."""

    def __init__(self, c: "Client"):
        self._c = c

    def get(self) -> dict:
        return self._c._req("GET", "/profile")["data"]

    def update(self, data: dict, merge: bool = True) -> dict:
        return self._c._req("PUT", "/profile", params={"merge": str(merge).lower()}, json=data)["data"]

    def delete(self) -> dict:
        return self._c._req("DELETE", "/profile")


class Client:
    def __init__(self, base_url: str, api_key: Optional[str] = None, http: Optional[httpx.Client] = None, timeout: float = 120):
        self.base = base_url.rstrip("/") + "/v1"
        self.api_key = api_key or os.environ.get("AICE_API_KEY", "")
        self._own = http is None
        self.http = http or httpx.Client(timeout=timeout)
        self.jobs = _Jobs(self)
        self.webhooks = _Webhooks(self)
        self.signatures = _Signatures(self)
        self.profile = _Profile(self)

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else self.base + path

    def _req(self, method: str, path: str, **kw) -> Any:
        r = self.http.request(method, self._url(path), headers={"Authorization": f"Bearer {self.api_key}"}, **kw)
        if r.status_code >= 400:
            try:
                e = r.json()["error"]
                raise ApiError(r.status_code, e.get("code", "ERROR"), e.get("message", ""))
            except (ValueError, KeyError):
                raise ApiError(r.status_code, "ERROR", r.text[:300])
        return r.json()

    def me(self) -> dict:
        return self._req("GET", "/me")

    def formats(self, category: Optional[str] = None) -> list[dict]:
        return self._req("GET", "/catalog/formats", params={"category": category} if category else None)["formats"]

    def targets(self, source_format: str) -> list[str]:
        return self._req("GET", "/catalog/conversions", params={"from": source_format})["targets"]

    def convert(self, output_format: str, url: Optional[str] = None, path: Union[str, Path, None] = None,
                options: Optional[dict] = None, wait: bool = True) -> dict:
        """One-step conversion of a URL or a local file."""
        if url:
            job = self._req("POST", "/convert", json={"url": url, "output_format": output_format, "options": options or {},
                                                      "wait": 60 if wait else 0})
        elif path:
            job = self.jobs.create({"up": {"operation": "import/upload"},
                                    "convert": {"operation": "convert", "input": "up", "output_format": output_format,
                                                "options": options or {}},
                                    "export": {"operation": "export/url", "input": "convert"}})
            self.upload(job, "up", path)
        else:
            raise ValueError("give url or path")
        return self.jobs.wait(job["id"]) if wait and job["status"] not in ("finished", "error") else job

    def tool(self, tool: str, path: Union[str, Path, None] = None, url: Optional[str] = None,
             options: Optional[dict] = None, wait: bool = True) -> dict:
        """Run one tool (e.g. "compress-pdf", "fill-form", "sign-document") on a local file or a URL."""
        first = {"operation": "import/url", "url": url} if url else {"operation": "import/upload"} if path else None
        tasks: dict[str, dict] = {"run": {"operation": f"tool/{tool}", "options": options or {}}}
        if first:
            tasks = {"in": first, "run": {**tasks["run"], "input": "in"}}
        tasks["out"] = {"operation": "export/url", "input": "run"}
        job = self.jobs.create(tasks)
        if path and not url:
            self.upload(job, "in", path)
        return self.jobs.wait(job["id"]) if wait else job

    def upload(self, job: dict, task_name: str, path: Union[str, Path], timeout: float = 30) -> dict:
        """Upload a local file to an import/upload task (chunked above 50 MB)."""
        path = Path(path)
        task = next(t for t in job["tasks"] if t["name"] == task_name)
        end = time.time() + timeout
        while not ((task.get("result") or {}).get("form")):
            if time.time() > end:
                raise TimeoutError("the upload task didn't become ready")
            time.sleep(0.2)
            task = self._req("GET", f"/tasks/{task['id']}")
        form = task["result"]["form"]
        size = path.stat().st_size
        if size <= CHUNK:
            with open(path, "rb") as f:
                r = self.http.post(self._url(form["url"]), files={"file": (path.name, f)})
        else:
            with open(path, "rb") as f:
                n = 0
                while chunk := f.read(CHUNK):
                    r = self.http.put(self._url(form["chunks"]["put"].replace("{n}", str(n))), content=chunk)
                    if r.status_code >= 400:
                        raise ApiError(r.status_code, "UPLOAD_FAILED", r.text[:200])
                    n += 1
            r = self.http.post(self._url(form["chunks"]["complete"]), json={"filename": path.name, "chunks": n})
        if r.status_code >= 400:
            raise ApiError(r.status_code, "UPLOAD_FAILED", r.text[:200])
        return r.json()

    def download(self, url: str, dest: Union[str, Path]) -> Path:
        dest = Path(dest)
        with self.http.stream("GET", url if url.startswith("http") else self.base.rsplit("/v1", 1)[0] + url) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        return dest

    @staticmethod
    def verify_webhook(secret: str, body: bytes, signature: str, timestamp: str, tolerance: int = 300) -> bool:
        if abs(time.time() - int(timestamp)) > tolerance:
            return False
        expect = "sha256=" + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expect, signature)

    def close(self) -> None:
        if self._own:
            self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
