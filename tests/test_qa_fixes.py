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


# ---------- 3. forms recognize: a kitöltött adattáblából nem lesz mező ----------

WANT_FIELDS = {"nev": "text", "szuletesi_datum": "date", "telefonszam": "phone"}


def _fixture_as(client, put_file, fmt: str, engine: str | None = None) -> str:
    """A fixtúra docx-ként (WordCraft) vagy PDF-ként (a docx LibreOffice-szal, vagy engine='wordcraft')."""
    err, d = _call(client, "convert", {"file_id": put_file(FIX), "target_format": "docx"})
    assert not err, d
    if fmt == "docx":
        return d["file_id"]
    err, p = _call(client, "convert", {"file_id": d["file_id"], "target_format": "pdf", **({"engine": engine} if engine else {})})
    assert not err, p
    return p["file_id"]


def _recognized(client, fid: str) -> dict:
    err, r = _call(client, "forms", {"action": "recognize", "file_id": fid})
    assert not err, r
    return r["data"]


@pytest.mark.parametrize("fmt,engine", [("docx", None), ("pdf", None), ("pdf", "wordcraft")])
def test_forms_recognize_fixture_exactly_three_fields(client, put_file, fmt, engine):
    _need("wordcraft", "soffice")
    data = _recognized(client, _fixture_as(client, put_file, fmt, engine))
    assert {f["name"]: f["type"] for f in data["fields"]} == WANT_FIELDS, data["fields"]
    if fmt == "pdf":
        assert data["quality"]["level"] == "good"


def test_make_fillable_and_fill_assistant_use_the_same_three_fields(client, put_file):
    _need("wordcraft", "soffice")
    import fitz
    fid = _fixture_as(client, put_file, "pdf")
    err, r = _call(client, "forms", {"action": "make_fillable", "file_id": fid})
    assert not err, r
    out = r.get("file_id") or r["files"][0]["file_id"]
    with fitz.open(_path(out)) as d:
        names = {w.field_name for pg in d for w in pg.widgets()}
    assert names == set(WANT_FIELDS), names
    err, a = _call(client, "fill_assistant", {"file_id": fid})
    assert not err, a
    assert {q["name"] for q in a["questions"]} == set(WANT_FIELDS), a["questions"]


def test_type_keywords_match_whole_words():
    from app.formkit.schema import guess_type as g
    assert [g(x) for x in ("Bevétel", "Kivétel", "Feltétel", "Tétel", "Vétel")] == ["text"] * 5
    assert [g(x) for x in ("Telefonszám", "Tel.", "Mobil", "Mobilszám", "Phone")] == ["phone"] * 5
    assert g("Privát cím") == "text" and g("Adószám") == "hu_vat" and g("Martin") == "text"
    assert g("Update") == "text" and g("Mandátum") == "text" and g("Kezdődátum") == "date" and g("Zipper") == "text"


def test_table_fields_rule():
    from app.formkit.detect import table_fields
    data = [["Év", "Bevétel", "Kiadás"], ["2023", "120", "95"], ["2024", "140", ""]]
    assert table_fields(data) == []                               # adattábla: az üres cella sem mező
    form = [["Név", ""], ["Telefonszám", ""]]
    assert table_fields(form) == [(0, 1, "Név"), (1, 1, "Telefonszám")]
    assert table_fields([["2023", ""], ["120", ""]]) == []          # szám nem felirat


def test_form_like_table_empty_cells_stay_fields(tmp_path):
    """Kiegészítés a 3. ponthoz: az űrlapjellegű tábla (jelenléti ív napi sorai) üres cellái továbbra is mezők."""
    from docx import Document
    from app.formkit import detect
    d = Document()
    d.add_paragraph("Jelenléti ív — 2026. október")
    t = d.add_table(rows=4, cols=3)
    for c, h in enumerate(("Nap", "Érkezés", "Távozás")):
        t.cell(0, c).text = h
    for r in range(1, 4):
        t.cell(r, 0).text = f"{r}."
    p = tmp_path / "jelenleti.docx"
    d.save(str(p))
    fields = detect.recognize_docx(p)["fields"]
    assert len(fields) == 6
    assert fields[0]["label"] == "Érkezés – 1." and fields[0]["anchor"] == {"kind": "docx_cell", "t": 0, "r": 1, "c": 1}
    assert {f["anchor"]["c"] for f in fields} == {1, 2}


def test_form_like_grid_in_pdf_and_numeric_label_lowers_quality(tmp_path):
    """Lapos PDF: a rácsból felismert űrlaptábla üres cellái mezők; a kitöltött cellák nem. Szám mellett üres vonal:
    a mezőnek nincs felirata, és a minőség nem „good”."""
    import fitz
    from app.formkit import deep, detect
    pdf = tmp_path / "racs.pdf"
    doc = fitz.open()
    pg = doc.new_page()
    xs, ys = [72, 172, 272, 372], [100, 120, 140, 160]
    for y in ys:
        pg.draw_line((xs[0], y), (xs[-1], y))
    for x in xs:
        pg.draw_line((x, ys[0]), (x, ys[-1]))
    for c, h in enumerate(("Nap", "Erkezes", "Tavozas")):
        pg.insert_text((xs[c] + 4, ys[0] + 14), h)
    pg.insert_text((xs[0] + 4, ys[1] + 14), "1.")
    pg.insert_text((xs[1] + 4, ys[1] + 14), "8:00")             # kitöltött cella: nem mező
    pg.insert_text((xs[0] + 4, ys[2] + 14), "2.")
    pg.insert_text((72, 260), "120")
    pg.draw_line((100, 262), (250, 262))
    doc.save(str(pdf))
    sch = detect.recognize_pdf(pdf)
    labels = [f["label"] for f in sch["fields"]]
    assert "Tavozas – 1." in labels and "Erkezes – 2." in labels and "Erkezes – 1." not in labels
    assert not any(detect.numeric(l) for l in labels)
    assert sch["numeric_labels"] == 1 and deep.quality(sch)["level"] == "low"


# ---------- 4. extract: a DOCX-táblák is ----------

def test_extract_docx_text_has_cells_in_order_and_tables_mode(client, put_file):
    _need("wordcraft")
    fid = _fixture_as(client, put_file, "docx")
    err, r = _call(client, "extract", {"file_id": fid, "mode": "text"})
    assert not err, r
    lines = r["text"].split("\n")
    i = lines.index("Év\tBevétel\tKiadás")
    assert lines[i + 1:i + 3] == ["2023\t120\t95", "2024\t140\t110"]
    assert lines.index("A kísérleti program bevételei és kiadásai ezer forintban:") < i < lines.index("Jelentkezési adatlap")
    assert "árvíztűrő tükörfúrógép" in r["text"]
    err, r = _call(client, "extract", {"file_id": fid, "mode": "tables"})
    assert not err, r
    assert r["tables"] == [{"page": 1, "rows": [["Év", "Bevétel", "Kiadás"], ["2023", "120", "95"], ["2024", "140", "110"]]}]


def test_extract_docx_merged_cells_like_pdfplumber(tmp_path, put_file, client):
    from docx import Document
    d = Document()
    t = d.add_table(rows=2, cols=3)
    t.cell(0, 0).merge(t.cell(0, 1)).text = "Összevont fejléc"
    t.cell(0, 2).text = "Ű"
    t.cell(1, 0).text, t.cell(1, 1).text, t.cell(1, 2).text = "a", "b", "c"
    p = tmp_path / "osszevont.docx"
    d.save(str(p))
    err, r = _call(client, "extract", {"file_id": put_file(p), "mode": "tables"})
    assert not err, r
    assert r["tables"][0]["rows"] == [["Összevont fejléc", None, "Ű"], ["a", "b", "c"]]


# ---------- 5. facts: a múltbeli állítást a SAJÁT időszakával vetjük össze ----------

async def test_facts_past_period_claims_use_their_own_period(tmp_path, monkeypatch):
    from docx import Document
    from app.tools import content_check as cc, datasources as ds
    d = Document()
    for t in ("2024-ben a magyar infláció 3,7% volt, ami jóval kisebb az előző évinél.",
              "2025 végén az infláció 8,5% volt a KSH szerint.",
              "2023-ban a munkanélküliségi ráta 4,1% volt Magyarországon.",
              "2022-ben az alapkamat 13% volt, és ez a régió legmagasabbja volt."):
        d.add_paragraph(t)
    p = tmp_path / "allitasok.docx"
    d.save(str(p))
    claims = {"claims": [
        {"paragraph": 1, "quote": "2024-ben a magyar infláció 3,7% volt", "claim": "HU infláció 2024: 3,7%",
         "stat": {"country": "HU", "indicator": "cpi", "period": "2024", "value": 3.7}},
        {"paragraph": 2, "quote": "2025 végén az infláció 8,5% volt", "claim": "HU infláció 2025 dec.: 8,5%",
         "stat": {"country": "HU", "indicator": "cpi", "period": "2025-12", "value": 8.5}},
        {"paragraph": 3, "quote": "2023-ban a munkanélküliségi ráta 4,1% volt", "claim": "HU munkanélküliség 2023: 4,1%",
         "stat": {"country": "HU", "indicator": "unemployment", "period": "2023", "value": 4.1}},
        {"paragraph": 4, "quote": "2022-ben az alapkamat 13% volt", "claim": "HU alapkamat 2022: 13%",
         "stat": {"country": "HU", "indicator": "policy_rate", "period": "2022", "value": 13}}]}
    asks = []

    async def fake_ask(prompt, thinking=False):
        asks.append(prompt[:40])
        assert prompt.startswith("List the checkable"), "a statisztikai ítélethez nem kell a modell"
        return claims
    calls = []

    async def fake_statdata(tool, args):
        calls.append(dict(args))
        key = (args["indicator"], args.get("period"))
        return {("cpi", "2024"): {"value": 3.74, "period": "2024", "period_returned": "2024", "aggregation": "avg",
                                  "measure": "national CPI", "status": "ok", "source_used": "KSH"},
                ("cpi", "2025-12"): {"value": 4.4, "period": "2025-12", "period_returned": "2025-12", "status": "ok",
                                     "source_used": "Eurostat"},
                # régi szerver: a kért év helyett a legfrissebbet adja → nem vetjük össze
                ("unemployment", "2023"): {"value": 4.5, "period": "2026-09", "status": "fresh", "source_used": "KSH"},
                ("policy_rate", "2022"): {"status": "missing", "value": None}}[key]
    monkeypatch.setattr(cc, "_ask", fake_ask)
    monkeypatch.setattr(ds, "_statdata", fake_statdata)
    r = await cc.facts(p)
    by = {c["claim"]: c for c in r["claims"]}
    assert by["HU infláció 2024: 3,7%"]["verdict"] == "supported"
    c = by["HU infláció 2025 dec.: 8,5%"]
    assert c["verdict"] == "contradicted" and c["correct"] == "4,4" and c["replacement"] == "2025 végén az infláció 4,4% volt"
    for name, per in (("HU munkanélküliség 2023: 4,1%", "2023"), ("HU alapkamat 2022: 13%", "2022")):
        c = by[name]
        assert c["verdict"] == "unverified" and c["no_data_for_period"] == per
        assert "Nincs adat erre az időszakra" in c["explanation"]
    assert all(a.get("period") for a in calls), calls          # periódus nélküli („legfrissebb”) lekérés nem volt
    assert {a["period"] for a in calls} == {"2024", "2025-12", "2023", "2022"}
    assert len(asks) == 1


def test_period_matching_rules():
    from app.tools.datasources import norm_period, period_matches
    assert norm_period("2025q4") == "2025-Q4" and norm_period("2024") == "2024" and norm_period("tavaly") is None
    assert period_matches("2024", {"period": "2024"})
    assert not period_matches("2024", {"period": "2024-12"})                       # decemberi érték ≠ éves
    assert period_matches("2024", {"period_returned": "2024-12-31", "aggregation": "end"})
    assert period_matches("2025-12", {"period_returned": "2025-12-31"})            # lépcsős kamat: a hónap utolsó napja
    assert not period_matches("2025-12", {"period": "2026-09"})
    assert period_matches("2025-12-31", {"period_returned": "2025-09-24", "period_requested": "2025-12-31"})


# ---------- 6. a tool-leírás azt ígéri, amit a szerver MCP-n tud ----------

def _tools(client) -> dict:
    client.post("/mcp/", headers=HDR, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    r = client.post("/mcp/", headers=HDR, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    return {t["name"]: t for t in r.json()["result"]["tools"]}


def test_mcp_descriptions_dont_promise_ai_pictures(client):
    import re
    tools = _tools(client)
    g = tools["generate_image"]
    assert not re.search(r"FLUX|costs a credit", g["description"]) and "stock_search" in g["description"]
    assert {"size", "model"}.isdisjoint(g["inputSchema"]["properties"])
    assert "ai_prompt (generated" not in tools["insert_image"]["description"]
    assert "generate" not in tools["pptx_build"]["description"] and "stock|doc|none" in tools["pptx_build"]["description"]
    for name, t in tools.items():                          # sehol ne hirdessük a szerver képgenerálását
        assert not re.search(r"\{\"generate\"|FLUX", t["description"]), name


def test_ai_picture_requests_get_one_clear_error_before_any_work(client, put_file, samples):
    from app import tenancy
    msg = tenancy.CLIENT_IMAGE_MSG
    calls = [("generate_image", {"prompt": "egy őszi erdő"}),
             ("insert_image", {"file_id": put_file(samples.pptx()), "slot": "s1", "ai_prompt": "őszi erdő"}),
             ("pptx_build", {"spec": {"title": "Ű", "slides": [{"layout": "cover", "title": "Ő",
                                                                 "image": {"generate": "őszi erdő"}}]}}),
             ("pptx_build", {"from_document": put_file(samples.md()), "images": "ai"}),
             ("design", {"template": "poster", "data": {"title": "Ő", "image": {"generate": "őszi erdő"}}})]
    for name, args in calls:
        err, res = _call(client, name, args)
        assert err and msg in str(res), (name, res)
    err, res = _call(client, "generate_image", {"stock_search": ""})
    assert err and "stock_search" in str(res)


# ---------- 7. közös kimenő HTTP-kliens: leíró User-Agent, SSRF-védelem ----------

@pytest.fixture()
def local_http():
    """127.0.0.1-en futó apró szerver: /ua visszaadja a User-Agentet, /to-meta átirányít a metaadat-címre."""
    import http.server
    import threading
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["ua"] = self.headers.get("User-Agent")
            if self.path == "/to-meta":
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                self.end_headers()
                return
            body = "árvíztűrő".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/x", "http://localhost/x", "http://169.254.169.254/latest/meta-data/",
                                 "http://metadata.google.internal/computeMetadata/v1/", "http://[::1]/x",
                                 "http://100.100.100.200/latest/meta-data/", "file:///etc/passwd"])
def test_upload_from_url_refuses_internal_addresses(client, url):
    err, res = _call(client, "upload_from_url", {"url": url})
    assert err and ("internal addresses" in str(res) or "only http(s)" in str(res)), res


async def test_redirect_to_metadata_and_internal_peer_are_refused(local_http, monkeypatch):
    from app import net
    base, seen = local_http
    real = net.check_url

    async def as_if_public(url):                     # a helyi szervert „nyilvánosnak” vesszük, minden mást a valódi szabály
        if not url.startswith(base):
            await real(url)
    monkeypatch.setattr(net, "check_url", as_if_public)
    with pytest.raises(net.UnsafeURL):
        await net.fetch(base + "/to-meta")           # az átirányítás célját is ellenőrizzük
    with pytest.raises(net.UnsafeURL):
        await net.fetch(base + "/ua")                # a kapcsolat tényleges címe (127.0.0.1) belső


async def test_outbound_requests_carry_a_descriptive_user_agent(local_http, monkeypatch):
    from app import net
    base, seen = local_http
    monkeypatch.setenv("ALLOW_PRIVATE_URLS", "1")
    got = await net.fetch(base + "/ua")
    assert got.content.decode() == "árvíztűrő"
    assert seen["ua"].startswith("AIConversionEditor-Studio/1.0 (+") and "github.com" in seen["ua"]


def test_upload_from_url_wikimedia(client):
    """A stock_search Wikimedia-linkjei letölthetők (a leíró UA nélkül a Wikimedia 403-at ad)."""
    import socket
    try:
        socket.getaddrinfo("upload.wikimedia.org", 443)
    except OSError:
        pytest.skip("no network")
    err, res = _call(client, "upload_from_url", {
        "url": "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png"})
    assert not err, res                                   # a régi kliens „python-httpx” UA-jára 403 jött
    assert res["file_id"].endswith(".png") and res["size_kb"] > 100


# ---------- 8. betű-részhalmaz minden PDF-író úton ----------

def _fonts(pdf: Path) -> list[tuple[str, int]]:
    """A beágyazott betűk: (név, a betűfájl hossza bájtban)."""
    import fitz
    out = []
    with fitz.open(pdf) as d:
        for x in range(1, d.xref_length()):
            if d.xref_get_key(x, "FontFile2")[0] == "xref":
                ff = int(d.xref_get_key(x, "FontFile2")[1].split()[0])
                out.append((d.xref_get_key(x, "FontName")[1], len(d.xref_stream(ff))))
    return out


def _text(pdf: Path) -> str:
    import fitz
    with fitz.open(pdf) as d:
        return "".join(p.get_text() for p in d)


def _out_file(res: dict) -> Path:
    fid = res.get("file_id") or (res.get("files") or [{}])[0].get("file_id") or res["result"]["files"][0]["file_id"]
    return _path(fid)


def test_page_numbers_and_watermark_stay_small(client, put_file):
    _need("wordcraft", "soffice")
    fid = _fixture_as(client, put_file, "pdf")
    base = _path(fid).stat().st_size
    err, r = _call(client, "file_tools", {"tool": "page-numbers", "file_ids": [fid], "options": {"text_format": "{n}. oldal ő"}})
    assert not err, r
    p = _out_file(r)
    assert p.stat().st_size < base + 30_000, (base, p.stat().st_size, _fonts(p))
    assert "1. oldal ő" in _text(p)
    err, r = _call(client, "file_tools", {"tool": "watermark-pdf", "file_ids": [fid], "options": {"text": "Bizalmas – ű"}})
    assert not err, r
    p = _out_file(r)
    assert p.stat().st_size < base + 30_000, (base, p.stat().st_size, _fonts(p))
    assert "Bizalmas – ű" in _text(p)


def test_fillable_filled_and_flattened_under_100kb_with_accents(client, put_file):
    _need("wordcraft", "soffice")
    import fitz
    fid = _fixture_as(client, put_file, "pdf")
    err, r = _call(client, "forms", {"action": "make_fillable", "file_id": fid})
    assert not err, r
    fillable = r.get("file_id") or r["files"][0]["file_id"]
    assert _path(fillable).stat().st_size < 100_000, _fonts(_path(fillable))
    vals = {"nev": "Kőváry Ödön Űrsula", "szuletesi_datum": "1990-05-17", "telefonszam": "+36 30 123 4567"}
    for flatten in (False, True):
        err, r = _call(client, "forms", {"action": "fill", "file_id": fillable, "values": vals, "flatten": flatten})
        assert not err, r
        out = _out_file(r)
        assert out.stat().st_size < 100_000, (flatten, out.stat().st_size, _fonts(out))
        if flatten:
            assert "Kőváry Ödön Űrsula" in _text(out)
        else:
            with fitz.open(out) as d:
                assert {w.field_name: w.field_value for p in d for w in p.widgets()}["nev"] == "Kőváry Ödön Űrsula"
                d.bake()                                  # a megjelenés (amit a néző lát) is a részhalmaz-betűvel helyes
                assert "Kőváry Ö" in "".join(p.get_text() for p in d)        # a „____” mező keskeny: a vége levágva
    # a mező betűje a gépelési részhalmaz: a később begépelt ékezet is benne van
    from fontTools.ttLib import TTFont
    import io
    from app.tools import pdffonts
    cmap = TTFont(io.BytesIO(pdffonts.typing_font())).getBestCmap()
    assert all(ord(c) in cmap for c in "őűŐŰșțéáüöß€✓")


def test_rebuilt_fillable_form_stays_small(tmp_path):
    """A digitize fillable_pdf útja (forms.build_pdf): a lapszöveg betűje szűkül, a mezőké a gépelési részhalmaz."""
    from PIL import Image
    from app.tools import forms
    img = tmp_path / "lap.png"
    Image.new("RGB", (1240, 1754), "white").save(img)
    layout = {"items": [{"type": "text", "text": "Jelentkezési lap — árvíztűrő tükörfúrógép", "box": [80, 60, 900, 90], "size": 16},
                        {"type": "text", "text": "Név:", "box": [80, 150, 200, 175]},
                        {"type": "field", "name": "nev", "label": "Név", "box": [210, 150, 800, 175]}]}
    dst = tmp_path / "kitoltheto.pdf"
    names = forms.build_pdf([layout], [img], dst)
    assert names == ["nev"] and dst.stat().st_size < 100_000, _fonts(dst)
    assert "árvíztűrő tükörfúrógép" in _text(dst)
