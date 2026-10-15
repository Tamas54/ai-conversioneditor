"""WebMCP: az oldalak a böngészős AI-ügynököknek is felkínálják az eszközeiket (document.modelContext)."""
from __future__ import annotations

import os
import socket
import threading
import time

import pytest

TOOLS = {"list_formats", "list_tools", "list_my_files", "import_file", "convert_file", "run_tool", "open_page"}


def test_catalog_endpoint_describes_tools_and_formats(client):
    c = client.get("/api/catalog").json()
    assert c["formats"] > 200 and c["categories"]["document"] > 10
    t = {x["key"]: x for x in c["tools"]}
    assert t["compress-pdf"]["name"] == "Compress PDF" and t["compress-pdf"]["accepts"] == ["pdf"]
    assert t["compress-pdf"]["options"]["compression"]["enum"] == ["low", "recommended", "extreme"]
    assert t["fill-form"]["description"]


def test_every_page_loads_webmcp(client):
    for path in ("/app", "/convert/docx-to-pdf", "/tools/compress-pdf", "/ai-agents"):
        assert '<script src="/static/webmcp.js" defer></script>' in client.get(path).text, path
    js = client.get("/static/webmcp.js").text
    assert "document.modelContext || navigator.modelContext" in js and "registerTool" in js
    assert "WebMCP" in client.get("/ai-agents").text and "WebMCP" in client.get("/llms.txt").text


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_url():
    uvicorn = pytest.importorskip("uvicorn")
    from app.main import app
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(5)


_MOCK = """
window.__tools = {};
Object.defineProperty(Document.prototype, "modelContext", { configurable: true, get() {
  return window.__mc || (window.__mc = { registerTool(t, o) { window.__tools[t.name] = t; return Promise.resolve(); } });
} });
"""


@pytest.mark.skipif(not os.getenv("CHROMIUM_EXECUTABLE"), reason="Chromium needed")
def test_browser_agent_can_list_and_convert(live_url):
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        br = p.chromium.launch(executable_path=os.environ["CHROMIUM_EXECUTABLE"])
        page = br.new_page()
        page.add_init_script(_MOCK)
        page.goto(f"{live_url}/convert/md-to-html")
        page.wait_for_function("Object.keys(window.__tools).length >= 7", timeout=10000)
        assert set(page.evaluate("Object.keys(window.__tools)")) == TOOLS
        assert page.evaluate("window.__tools.list_formats.annotations.readOnlyHint") is True

        fmts = page.evaluate("window.__tools.list_formats.execute({from: 'md'})")
        assert "html" in [t["format"] for t in fmts["targets"]]
        tools = page.evaluate("window.__tools.list_tools.execute({query: 'compress pdf'})")
        assert "compress-pdf" in [t["key"] for t in tools["tools"]]

        res = page.evaluate("""async () => {
          const fd = new FormData();
          fd.append("file", new File(["# Árvíztűrő\\n\\nŐsz ű"], "jegyzet.md", { type: "text/markdown" }));
          const up = await (await fetch("/api/upload", { method: "POST", body: fd })).json();
          const r = await window.__tools.convert_file.execute({ file_id: up.file_id, to: "html" }, {});
          const u = new URL(r.result.download_url);          // a tesztben PUBLIC_BASE_URL = http://testserver
          const html = await (await fetch(u.pathname + u.search)).text();
          return { r, html };
        }""")
        assert "/files/" in res["r"]["result"]["download_url"] and "Árvíztűrő" in res["html"]
        mine = page.evaluate("window.__tools.list_my_files.execute({limit: 5})")
        assert any(f["file_id"] == res["r"]["result"]["file_id"] for f in mine["files"])
        err = page.evaluate("window.__tools.import_file.execute({url: 'http://127.0.0.1/secret'}).then(() => 'ok', e => e.message)")
        assert err != "ok"                                   # SSRF-őr: belső címről nem hoz be
        br.close()
