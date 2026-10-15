"""A nyilvános, többnyelvű honlap: nyelvi paritás, oldaltípusok, hreflang/canonical/schema.org, linkstruktúra
(nincs árva oldal, legfeljebb 3 kattintás), robots/sitemap/llms, ékezetek."""
from __future__ import annotations

import json
import re
import string

import pytest
from bs4 import BeautifulSoup


def _keys(d, pre=""):
    out = {}
    for k, v in d.items():
        key = f"{pre}{k}"
        if isinstance(v, dict):
            out.update(_keys(v, key + "."))
        else:
            out[key] = v
    return out


def _fields(s):
    return {f for _, f, _, _ in string.Formatter().parse(s) if f} if isinstance(s, str) else set()


def test_i18n_parity_hu_has_every_en_key():
    from app.site import i18n
    en, hu = _keys(i18n.data("en")), _keys(i18n.data("hu"))
    missing = sorted(k for k in en if k not in hu and not k.endswith(".tag"))
    assert not missing, missing[:20]
    # a fordítás nem használhat olyan helyőrzőt, amit a kód nem ad át (az angolban sincs) — a {src_from} nyelvi rag
    bad = [k for k, v in en.items() if isinstance(v, str) and isinstance(hu.get(k), str)
           and not {f for f in _fields(hu[k]) if '"' not in f} <= _fields(v) | {"src_from"}]
    assert not bad, bad[:20]


def test_new_language_is_one_file(tmp_path, monkeypatch):
    """Egy új nyelvi fájl (itt egy minimális „de”) + a kódja a SITE_LANGS-ben = új nyelv; a hiányzó kulcsok angolul."""
    from app.site import i18n
    monkeypatch.setenv("SITE_LANGS", "en,hu,de")
    src = i18n.DIR
    new = tmp_path / "i18n"
    new.mkdir()
    for p in src.glob("*.json"):
        (new / p.name).write_text(p.read_text("utf-8"), "utf-8")
    (new / "de.json").write_text(json.dumps({"_meta": {"native": "Deutsch", "html": "de", "pair": "{src}-zu-{dst}"},
                                             "slug": {"convert": "konvertieren"}, "nav": {"convert": "Konvertieren"}}), "utf-8")
    monkeypatch.setattr(i18n, "DIR", new)
    i18n.reload()
    try:
        assert "de" in i18n.langs()
        assert i18n.t("de", "nav.convert") == "Konvertieren" and i18n.t("de", "nav.tools") == "Tools"
        from app.site import urls
        assert urls.pair("de", "docx", "pdf") == "/de/konvertieren/docx-zu-pdf"
        assert urls.parse_pair("de", "docx-zu-pdf") == ("docx", "pdf")
    finally:
        monkeypatch.setattr(i18n, "DIR", src)
        i18n.reload()


PAGES = {
    "en": ["/ai-agents", "/convert", "/convert/documents", "/convert/docx-to-pdf", "/convert/heic-to-jpg", "/convert/tar-gz-to-zip",
           "/formats/docx", "/tools", "/compare", "/compare/cloudconvert-alternative", "/developers", "/developers/docs",
           "/developers/job-builder", "/developers/convert/docx-to-pdf", "/connect-ai", "/unit-converter",
           "/unit-converter/length", "/time-zone-converter"],
    "hu": ["/hu/ai-ugynokok", "/hu/atalakitas", "/hu/atalakitas/dokumentumok", "/hu/atalakitas/docx-bol-pdf", "/hu/atalakitas/heic-bol-jpg",
           "/hu/formatumok/docx", "/hu/eszkozok", "/hu/osszehasonlitas", "/hu/osszehasonlitas/smallpdf-alternativa",
           "/hu/fejlesztoknek", "/hu/fejlesztoknek/dokumentacio", "/hu/ai-csatlakoztatas", "/hu/mertekegyseg-valto/homerseklet",
           "/hu/idozona-valto"],
}


@pytest.mark.parametrize("lang,path", [(L, p) for L, ps in PAGES.items() for p in ps])
def test_page_renders_with_seo_head(client, lang, path):
    r = client.get(path)
    assert r.status_code == 200, path
    s = BeautifulSoup(r.text, "lxml")
    assert s.html["lang"] == lang
    assert s.title.text and s.find("meta", {"name": "description"})["content"]
    canon = s.find("link", rel="canonical")["href"]
    assert canon.endswith(path)
    alts = {l["hreflang"]: l["href"] for l in s.find_all("link", rel="alternate") if l.get("hreflang")}
    assert {"en", "hu", "x-default"} <= set(alts)
    lds = [json.loads(x.string) for x in s.find_all("script", type="application/ld+json")]
    types = {d["@type"] for d in lds}
    assert "Organization" in types
    assert "BreadcrumbList" in types and s.select_one("nav.crumbs")
    # a meglévő honlap (/app) része: ugyanaz a fejléc, a morzsamenü gyökere az /app, és van „vissza” gomb
    assert s.select_one("header.topbar a.brand")["href"] == "/app"
    assert s.select_one("nav.crumbs a")["href"] == "/app" and s.select_one("a.back[data-back]")
    assert len(s.find_all("h1")) == 1
    # a nyelvváltó a másik nyelv ugyanazon oldalára mutat, ami tényleg létezik
    other = alts["hu" if lang == "en" else "en"].replace("http://testserver", "").replace("http://localhost:8000", "")
    assert client.get(other).status_code == 200


def test_pair_page_content_is_specific(client):
    a = BeautifulSoup(client.get("/convert/heic-to-jpg").text, "lxml")
    b = BeautifulSoup(client.get("/convert/mp4-to-mp3").text, "lxml")
    ta, tb = a.find("main").get_text(" "), b.find("main").get_text(" ")
    assert "iPhone" in ta and "quality defaults to 88" in ta
    assert "192 kbit/s" in tb and "FFmpeg" in tb
    types = {json.loads(x.string)["@type"] for x in a.find_all("script", type="application/ld+json")}
    assert {"WebApplication", "FAQPage", "BreadcrumbList", "HowTo"} <= types
    # nem ugyanaz a szöveg két párnál (a sablonrészeken túl)
    common = set(ta.split(". ")) & set(tb.split(". "))
    assert len(common) < 0.5 * len(set(ta.split(". ")))


def test_hungarian_accents_and_suffix(client):
    t = client.get("/hu/atalakitas/mp3-bol-wav").text
    assert "MP3-ból WAV" in t and "Átalakítás" in t and "törlődik" in t
    assert "HEIC-ből JPG" in client.get("/hu/atalakitas/heic-bol-jpg").text


def test_unknown_pages_404(client):
    for p in ("/convert/nope-to-pdf", "/convert/mp3-to-docx", "/tools/nope", "/hu/atalakitas/xyz", "/formats/zzz"):
        assert client.get(p).status_code == 404, p


def test_link_structure_depth_and_no_orphans(client):
    """Kezdőlap (0) → hubok (1) → minden indexelhető oldal (2); formátum- és API-pár-oldalak legfeljebb 3."""
    from app.catalog import tools as T
    from app.catalog.formats import CATEGORIES, FORMATS
    from app.site import content, urls

    def links(path):
        s = BeautifulSoup(client.get(path).text, "lxml")
        return {a["href"].split("#")[0] for a in s.find_all("a", href=True) if a["href"].startswith("/")}
    lang = "en"
    lvl1 = links("/app")                         # a kezdőlap a meglévő /app
    for c in CATEGORIES:
        assert urls.category(lang, c) in lvl1
    for u in (urls.tools_hub(lang), urls.developers(lang), urls.connect_ai(lang), urls.compare_hub(lang), urls.units(lang)):
        assert u in lvl1
    lvl2 = set()
    for u in lvl1:
        if u.startswith(("/convert", "/tools", "/compare", "/developers", "/unit-converter")):
            lvl2 |= links(u)
    pairs = [p for p in content.indexable_pairs()]
    missing = [p for p in pairs if urls.pair(lang, *p) not in lvl2]
    assert not missing, missing[:10]
    for t in T.live(static=True):
        assert urls.tool(lang, t.key) in lvl2, t.key
    for k, f in FORMATS.items():
        if f.tier <= 2:
            assert urls.fmt(lang, k) in lvl2, k
    # az API-pár-oldal a konverziós oldalról egy kattintás → 3
    assert urls.api_pair(lang, "docx", "pdf") in links(urls.pair(lang, "docx", "pdf"))


def test_robots_sitemap_llms(client, monkeypatch):
    from app.config import settings
    assert "Disallow: /" in client.get("/robots.txt").text
    monkeypatch.setattr(settings, "INDEXING_OPEN", True)
    r = client.get("/robots.txt").text
    assert "User-agent: GPTBot" in r and "User-agent: ClaudeBot" in r and "PerplexityBot" in r
    assert "Sitemap: http" in r and "sitemap-index.xml" in r
    idx = client.get("/sitemap-index.xml").text
    assert "/sitemaps/en-1.xml" in idx and "/sitemaps/hu-1.xml" in idx and "/sitemap.xml" in idx
    part = client.get("/sitemaps/hu-1.xml").text
    assert "/hu/atalakitas/docx-bol-pdf" in part and 'hreflang="en"' in part
    page = client.get("/convert/docx-to-pdf")
    assert "X-Robots-Tag" not in page.headers
    assert 'content="index, follow"' in page.text
    full = client.get("/llms-full.txt").text
    assert "## REST API" in full and "/mcp" in full and "HEIC" in full
    assert "/connect-ai" in client.get("/llms.txt").text


def test_indexnow_keyfile(client):
    from app import seo
    k = seo.indexnow_key()
    assert client.get(f"/indexnow-{k}.txt").text == k
    assert client.get("/indexnow-wrong.txt").status_code == 404


def test_root_still_goes_to_the_existing_site(client):
    """A honlap a meglévő /app marad: a gyökér oda irányít (mint eddig), az /app linkel az új oldalakra."""
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/app"
    from app.site import urls
    s = BeautifulSoup(client.get("/app").text, "lxml")
    hrefs = {a["href"] for a in s.find_all("a", href=True)}
    assert {urls.agents("en"), urls.convert_hub("en"), urls.tools_hub("en"), urls.developers("en"), "/llms.txt"} <= hrefs
    assert s.select_one("#nav-back") is not None and s.select_one("#site-foot") is not None
    assert "{{" not in str(s.select_one("#site-foot"))
    html = client.get("/app").text                      # a fiókban: teszt (sandbox) API-kulcs, link a dokumentációra
    assert 'id="key-sandbox"' in html and urls.docs("en") in html and "{{DOCS_URL}}" not in html


def test_back_button_goes_to_the_parent_page(client):
    from app.site import urls
    s = BeautifulSoup(client.get(urls.pair("en", "docx", "pdf")).text, "lxml")
    assert s.select_one("a.back")["href"] == urls.category("en", "document")
    s = BeautifulSoup(client.get(urls.tools_hub("en")).text, "lxml")
    assert s.select_one("a.back")["href"] == "/app"


def test_english_only_unless_enabled(monkeypatch):
    from app.site import i18n
    monkeypatch.delenv("SITE_LANGS", raising=False)
    assert i18n.langs() == ["en"]
    monkeypatch.setenv("SITE_LANGS", "en,hu")
    assert i18n.langs() == ["en", "hu"]


def test_agents_page_highlights_existing_agentic_features(client):
    from app.site import content, urls
    s = BeautifulSoup(client.get(urls.agents("en")).text, "lxml")
    text = s.find("main").get_text(" ")
    n = content.agent_counts()
    assert n["agent"] >= 60 and n["mcp"] >= 40 and str(n["agent"]) in text and "WordCraft" in text and "Rust" in text
    links = {a["href"] for a in s.find("main").find_all("a", href=True)}
    assert {"/app/make/presentation", "/app/make/photos", "/app/make/scan-to-text", urls.tool("en", "ai-form-filler"),
            urls.connect_ai("en")} <= links
    for href in links:
        if href.startswith("/") and not href.startswith("/app"):
            assert client.get(href).status_code == 200, href


def test_units_and_timezones_api(client):
    r = client.get("/api/units/convert", params={"value": 100, "from": "c", "to": "f"}).json()
    assert r["result"] == 212.0
    assert client.get("/api/units/convert", params={"value": 1, "from": "kg", "to": "km"}).status_code == 400
    z = client.get("/api/timezones/convert", params={"from": "Europe/Budapest", "to": "UTC,Asia/Tokyo", "time": "2026-01-15 12:00"}).json()
    assert z["to"][0]["time"].startswith("2026-01-15T11:00") and z["to"][1]["time"].startswith("2026-01-15T20:00")
