"""Az MCP-QA (mind a 43 tool, 2026-10) hibáinak javításai, pontonként — a tests/fixtures/qa_dokumentum.md-re építve.

A fixtúra: H1/H2 címek, ékezetes szöveg (ő, ű), egy 3 oszlopos táblázat (Év | Bevétel | Kiadás, 2 kitöltött sor) és
egy űrlaprész (Név, Születési dátum, Telefonszám). Az elfogadási feltételek erre épülnek.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures" / "qa_dokumentum.md"
CELLS = ["Év", "Bevétel", "Kiadás", "2023", "120", "95", "2024", "140", "110"]
HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
ADMIN_KEY = "qa-admin-key"


def _call(client, name, args):
    client.post("/mcp/", headers=HDR, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    r = client.post("/mcp/", headers=HDR, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                "params": {"name": name, "arguments": args}})
    res = r.json()["result"]
    text = res["content"][0]["text"]
    return res.get("isError"), (json.loads(text) if text.startswith("{") else text)


@pytest.fixture()
def api(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "MCP_AUTH_TOKEN", ADMIN_KEY)
    client.headers["Authorization"] = f"Bearer {ADMIN_KEY}"
    yield client
    client.headers.pop("Authorization", None)


def _task(job: dict, name: str) -> dict:
    return next(t["result"] for t in job["tasks"] if t["name"] == name)


def _need(*exes: str) -> None:
    from app.tools import wordcraft
    for e in exes:
        if (e == "wordcraft" and not wordcraft.available()) or (e != "wordcraft" and not shutil.which(e)):
            pytest.skip(f"{e} isn't installed")


def _path(fid: str) -> Path:
    from app.storage import input_path, output_path
    return output_path(fid) if output_path(fid).exists() else input_path(fid)


def lo_pdf(src: Path, out: Path) -> Path:
    """LibreOffice-render (saját profillal, hogy a párhuzamos futások ne akadjanak össze)."""
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(["soffice", f"-env:UserInstallation=file://{out}/lo", "--headless", "--convert-to", "pdf",
                    "--outdir", str(out), str(src)], check=True, capture_output=True, timeout=180)
    return out / f"{src.stem}.pdf"


def _margins(docx: Path) -> tuple[float, float]:
    """A szövegtükör bal és jobb széle (pt) a docx sectPr-éből; ha nincs, a Word alapja (Letter, 1")."""
    import re
    x = zipfile.ZipFile(docx).read("word/document.xml").decode("utf-8")
    sz = re.search(r'<w:pgSz [^>]*w:w="(\d+)', x)
    left = re.search(r'<w:pgMar [^>]*w:left="(\d+)', x)
    right = re.search(r'<w:pgMar [^>]*w:right="(\d+)', x)
    w = int(sz.group(1)) if sz else 12240
    return (int(left.group(1)) if left else 1440) / 20, (w - (int(right.group(1)) if right else 1440)) / 20


def assert_table_intact(docx: Path, tmp: Path) -> None:
    """A docx LibreOffice-os PDF-jének szövegrétegében mind a 9 cella megvan, és minden szódoboz a margón belül."""
    import fitz
    pdf = lo_pdf(docx, tmp)
    left, right = _margins(docx)
    words = [w for page in fitz.open(pdf) for w in page.get_text("words")]
    texts = [w[4] for w in words]
    missing = [c for c in CELLS if c not in texts]
    assert not missing, f"cells missing from the PDF text layer: {missing}"
    out = [w[4] for w in words if w[0] < left - 1 or w[2] > right + 1]
    assert not out, f"words outside the margins: {out}"
    assert "árvíztűrő" in texts and "tükörfúrógép" in texts


# ---------- 1. md → docx: a táblázat egészben marad ----------

def test_md_docx_default_is_wordcraft_and_table_intact(client, put_file, tmp_path):
    _need("wordcraft", "soffice")
    err, res = _call(client, "convert", {"file_id": put_file(FIX), "target_format": "docx"})
    assert not err, res
    assert res["backend"] == "wordcraft"
    assert_table_intact(_path(res["file_id"]), tmp_path)


def test_md_docx_pandoc_fallback_has_table_grid(client, put_file, tmp_path, monkeypatch):
    """A pandoc-út (a WordCraft nem való / nincs) kimenete is ép: explicit rács, 100 %-os szélesség."""
    _need("pandoc", "soffice")
    from app.tools import wordcraft
    monkeypatch.setattr(wordcraft, "md_ok", lambda p: False)
    err, res = _call(client, "convert", {"file_id": put_file(FIX), "target_format": "docx"})
    assert not err, res
    assert res["backend"] == "pandoc"
    docx = _path(res["file_id"])
    xml = zipfile.ZipFile(docx).read("word/document.xml").decode("utf-8")
    assert xml.count("<w:gridCol ") == 3 and 'w:type="pct" w:w="5000"' in xml
    assert_table_intact(docx, tmp_path)


def test_md_with_footnote_and_picture_stays_on_pandoc(client, put_file, tmp_path):
    """Lábjegyzet és kép: a WordCraft betűre hagyná / elejtené — ilyenkor a pandoc fut (a lábjegyzet megmarad)."""
    _need("pandoc", "wordcraft")
    from PIL import Image
    Image.new("RGB", (40, 20), (200, 30, 30)).save(tmp_path / "kep.png")
    md = tmp_path / "labjegyzet.md"
    md.write_text("# Cím\n\nSzöveg[^1] ő ű.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n[^1]: A lábjegyzet szövege.\n", "utf-8")
    err, res = _call(client, "convert", {"file_id": put_file(md), "target_format": "docx"})
    assert not err, res
    assert res["backend"] == "pandoc"
    z = zipfile.ZipFile(_path(res["file_id"]))
    assert "A lábjegyzet szövege." in z.read("word/footnotes.xml").decode("utf-8")
    assert z.read("word/document.xml").decode("utf-8").count("<w:gridCol ") == 2


def test_md_docx_rest_api_table_intact(api, tmp_path):
    """REST (/v1/convert — az univerzális gráf): ugyanaz a WordCraft-alapértelmezés."""
    _need("wordcraft", "soffice")
    r = api.post("/v1/convert", json={"file": base64.b64encode(FIX.read_bytes()).decode(), "filename": "qa.md",
                                      "output_format": "docx", "wait": 60}).json()
    assert r["status"] == "finished", r
    res = _task(r, "convert")
    assert res["engines"] in (["legacy"], ["wordcraft"])         # opció nélkül a bevált út (az is WordCraftot hív)
    assert_table_intact(_path(res["files"][0]["file_id"]), tmp_path)


def test_universal_pandoc_step_writes_table_grid(api, tmp_path):
    """Ha a hívó kifejezetten a pandocot kéri (engine=pandoc), annak a docx-e is ép táblát ad."""
    _need("pandoc", "soffice")
    r = api.post("/v1/convert", json={"file": base64.b64encode(FIX.read_bytes()).decode(), "filename": "qa.md",
                                      "output_format": "docx", "options": {"engine": "pandoc"}, "wait": 60}).json()
    assert r["status"] == "finished", r
    res = _task(r, "convert")
    assert res["engines"] == ["pandoc"]
    assert_table_intact(_path(res["files"][0]["file_id"]), tmp_path)


def test_ooxml_table_fix_leaves_healthy_tables_alone(tmp_path):
    """A rendes ráccsal bíró táblához (python-docx, Word) nem nyúlunk."""
    from docx import Document
    from app.tools import ooxml_safe
    d = Document()
    t = d.add_table(rows=2, cols=3)
    t.cell(0, 0).text = "Év"
    p = tmp_path / "ep.docx"
    d.save(str(p))
    before = zipfile.ZipFile(p).read("word/document.xml")
    ooxml_safe.fix(p)
    after = zipfile.ZipFile(p).read("word/document.xml")
    assert before.count(b"<w:gridCol ") == after.count(b"<w:gridCol ") == 3
    assert b'w:w="5000"' not in after


# ---------- 2. validate_document: a szétesett táblát meg kell fognia ----------

def _broken_pandoc_docx(tmp: Path) -> Path:
    """A javítás előtti pandoc-kimenet (üres táblarács) — közvetlenül a pandocból, az ooxml_safe-javítás nélkül."""
    out = tmp / "pandoc_hibas.docx"
    subprocess.run(["pandoc", str(FIX), "-o", str(out)], check=True, timeout=60)
    assert b"<w:tblGrid />" in zipfile.ZipFile(out).read("word/document.xml")
    return out


def test_validate_flags_cut_off_table_with_missing_cells(client, put_file, tmp_path):
    _need("pandoc", "soffice", "wordcraft")
    err, info = _call(client, "validate_document", {"file_id": put_file(_broken_pandoc_docx(tmp_path))})
    assert not err, info
    errors = [i for i in info["issues"] if i["severity"] == "error"]
    assert errors, info["summary"]
    miss = next(i for i in errors if i["kind"] == "content_missing")
    assert "Év | Bevétel | Kiadás" in miss["message"]
    assert {c["text"] for c in miss["missing_cells"]} == {"Bevétel", "Kiadás", "120", "95", "140", "110"}
    assert miss["object"].startswith("table") and miss["box"]["page"] == 1


def test_validate_good_wordcraft_docx_has_no_errors(client, put_file, tmp_path):
    _need("wordcraft", "soffice")
    err, res = _call(client, "convert", {"file_id": put_file(FIX), "target_format": "docx", "engine": "wordcraft"})
    assert not err, res
    err, info = _call(client, "validate_document", {"file_id": res["file_id"]})
    assert not err, info
    assert [i for i in info["issues"] if i["severity"] == "error"] == [], info["issues"]


def test_validate_right_margin_overflow_is_error_with_table_box():
    """Geometria a LibreOffice-renderen: a jobb margón túli cella hiba, a tábla határoló dobozával."""
    from app.tools import validate as v
    b = v.Block(pos=0, index=0, kind="table", rows=[["Év", "Bevétel"], ["2023", "120"]])
    L = v.Layout("word", Path("x.pdf"))
    L.sizes = [(612.0, 792.0)]
    L.toks = [v.Tok("ev", 1, (72, 100, 90, 112)), v.Tok("bevetel", 1, (400, 100, 600, 112)),
              v.Tok("2023", 1, (72, 120, 100, 132)), v.Tok("120", 1, (400, 120, 590, 132))]
    L.words, L.images, L.inked = [[]], [[]], [True]
    dc = v.DocCheck([b], [{"mL": 72, "mR": 72, "mT": 72, "mB": 72, "w": 612, "h": 792}], [L])
    issues = dc.run()
    e = next(i for i in issues if i["kind"] == "into_margin")
    assert e["severity"] == "error" and e["box"] == {"page": 1, "pt": [72, 100, 600, 132]}


def _lo_docx_to_pdf_check(client, fid):
    err, res = _call(client, "convert", {"file_id": fid, "target_format": "pdf"})
    assert not err, res
    return res["check"]


def test_convert_check_flags_missing_cells_docx_to_pdf(client, put_file, tmp_path):
    """A convert saját checkje (docx → pdf): a forrás szövegének lefedettsége a PDF szövegrétegében."""
    _need("pandoc", "soffice")
    check = _lo_docx_to_pdf_check(client, put_file(_broken_pandoc_docx(tmp_path)))
    assert check["status"] == "failed", check
    assert any("Év | Bevétel | Kiadás" in f and "“Kiadás”" in f for f in check["findings"]), check


def test_convert_check_ok_when_complete(client, put_file, tmp_path):
    _need("wordcraft", "soffice")
    err, res = _call(client, "convert", {"file_id": put_file(FIX), "target_format": "docx"})
    check = _lo_docx_to_pdf_check(client, res["file_id"])
    assert check["status"] == "ok", check
    assert any("table cell" in v for v in check["verified"])


def test_rest_convert_carries_the_coverage_check(api, tmp_path):
    """REST: a LibreOffice-út levágja az oszlopokat → a fájl mellett failed check; az alapút (WordCraft) teljes → ok."""
    _need("pandoc", "soffice", "wordcraft")
    data = base64.b64encode(_broken_pandoc_docx(tmp_path).read_bytes()).decode()
    r = api.post("/v1/convert", json={"file": data, "filename": "hibas.docx", "output_format": "pdf",
                                      "options": {"engine": "libreoffice"}, "wait": 90}).json()
    assert r["status"] == "finished", r
    t = _task(r, "convert")
    f = t["files"][0]
    assert t["engines"] == ["libreoffice"], t
    assert f["check"]["status"] == "failed" and any("Kiadás" in x for x in f["check"]["findings"]), f
    r = api.post("/v1/convert", json={"file": data, "filename": "hibas.docx", "output_format": "pdf", "wait": 90}).json()
    assert _task(r, "convert")["files"][0]["check"]["status"] == "ok"

