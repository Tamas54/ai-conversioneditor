"""Nagy fájlok (10-22, Kommandant: „Csináltam egy 2 perces kis videót az iPhone Pro Max 17-esemmel … 235 megabájt.
Ezt hogy a francba tudjuk feltölteni az appba? és EKKORA fájlt hogy tud értelmesen kezelni egy agent?”).

- az app feltöltése lemezre ír (eddig az egész fájl a memóriába került), a félkész fájl rejtett és eltűnik;
- a darabolt feltöltés folytatható: a szerver megmondja, mi érkezett meg; csonka darab nem számít megérkezettnek;
- az ügynök URL-ről is lemezre tölt; a base64-es darabolt feltöltés kis fájlokra való (korlát, útmutatással).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

CH = 8 * 1024 * 1024


def _video(path: Path, size: int) -> Path:
    """MOV-fejlécű (iPhone: ftyp qt) próbafájl a megadott méretben — a feltöltésnek a tartalom mindegy."""
    with open(path, "wb") as f:
        f.write(b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  ")
        left = size - 20
        block = os.urandom(1 << 20)
        while left > 0:
            f.write(block[:min(left, len(block))])
            left -= len(block)
    return path


def test_app_upload_streams_to_disk_and_leaves_nothing_half_written(client, tmp_path):
    from app.storage import input_path
    src = _video(tmp_path / "IMG_0001.MOV", 3 * 1024 * 1024 + 5)
    with open(src, "rb") as f:
        r = client.post("/api/upload", files={"file": ("IMG_0001.MOV", f, "video/quicktime")})
    assert r.status_code == 200, r.text
    fid = r.json()["file_id"]
    assert input_path(fid).read_bytes() == src.read_bytes() and r.json()["size_kb"] == 3072
    assert not list(input_path(fid).parent.glob(".*.part"))
    bad = client.post("/api/upload", files={"file": ("ures.mp4", b"", "video/mp4")})
    assert bad.status_code >= 400 and "empty" in bad.text
    assert not list(input_path(fid).parent.glob(".*.part"))


def test_chunked_upload_resumes_and_rejects_a_short_chunk(client, tmp_path):
    from app.storage import input_path
    src = _video(tmp_path / "IMG_0002.MOV", 2 * CH + 1234)
    data = src.read_bytes()
    st = client.post("/api/upload/chunks", json={"filename": "IMG_0002.MOV", "size": len(data)}).json()
    assert st["chunks"] == 3 and st["chunk_size"] == CH
    uid = st["upload_id"]
    part = lambda i: data[i * CH:(i + 1) * CH]                                   # noqa: E731
    assert client.put(f"/api/upload/chunks/{uid}/2", content=part(2)).status_code == 200      # sorrend mindegy
    short = client.put(f"/api/upload/chunks/{uid}/0", content=part(0)[:1000])                  # megszakadt darab
    assert short.status_code == 400 and "send it again" in short.text
    assert client.get(f"/api/upload/chunks/{uid}").json()["received"] == [2]     # a csonka nem számít
    early = client.post(f"/api/upload/chunks/{uid}/complete")
    assert early.status_code == 400 and "2 chunk(s) missing" in early.text
    for i in (0, 1):                                                               # a folytatás: csak a hiányzók
        assert client.put(f"/api/upload/chunks/{uid}/{i}", content=part(i)).status_code == 200
    s = client.get(f"/api/upload/chunks/{uid}").json()
    assert s["received"] == [0, 1, 2] and s["size"] == len(data) and s["filename"] == "IMG_0002.MOV"
    done = client.post(f"/api/upload/chunks/{uid}/complete")
    assert done.status_code == 200, done.text
    assert input_path(done.json()["file_id"]).read_bytes() == data
    assert client.get(f"/api/upload/chunks/{uid}").status_code == 404           # a darabok eltűntek


async def test_agent_url_import_goes_to_disk_and_recognises_a_phone_video(tmp_path, monkeypatch):
    from app import net
    from app.mcp_server import upload_from_url
    from app.storage import input_path
    src = _video(tmp_path / "v.bin", 5 * 1024 * 1024)

    async def download(url, dst, max_bytes, headers=None, timeout=120, retries=3):
        Path(dst).write_bytes(src.read_bytes())
        return "download", src.stat().st_size, "application/octet-stream"

    async def no_fetch(*a, **k):
        raise AssertionError("the whole file must not be read into memory")
    monkeypatch.setattr(net, "download", download)
    monkeypatch.setattr(net, "fetch", no_fetch)
    r = await upload_from_url(url="https://example.com/share/abc")
    assert r["file_id"].endswith(".mov") and r["original_name"] == "downloaded.mov" and r["size_kb"] == 5120
    assert input_path(r["file_id"]).read_bytes() == src.read_bytes()


async def test_base64_chunks_are_for_small_files(monkeypatch):
    import base64
    from app import mcp_server
    monkeypatch.setattr(mcp_server, "_CHUNKED_MAX", 1000)
    piece = base64.b64encode(b"x" * 600).decode()
    await mcp_server.upload_chunk("u1", 0, 3, "video.mov", piece)
    with pytest.raises(ValueError, match="upload it in the app"):
        await mcp_server.upload_chunk("u1", 1, 3, "video.mov", piece)
    assert "u1" not in mcp_server._CHUNK_BUFFERS


def test_sniff_from_a_path(tmp_path):
    from docx import Document
    from app.mcp_server import _sniff_ext
    Document().save(tmp_path / "d")
    assert _sniff_ext(tmp_path / "d") == "docx"
    assert _sniff_ext(_video(tmp_path / "m", 4096)) == "mov"
    (tmp_path / "p").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    assert _sniff_ext(tmp_path / "p") == "mp4"
