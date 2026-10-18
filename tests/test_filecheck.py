"""Sérült feltöltés már a feltöltéskor, érthetően — és a felhasználó soha nem lát szerveroldali útvonalat.

Élesben (10-18) egy szövegként továbbított PNG (minden 0x7F feletti bájt helyén EF BF BD) a konvertálásnál
„cannot identify image file '/data/u/…/inputs/….png'” hibát adott: nyers PIL-szöveg, belső útvonallal.
"""
from __future__ import annotations

import io

from PIL import Image

from app.filecheck import problem, public_message, scrub


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 40, 40)).save(buf, "PNG")
    return buf.getvalue()


def _mangled(data: bytes) -> bytes:              # ahogy egy szövegként továbbított bináris kinéz
    return data.decode("utf-8", errors="replace").encode("utf-8")


def test_problem_catches_only_sure_cases():
    good = _png()
    assert problem(good, "a.png") is None
    assert "saved as text" in problem(_mangled(good), "a (1).png")
    assert "web page" in problem(b"<!DOCTYPE html><html><body>", "photo.jpg")
    assert "empty" in problem(b"", "a.docx")
    assert problem(b"<!DOCTYPE html>", "page.html") is None          # a weboldal weboldalként rendben
    assert problem("árvíztűrő".encode(), "notes.txt") is None         # szövegfájlban az ékezet nem hiba


def test_scrub_removes_server_paths_only():
    assert "/data/" not in scrub("cannot identify image file '/data/u/g1/inputs/x.png'")
    assert "/home/" not in scrub("missing /home/u/app/data/x.docx")
    for keep in ("https://example.com/a/b failed", "image/png", "a 1/2/3 ratio", "and/or"):
        assert scrub(keep) == keep


def test_public_message_for_unreadable_image():
    from PIL import UnidentifiedImageError
    msg = public_message(UnidentifiedImageError("cannot identify image file '/data/u/g/inputs/x.png'"))
    assert "can't be read" in msg and "/data" not in msg


def test_upload_rejects_mangled_file(client):
    r = client.post("/api/upload", files={"file": ("photo (1).png", _mangled(_png()), "image/png")})
    assert r.status_code == 400 and "saved as text" in r.json()["detail"]
    r = client.post("/api/upload", files={"file": ("photo.png", _png(), "image/png")})
    assert r.status_code == 200 and r.json()["file_id"]


def test_http_errors_never_show_server_paths(client):
    r = client.post("/api/make/convert", json={"file_id": "nope.png", "target": "webp"})
    assert r.status_code >= 400 and "/data/" not in r.text and "/home/" not in r.text
