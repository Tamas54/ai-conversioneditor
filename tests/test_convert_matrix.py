"""Konverziós mátrix valódi motorokkal: kategóriánként reprezentatív párok, a kimenet érvényes és az ékezetek
(ő, ű) épek. Ami motor a gépen hiányzik, az a párnál kihagyás (skip) — a Docker-képben minden motor megvan."""
from __future__ import annotations

import asyncio
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from samples import Samples


def _text_of(p: Path) -> str:
    ext = p.suffix.lower().lstrip(".")
    if ext == "pdf":
        import pymupdf
        with pymupdf.open(str(p)) as d:
            return "".join(pg.get_text() for pg in d)
    if ext == "docx":
        from docx import Document
        return "\n".join(par.text for par in Document(str(p)).paragraphs)
    if ext in ("odt", "epub", "fodt") and ext != "fodt":
        with zipfile.ZipFile(p) as z:
            return "".join(z.read(n).decode("utf-8", "replace") for n in z.namelist() if n.endswith((".xml", ".xhtml", ".html")))
    if ext == "rtf":
        import re
        raw = p.read_text("latin-1")
        return re.sub(r"\\u(-?\d+)\s?(\\'[0-9a-f]{2}|\?)?", lambda m: chr(int(m.group(1)) % 65536), raw)
    return p.read_text("utf-8", "replace")


def _ffprobe(p: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)],
                         capture_output=True, text=True, timeout=60).stdout
    return json.loads(out or "{}")


DOC = [("docx", "pdf"), ("docx", "odt"), ("docx", "rtf"), ("docx", "md"), ("docx", "html"), ("docx", "epub"), ("docx", "txt"),
       ("md", "docx"), ("md", "pdf"), ("md", "html"), ("html", "pdf"), ("html", "docx"), ("txt", "pdf"), ("pdf", "txt"),
       ("pdf", "docx"), ("pdf", "html"), ("docx", "doc"), ("odt", "docx"), ("md", "rst"), ("md", "tex")]
IMG = [("png", "jpg"), ("png", "webp"), ("png", "avif"), ("png", "heic"), ("png", "jxl"), ("png", "tiff"), ("png", "bmp"),
       ("png", "gif"), ("png", "ico"), ("png", "icns"), ("png", "tga"), ("png", "jp2"), ("png", "pcx"), ("png", "ppm"),
       ("png", "pdf"), ("png", "xpm"), ("png", "exr"), ("png", "psd"), ("jpg", "png"), ("png", "svg")]
AV = [("wav", "mp3"), ("wav", "flac"), ("wav", "ogg"), ("wav", "opus"), ("wav", "m4a"), ("wav", "aac"), ("wav", "aiff"),
      ("wav", "wma"), ("wav", "ac3"), ("wav", "mp4"), ("mp4", "webm"), ("mp4", "mkv"), ("mp4", "mov"), ("mp4", "avi"),
      ("mp4", "flv"), ("mp4", "mpeg"), ("mp4", "ts"), ("mp4", "3gp"), ("mp4", "ogv"), ("mp4", "gif"), ("mp4", "mp3"),
      ("mp4", "wav"), ("mp4", "jpg"), ("mp4", "webp")]
OTHER = [("zip", "7z"), ("zip", "tar"), ("zip", "tar.gz"), ("zip", "tar.bz2"), ("zip", "tar.xz"), ("zip", "tar.zst"),
         ("ttf", "woff"), ("ttf", "woff2"), ("ttf", "otf"), ("ttf", "eot"), ("dxf", "png"), ("dxf", "svg"), ("dxf", "pdf"),
         ("svg", "png"), ("svg", "pdf"), ("svg", "eps"), ("svg", "emf"), ("xlsx", "csv"), ("xlsx", "ods"), ("xlsx", "pdf"),
         ("xlsx", "json"), ("csv", "xlsx"), ("pptx", "pdf"), ("pptx", "odp"), ("pptx", "png"), ("docx", "mobi"), ("docx", "azw3"),
         ("docx", "fb2")]

_SRC = {"docx": "docx", "md": "md", "html": "html", "txt": "txt", "pdf": "pdf", "odt": None, "png": "png", "jpg": "jpg",
        "wav": "wav", "mp4": "mp4", "zip": "zip", "ttf": "ttf", "dxf": "dxf", "svg": "svg", "xlsx": "xlsx", "csv": "csv_rows",
        "pptx": "pptx"}


def _source(s: Samples, fmt: str, tmp: Path) -> Path:
    if fmt == "odt":
        from app.convert.service import convert_path
        outs, _ = asyncio.run(convert_path(s.docx(), "odt", {}, tmp / "mk"))
        return outs[0]
    return getattr(s, _SRC[fmt])()


@pytest.mark.parametrize("src,dst", DOC + IMG + AV + OTHER)
def test_conversion(src, dst, tmp_path):
    from app.convert import graph
    from app.convert.service import convert_path
    if not graph.plan(src, dst):
        pytest.skip(f"{src}→{dst}: engine missing here ({graph.engines_for(src, dst)})")
    s = Samples(tmp_path)
    source = _source(s, src, tmp_path)
    outs, used = asyncio.run(convert_path(source, dst, {}, tmp_path / "w"))
    assert outs and all(o.stat().st_size > 0 for o in outs)
    out = outs[0]
    from app.catalog.formats import FORMATS
    cat = FORMATS[dst].category
    if src in ("docx", "md", "html", "txt", "odt") and dst in ("pdf", "docx", "odt", "rtf", "md", "html", "epub", "txt", "rst", "tex", "fb2"):
        t = _text_of(out) if dst != "fb2" else out.read_text("utf-8")
        assert "ő" in t and "ű" in t, f"accents lost in {src}→{dst}: {t[:200]!r}"
    elif src == "pdf" and dst in ("txt", "docx", "html"):
        assert "Árvíztűrő" in _text_of(out)
    elif cat == "image" and dst not in ("svg", "psd", "exr", "xpm"):
        from PIL import Image
        from app.convert.steps import _register_plugins
        _register_plugins()
        with Image.open(out) as im:
            assert im.width > 0
    elif cat in ("audio", "video") and dst not in ("gif", "jpg", "webp"):
        info = _ffprobe(out)
        assert float(info["format"]["duration"]) > 0.5
    elif cat == "archive":
        from app.convert.steps import extract
        d = asyncio.run(extract(out, tmp_path / "x"))
        names = {p.name for p in d.rglob("*") if p.is_file()}
        assert {"első.txt", "második.txt"} <= names
    elif cat == "font":
        from fontTools.ttLib import TTFont
        if dst == "eot":
            from app.convert.steps import eot_unwrap
            import io
            TTFont(io.BytesIO(eot_unwrap(out.read_bytes())))
        else:
            assert len(TTFont(str(out)).getGlyphOrder()) > 100
    elif dst == "json" and src == "xlsx":
        assert json.loads(out.read_text("utf-8"))[0]["Név"] == "Őrs"


def test_options_applied_image(tmp_path):
    from PIL import Image
    from app.convert.service import convert_path
    s = Samples(tmp_path)
    outs, _ = asyncio.run(convert_path(s.jpg(), "png", {"width": 100, "strip_metadata": True, "rotate": 90}, tmp_path / "w"))
    with Image.open(outs[0]) as im:
        assert im.height == 100 or im.width == 100
    outs, _ = asyncio.run(convert_path(s.jpg(), "jpg" if False else "webp", {"quality": 10, "width": 64}, tmp_path / "w2"))
    with Image.open(outs[0]) as im:
        assert im.width == 64


def test_options_video_trim_resolution(tmp_path):
    from app.convert.service import convert_path
    s = Samples(tmp_path)
    outs, _ = asyncio.run(convert_path(s.mp4(seconds=3), "mp4", {"start": "1", "end": "2", "resolution": "160x120"}, tmp_path / "w")) \
        if False else asyncio.run(convert_path(s.mp4(seconds=3), "webm", {"start": "1", "end": "2", "resolution": "160x120", "remove_audio": True}, tmp_path / "w"))
    info = _ffprobe(outs[0])
    v = next(st for st in info["streams"] if st["codec_type"] == "video")
    assert (v["width"], v["height"]) == (160, 120)
    assert 0.5 < float(info["format"]["duration"]) < 1.6
    assert not any(st["codec_type"] == "audio" for st in info["streams"])


def test_page_range_and_multi_page_zip(tmp_path, put_file):
    from app.convert import service
    s = Samples(tmp_path)
    fid = put_file(s.pdf(pages=4))
    res = asyncio.run(service.convert_file(fid, "png", {"page_range": "2-3"}))
    assert res["file_id"].endswith(".zip") and res["count"] == 2
    res = asyncio.run(service.convert_file(fid, "png", {"page_range": "2-3", "output": "files"}))
    assert len(res["files"]) == 2 and all(f["file_id"].endswith(".png") for f in res["files"])
    res = asyncio.run(service.convert_file(fid, "pdfa", {"page_range": "1"}))
    import pymupdf
    from app.storage import output_path
    with pymupdf.open(str(output_path(res["file_id"]))) as d:
        assert d.page_count == 1


def test_ocr_language_on_scanned_pdf(tmp_path):
    """Képből álló (szkennelt) PDF → szöveg: OCRmyPDF + Tesseract magyar nyelvvel."""
    from app.convert import engines
    from app.convert.service import convert_path, ocr_languages
    if not engines.available("ocrmypdf") or "hun" not in ocr_languages():
        pytest.skip("ocrmypdf / Tesseract hun missing")
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 48)
    im = Image.new("RGB", (1240, 400), "white")
    ImageDraw.Draw(im).text((60, 150), "Tükörfúrógép és őszi űrlap", font=font, fill="black")
    scan = tmp_path / "scan.pdf"
    im.save(scan, "PDF", resolution=150)
    outs, used = asyncio.run(convert_path(scan, "txt", {"ocr_language": "hun"}, tmp_path / "w"))
    assert "ocrmypdf" in used
    t = outs[0].read_text("utf-8")
    assert "őszi" in t and "űrlap" in t


def test_legacy_paths_preferred_when_no_options(tmp_path, put_file, monkeypatch):
    """Opció nélkül a régi (anyconvert) út fut, ha a pár ott van — a bevált minőség nem változik."""
    from app.convert import service
    from app.tools import anyconvert
    called = {}

    async def fake(pool, fid, target):
        called["yes"] = (fid, target)
        return {"file_id": fid, "url": "u", "label": "x", "size_kb": 1}
    monkeypatch.setattr(anyconvert, "convert_any", fake)
    s = Samples(tmp_path)
    fid = put_file(s.png())
    asyncio.run(service.convert_file(fid, "jpg"))
    assert called["yes"] == (fid, "jpg")
    called.clear()
    asyncio.run(service.convert_file(fid, "jpg", {"quality": 50}))      # opcióval már az új út
    assert not called
