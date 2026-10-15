"""MCP: a bővített convert (univerzális út, a régi út változatlan), és a csoportosított új eszközök."""
from __future__ import annotations

import json

import pytest

HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _call(client, name, args):
    client.post("/mcp/", headers=HDR, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    r = client.post("/mcp/", headers=HDR, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                "params": {"name": name, "arguments": args}})
    res = r.json()["result"]
    text = res["content"][0]["text"]
    return res.get("isError"), (json.loads(text) if text.startswith("{") else text)


def test_new_tools_listed_and_annotated(client):
    r = client.post("/mcp/", headers=HDR, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    for name in ("file_tools", "transfer", "jobs", "catalog", "units", "convert"):
        assert name in tools
    assert tools["catalog"]["annotations"]["readOnlyHint"] is True
    assert "264 formats" in tools["convert"]["description"]


def test_convert_universal_png_to_webp(client, samples, put_file):
    fid = put_file(samples.png())
    err, res = _call(client, "convert", {"file_id": fid, "target_format": "webp", "options": {"quality": 70}})
    assert not err, res
    assert res["file_id"].endswith(".webp") and res["edit_url"] and res["engines"] == ["pillow"]


def test_convert_legacy_path_unchanged(client, samples, put_file, monkeypatch):
    """md → html: a régi út (t_convert) fut, nem az új gráf."""
    from app import mcp_server
    called = {}

    async def fake(pool, **kw):
        called.update(kw)
        from app.storage import output_path, new_file_id, public_url
        fid = new_file_id("html")
        output_path(fid).write_text("<p>ő</p>", "utf-8")
        return {"file_id": fid, "url": public_url(fid), "backend": "pandoc"}
    monkeypatch.setattr(mcp_server, "t_convert", fake)
    fid = put_file(samples.md())
    err, res = _call(client, "convert", {"file_id": fid, "target_format": "html"})
    assert not err and called["target_format"] == "html" and res["backend"] == "pandoc"


def test_catalog_and_units(client):
    err, res = _call(client, "catalog", {"kind": "conversions", "source_format": "heic", "target_format": "jpg"})
    assert res["possible"] is True and "quality" in res["options"]
    err, res = _call(client, "units", {"value": 1, "from_unit": "mi", "to_unit": "km"})
    assert res["result"] == 1.609344
    err, res = _call(client, "units", {"from_zone": "CET", "to_zones": ["UTC"], "time": "2026-07-01 12:00"})
    assert res["to"][0]["time"].startswith("2026-07-01T10:00")


def test_jobs_tool(client):
    import base64
    tasks = {"in": {"operation": "import/base64", "file": base64.b64encode("# Ő\n".encode()).decode(), "filename": "a.md"},
             "h": {"operation": "convert", "input": "in", "output_format": "html"}}
    err, res = _call(client, "jobs", {"action": "create", "tasks": tasks, "wait": 30})
    assert not err and res["status"] == "finished" and res["files"][0]["file_id"].endswith(".html")


def test_transfer_import_url(client, monkeypatch):
    from app import net

    async def fake_download(url, dst, max_bytes, headers=None, timeout=120):
        dst.write_bytes("letöltött ő".encode())
        return "lap.txt", dst.stat().st_size, "text/plain"
    monkeypatch.setattr(net, "download", fake_download)
    err, res = _call(client, "transfer", {"action": "import", "provider": "url", "url": "https://example.com/lap.txt"})
    assert not err and res["file_id"].endswith(".txt") and res["label"] == "lap.txt"
    err, res = _call(client, "transfer", {"action": "export", "provider": "url", "file_ids": [res["file_id"]]})
    assert res["files"][0]["url"].startswith("http")


def test_agent_has_universal_tools():
    from app.llm import agent
    names = {s["function"]["name"] for s in agent.TOOL_SCHEMAS}
    assert {"file_tool", "convert_any", "units_convert", "convert_format"} <= names
