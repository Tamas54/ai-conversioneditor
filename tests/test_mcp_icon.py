"""A bővítmény-/connector-lista ikonja (ChatGPT, Claude): serverInfo `icons` + belépés nélküli PNG-ikonok a hoszt
gyökerén (10-17: a ChatGPT bővítménylistáján üres „@” jel volt a Studio helyén)."""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("path", ["/favicon.ico", "/apple-touch-icon.png", "/icon-192.png", "/icon-512.png"])
def test_icons_are_public_pngs(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 200, (path, r.status_code)
    assert r.headers["content-type"].startswith("image/png")
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_server_info_carries_icons(client):
    r = client.post("/mcp/", headers={"accept": "application/json, text/event-stream", "content-type": "application/json"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "0"}}})
    assert r.status_code == 200, r.text[:300]
    info = r.json()["result"]["serverInfo"]
    icons = info.get("icons") or []
    assert any(i.get("mimeType") == "image/png" and i.get("sizes") == ["512x512"] for i in icons), info
    assert info.get("websiteUrl", "").startswith("http")
    for i in icons:                                       # minden ikon-cím tényleg elérhető, belépés nélkül
        path = "/" + i["src"].split("/", 3)[3]
        assert client.get(path).status_code == 200, path
