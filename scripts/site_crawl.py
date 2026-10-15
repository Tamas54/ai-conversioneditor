#!/usr/bin/env python3
"""Éles beállítású TELJES bejárás: jelszóvédelem be, süti nélküli látogató, indexelés nyitva — ahogy a Googlebot /
GPTBot / ClaudeBot látja (~18 000 oldal, néhány perc).

    python scripts/site_crawl.py ["Mozilla/5.0 (compatible; GPTBot/1.1)"] > crawl.json

Jelenti: belépésre dobó oldal, nem-200, cím/leírás/kanonikus hiánya, hibás JSON-LD, vissza-gomb hiánya, zsákutca,
árva sitemap-oldal, sitemapből kimaradt indexelhető oldal, noindex a sitemapben, halott llms.txt-link.
A gyors, mintavételes változat a tesztek között: tests/test_site_crawl.py.
"""
import json, os, re, sys, tempfile
from collections import defaultdict, deque
from urllib.parse import urljoin, urlparse

os.environ.update({"DATA_DIR": tempfile.mkdtemp(), "LO_WARM": "0", "CHROMIUM_WARM": "0", "DATABASE_URL": "",
                   "DASHBOARD_PASSWORD": "titok", "MCP_AUTH_TOKEN": "mcp-titok", "INDEXING_OPEN": "1",
                   "PUBLIC_BASE_URL": "https://example.test", "FILE_SIGNING_KEY": "k", "SILICONFLOW_API_KEY": ""})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
from lxml import html as LH
from app.main import app

UA = sys.argv[1] if len(sys.argv) > 1 else "Mozilla/5.0 (compatible; Googlebot/2.1)"
c = TestClient(app, base_url="https://example.test")
H = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}

def get(path):
    c.cookies.clear()                                   # robot: nincs süti
    return c.get(path, headers=H, follow_redirects=False)

problems = defaultdict(list)
noindex_pages = {}
seen, links_out, inbound = {}, {}, defaultdict(set)
q = deque(["/", "/app"])
sm = get("/sitemap-index.xml")
sitemap_urls = set()
if sm.status_code == 200:
    locs = re.findall(r"<loc>([^<]+)</loc>", sm.text)
    for loc in locs:
        if loc.endswith(".xml"):
            sub = get(urlparse(loc).path)
            locs += re.findall(r"<loc>([^<]+)</loc>", sub.text) if sub.status_code == 200 else []
    sitemap_urls = {urlparse(u).path for u in locs if not u.endswith(".xml")}
llms = get("/llms.txt")
llms_links = {urlparse(u).path for u in re.findall(r"\((https://example\.test[^)\s]*)\)", llms.text)}
while q:
    path = q.popleft()
    if path in seen:
        continue
    r = get(path)
    seen[path] = r.status_code
    if r.status_code in (301, 302, 303, 307, 308):
        loc = urlparse(r.headers.get("location", "")).path
        if loc.startswith(("/login", "/signup")):
            problems["redirects to login/signup"].append(path)
        else:
            q.append(loc)
        continue
    if r.status_code != 200:
        problems[f"HTTP {r.status_code}"].append(path)
        continue
    if "text/html" not in r.headers.get("content-type", ""):
        continue
    doc = LH.fromstring(r.content)
    hrefs = set()
    for a in doc.iter("a"):
        h = a.get("href") or ""
        if not h or h.startswith(("#", "mailto:", "tel:", "javascript:")) or "{" in h:
            continue
        u = urlparse(urljoin("https://example.test" + path, h))
        if u.netloc and u.netloc != "example.test":
            continue
        p = u.path or "/"
        if p.startswith(("/files/", "/static/")):
            continue
        hrefs.add(p)
    links_out[path] = hrefs
    for h in hrefs:
        inbound[h].add(path)
        if h not in seen:
            q.append(h)
    title = doc.findtext(".//title") or ""
    desc = doc.xpath("//meta[@name='description']/@content")
    canon = doc.xpath("//link[@rel='canonical']/@href")
    robots = doc.xpath("//meta[@name='robots']/@content")
    if not title.strip():
        problems["no <title>"].append(path)
    if not desc:
        problems["no meta description"].append(path)
    rb = robots[0] if robots else ""
    other_canon = bool(canon) and urlparse(canon[0]).path != path
    noindex_pages[path] = "noindex" in rb or other_canon        # máshová kanonizált: nem önálló indexelendő
    if not canon and "noindex" not in rb:
        problems["no canonical"].append(path)
    if canon and other_canon and seen.get(urlparse(canon[0]).path, 200) != 200:
        problems["canonical to non-200"].append(path)
    if "noindex" in rb and "nofollow" in rb and path not in ("/login", "/signup"):
        problems["noindex,nofollow (links not followed)"].append(path)
    for ld in doc.xpath("//script[@type='application/ld+json']/text()"):
        try:
            json.loads(ld)
        except ValueError:
            problems["broken JSON-LD"].append(path)
    back = doc.xpath("//a[@data-back] | //*[@id='nav-back']")
    if not back and path != "/app":
        problems["no back link"].append(path)
    if not (hrefs - {path}):
        problems["dead end (no outgoing link)"].append(path)

html_pages = [p for p in links_out]
orphans = [p for p in sitemap_urls if p not in seen]
not_in_sitemap = [p for p in html_pages if p not in sitemap_urls and seen.get(p) == 200 and not noindex_pages.get(p)]
noindex_in_sitemap = [p for p in sitemap_urls if noindex_pages.get(p)]
bad_sitemap = [p for p in sitemap_urls if seen.get(p) not in (200, None)]
llms_bad = [p for p in llms_links if (seen.get(p) or get(p).status_code) != 200]
print(json.dumps({"ua": UA, "robots": get("/robots.txt").text[:400], "pages_crawled": len(seen),
                  "html_pages": len(html_pages), "sitemap_urls": len(sitemap_urls),
                  "sitemap_not_reached_by_links": orphans[:20], "n_orphans": len(orphans),
                  "indexable_not_in_sitemap": sorted(not_in_sitemap)[:25], "n_not_in_sitemap": len(not_in_sitemap),
                  "bad_sitemap": bad_sitemap[:10], "noindex_in_sitemap": noindex_in_sitemap[:10],
                  "n_noindex": sum(noindex_pages.values()), "n_indexable": sum(not v for v in noindex_pages.values()), "llms_links": len(llms_links), "llms_bad": llms_bad[:10],
                  "problems": {k: (len(v), sorted(v)[:12]) for k, v in problems.items()}}, ensure_ascii=False, indent=1))
