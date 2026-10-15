"""A bővítés ELŐTTI működés rögzítése: ezeknek a teszteknek a bővítés után is zöldnek kell maradniuk.

Szerkesztők, AI-szerkesztés, digitalizálás, űrlapok, PDF-műveletek, konverzió, MCP, billing, auth, SEO —
annyit, amennyi a Craft-CLI-k és az LLM nélkül is mérhető (a Craft-hívásokat ahol kell, kicseréljük).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

EXISTING_MCP_TOOLS = {
    "chart_redraw", "convert", "delete_pages", "delete_section", "describe_image", "design", "digitize",
    "edit_with_instruction", "extract", "extract_table_from_image", "facts", "files", "find_replace",
    "generate_chart_image", "generate_image", "history", "image_to_xlsx", "insert_after", "insert_image", "merge",
    "ocr_image", "office", "pdf_form", "photo_edit", "photocraft", "pptx_build", "replace_heading", "restore_photos",
    "upload_chunk", "upload_file", "upload_from_url", "validate_document", "vectorcraft", "video", "wordcraft",
}


# ---------- konverzió: célok és útvonalak ----------

def test_anyconvert_targets_pinned():
    from app.tools import anyconvert as a
    assert a.targets_for("docx") == ["pdf", "odt", "rtf", "md", "html", "txt", "epub"]
    assert a.targets_for(".PDF") == ["docx", "txt", "html", "rtf", "jpg", "png"]
    assert a.targets_for("pptx") == ["pdf", "odp", "png", "docx"]
    assert a.targets_for("xlsx") == ["csv", "pdf", "html", "json", "ods", "tsv"]
    assert a.targets_for("jpeg") == ["png", "webp", "pdf", "tif", "psd"]
    assert a.targets_for("svg") == ["pdf", "png", "jpg"]
    assert a.targets_for("xyz") == []


def test_legacy_route_pinned():
    from app.tools.formats import route
    assert route("html", "pdf") == "weasyprint"
    assert route("html", "pdf", use_browser=True) == "chromium"
    assert route("md", "pdf") == "md_to_pdf"
    assert route("pdf", "docx") == "pdf2docx"
    assert route("docx", "pdf") == "libreoffice"
    assert route("pptx", "docx") == "via_pdf"
    assert route("pdf", "html") == "via_docx"
    assert route("docx", "docx") == "noop"


def test_convert_targets_endpoint(client):
    r = client.get("/api/convert/targets", params={"ext": "png"})
    assert r.status_code == 200
    assert [t["fmt"] for t in r.json()["targets"]] == ["jpg", "webp", "pdf", "tif", "psd"]
    assert r.json()["targets"][0]["label"] == "JPG image"


def test_make_convert_png_to_jpg(client, samples, put_file):
    fid = put_file(samples.png())
    r = client.post("/api/make/convert", json={"file_id": fid, "target": "jpg"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["file_id"].endswith(".jpg") and out["label"].endswith(".jpg")
    r2 = client.post("/api/make/convert", json={"file_id": fid, "target": "mp3"})
    assert r2.status_code == 400 and "Can't convert .png to .mp3" in r2.json()["detail"]
    assert client.post("/api/make/convert", json={"file_id": fid}).status_code == 400


# ---------- űrlapok ----------

def test_forms_coerce_pinned():
    from app.tools.forms import _coerce
    known = [{"name": "Név", "type": "text"}, {"name": "hozzajarul", "type": "checkbox"}]
    vals, skipped = _coerce({"nev": "Kőváry Ödön", "Hozzájárul": "igen", "ismeretlen": "x", "Név ": ""}, known)
    assert vals == {"Név": "Kőváry Ödön", "hozzajarul": True}
    assert skipped == ["ismeretlen"]


def test_forms_read_rows_keeps_accents(samples):
    from app.tools.forms import read_rows
    rows = read_rows(samples.csv_rows())
    assert rows[0] == {"nev": "Kőváry Ödön", "lakcim": "Győr, Fő utca 1.", "hozzajarul": "igen"}
    assert rows[1]["nev"] == "Űrmös Éva"


def test_forms_bake_keeps_accents(samples, tmp_path):
    import pymupdf as fitz
    from app.tools.forms import _bake
    p = samples.fillable_pdf()
    d = fitz.open(str(p))
    for w in d[0].widgets():
        if w.field_name == "nev":
            w.field_value = "Kőváry Ödön Űrmös"
            w.update()
        if w.field_name == "hozzajarul":
            w.field_value = w.on_state()
            w.update()
    d.save(str(tmp_path / "filled.pdf"))
    d.close()
    out = tmp_path / "filled.pdf"
    _bake(out)
    d = fitz.open(str(out))
    assert not list(d[0].widgets())                      # a mezők beégetve
    assert "Kőváry Ödön Űrmös" in d[0].get_text()
    d.close()


def test_fill_form_route_validation(client):
    r = client.post("/api/make/fill_form", json={})
    assert r.status_code == 400 and "Choose the form" in r.json()["detail"]


def test_fill_form_route_uses_forms_fill(client, samples, put_file, monkeypatch):
    from app.tools import forms
    fid = put_file(samples.fillable_pdf())
    seen = {}

    async def fake_fill(file_id, values, flatten, dst):
        seen.update(file_id=file_id, values=values, flatten=flatten)
        Path(dst).write_bytes(Path(samples.fillable_pdf("x.pdf")).read_bytes())
        return {"filled": sorted(values), "skipped": []}
    monkeypatch.setattr(forms, "fill", fake_fill)
    r = client.post("/api/make/fill_form", json={"file_id": fid, "values": {"nev": "Ő"}, "flatten": True})
    assert r.status_code == 200, r.text
    assert seen == {"file_id": fid, "values": {"nev": "Ő"}, "flatten": True}
    assert r.json()["filled"] == ["nev"] and r.json()["label"].endswith(".pdf")


def test_form_fields_non_pdf_is_empty(client):
    assert client.get("/api/forms/fields", params={"file_id": "abc.docx"}).json() == {"fields": []}


# ---------- PDF-műveletek ----------

def test_pdf_plan_pinned():
    from fastapi import HTTPException
    from app.routes.make import _pdf_plan, PDF_LABELS
    assert _pdf_plan("merge", {})[1:] == ("pdf", False)
    assert _pdf_plan("to_word", {})[1] == "docx"
    assert _pdf_plan("make_fillable", {}) == ([{"tool": "form_detect_fields", "args": {"add": True}}], "pdf", True)
    assert _pdf_plan("protect", {"password": "titok"})[0][0]["args"] == {"open_password": "titok"}
    for bad in (("watermark", {}), ("protect", {"password": "x"}), ("redact", {}), ("nope", {})):
        with pytest.raises(HTTPException):
            _pdf_plan(*bad)
    assert set(PDF_LABELS) == {"merge", "compress", "to_word", "page_numbers", "watermark", "redact", "ocr", "protect",
                               "compare", "make_fillable"}


def test_make_pdf_rejects_non_pdf(client):
    r = client.post("/api/make/pdf", json={"action": "compress", "file_ids": ["a.docx"]})
    assert r.status_code == 400 and "Only PDF files" in r.json()["detail"]
    r = client.post("/api/make/pdf", json={"action": "merge", "file_ids": ["a.pdf"]})
    assert r.status_code == 400


# ---------- MCP ----------

def test_mcp_tools_still_registered():
    from app.mcp_server import mcp
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert EXISTING_MCP_TOOLS <= names


def test_mcp_initialize_and_list(client):
    hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    r = client.post("/mcp/", headers=hdr, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    assert r.status_code == 200, r.text
    r = client.post("/mcp/", headers=hdr, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert EXISTING_MCP_TOOLS <= names


def test_agent_handlers_kept():
    from app.llm import agent
    for name in ("convert_format", "pdf_tools", "scan_to_text", "photo_edit", "office", "find_replace", "do_task"):
        assert name in agent.TOOL_HANDLERS


# ---------- auth ----------

def test_mcp_requires_key_when_token_set(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "MCP_AUTH_TOKEN", "secret-token")
    r = client.post("/mcp/", json={})
    assert r.status_code == 401 and "WWW-Authenticate" in r.headers


def test_guest_rules(client, monkeypatch):
    from app.config import settings
    from app.auth import _guest_ok
    monkeypatch.setattr(settings, "DASHBOARD_PASSWORD", "pw")
    r = client.get("/api/convert/targets", params={"ext": "pdf"})       # vendég: engedett
    assert r.status_code == 200
    r = client.post("/api/make/revise", json={})                        # vendég: AI → fiók kell
    assert r.status_code == 401 and r.json().get("account_required") is True

    class R:
        def __init__(self, m, p):
            self.method, self.url = m, type("U", (), {"path": p})()
    assert _guest_ok(R("POST", "/api/make/fill_form")) and not _guest_ok(R("POST", "/api/make/digitize"))


# ---------- billing + DB ----------

def test_billing_plans_pinned():
    from app import billing
    assert set(billing.PLANS) == {"free", "plus", "pro"}
    assert billing.PLANS["free"]["quota"]["form_pages"] == 20 and billing.PLANS["free"]["monthly_credits"] == 10
    assert billing.batch_limit("pro") == 200 and billing.batch_limit("nope") == 10


def test_db_keys_credits_quota(db_client):
    from app import billing, db, tenancy
    call = db_client.portal.call
    assert db.enabled()
    u = call(db.user_create, "kovacs@example.com", "jelszó123", "Kovács Ődön", 30)
    uid = u["id"]
    k = call(db.api_key_create, uid, "teszt")
    assert k["key"].startswith("sk_") and call(db.api_key_user, k["key"]) == uid
    assert call(db.credits_spend, uid, 5, "teszt") == 25
    with pytest.raises(db.InsufficientCredits):
        call(db.credits_spend, uid, 100, "teszt")
    assert call(db.credits_add, uid, 10, "vissza") == 35

    async def over_quota():
        tok = tenancy.set_user(uid)
        try:
            for _ in range(20):
                await billing.use("form_pages")
            await billing.use("form_pages")
        finally:
            tenancy.reset(tok)
    with pytest.raises(Exception) as e:
        call(over_quota)
    assert getattr(e.value, "status_code", None) == 402


# ---------- SEO ----------

def test_seo_closed_by_default(client):
    r = client.get("/robots.txt")
    assert "Disallow: /" in r.text
    r = client.get("/app/make/document")
    assert r.status_code == 200 and r.headers.get("X-Robots-Tag", "").startswith("noindex")
    assert "Online Word editor" in r.text


def test_seo_pages_and_sitemap(client):
    from app import seo
    assert "convert" in seo.PAGES and "pdf-tools" in seo.PAGES
    xml = client.get("/sitemap.xml").text
    assert "/app/make/pdf-tools" in xml
    assert "/mcp" in client.get("/llms.txt").text


def test_root_redirects_signed_in_user_to_app(client):
    from app.auth import COOKIE, new_session
    client.cookies.set(COOKIE, new_session("abcdef123456"))
    r = client.get("/", follow_redirects=False)
    client.cookies.clear()
    assert r.status_code in (301, 302, 303, 307) and r.headers["location"].endswith("/app")
