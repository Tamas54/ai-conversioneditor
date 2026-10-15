"""A Python-SDK a valódi apphoz (TestClient-en át), a Node-SDK és a Zapier/n8n/Make-integráció a saját node-tesztjeivel."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sdk" / "python"))


def test_python_sdk_end_to_end(client, monkeypatch, samples, tmp_path):
    from aice import ApiError, Client
    from app.config import settings
    monkeypatch.setattr(settings, "MCP_AUTH_TOKEN", "sdk-key")
    c = Client("http://testserver", api_key="sdk-key", http=client)
    assert "html" in c.targets("md")
    job = c.convert(output_format="html", path=samples.md())
    files = c.jobs.files(job)
    assert files and files[0]["filename"].endswith(".html")
    out = c.download(files[0]["url"], tmp_path / "out.html")
    assert "Árvíztűrő" in out.read_text("utf-8")
    with pytest.raises(ApiError) as e:
        c.jobs.create({"a": {"operation": "nope"}})
    assert e.value.code == "UNKNOWN_OPERATION"
    with pytest.raises(ApiError):
        c.convert(output_format="mp4", path=samples.txt())


def test_python_sdk_webhook_verify():
    import hashlib
    import hmac
    import time
    from aice import Client
    ts = str(int(time.time()))
    body = b'{"event":"job.finished"}'
    sig = "sha256=" + hmac.new(b"whsec_x", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert Client.verify_webhook("whsec_x", body, sig, ts) and not Client.verify_webhook("whsec_y", body, sig, ts)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("folder", ["sdk/node", "integrations/zapier", "integrations/n8n"])
def test_node_packages(folder):
    tests = sorted(str(p) for p in (ROOT / folder / "test").glob("*.test.js"))
    r = subprocess.run(["node", "--test", *tests], cwd=ROOT / folder, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]


def test_make_app_files_are_valid_json():
    make = ROOT / "integrations" / "make"
    app = json.loads((make / "app.json").read_text())
    for rel in app["connections"] + app["modules"] + app["webhooks"] + app["rpcs"]:
        json.loads((make / rel).read_text())
