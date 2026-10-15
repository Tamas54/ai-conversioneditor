"""Az oldal úgy, ahogy egy kereső- vagy AI-robot látja ÉLESBEN: jelszóvédelem be, indexelés nyitva, süti nélkül.

Minden nyilvános oldal 200 (nem dob a belépésre), van címe, leírása, kanonikus linkje, vissza-gombja és kimenő
linkje; a sitemap és az llms.txt minden linkje él; a sitemapben csak indexelhető oldal van; és minden sitemap-oldal
elérhető linkeken át is (nincs árva). A teljes, ~18 000 oldalas bejárás helyett a sok testvéroldalas típusokból
(formátum, eszköz, átváltás-pár…) típusonként mintát veszünk — a sablon ugyanaz; a teljes bejárás:
scripts/site_crawl.py.
"""
from __future__ import annotations

import json
import re
from collections import deque
from urllib.parse import urljoin, urlparse

import pytest

BOT = {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
       "Accept": "text/html,application/xhtml+xml"}
PAIR = re.compile(r"^/(?:developers/)?(?:convert|compare|units|timezones)/[^/]+-(?:to|vs)-[^/]+$")
PRIVATE = ("/app/files", "/app/connect", "/login", "/signup", "/logout", "/account", "/admin", "/oauth")


@pytest.fixture()
def bot(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "DASHBOARD_PASSWORD", "pw")
    monkeypatch.setattr(settings, "INDEXING_OPEN", True)

    def get(path):
        client.cookies.clear()
        return client.get(path, headers=BOT, follow_redirects=False)
    return get


def _sitemap(get) -> set[str]:
    r = get("/sitemap-index.xml")
    assert r.status_code == 200
    locs = re.findall(r"<loc>([^<]+)</loc>", r.text)
    out = set()
    for loc in locs:
        sub = get(urlparse(loc).path)
        assert sub.status_code == 200, loc
        out |= {urlparse(u).path for u in re.findall(r"<loc>([^<]+)</loc>", sub.text)}
    return out


def test_robots_and_llms_for_crawlers(bot):
    robots = bot("/robots.txt").text
    assert "Disallow: /\n" not in robots and "Sitemap:" in robots
    groups = {ua: block for block in robots.split("\n\n")
              for ua in re.findall(r"^User-agent: (\S+)$", block, re.M)}
    for ua in ("*", "Googlebot", "GPTBot", "ClaudeBot", "PerplexityBot", "Google-Extended", "Applebot-Extended"):
        rules = groups[ua].splitlines()
        assert "Allow: /" in rules and "Disallow: /" not in rules, ua
        assert "Disallow: /files/" in rules and "Disallow: /app/files" in rules, ua     # privát fájlok nem
    llms = bot("/llms.txt")
    assert llms.status_code == 200 and "WebMCP" in llms.text
    for u in set(re.findall(r"\]\((https?://[^)\s]+)\)", llms.text)):
        p = urlparse(u).path
        if p.startswith(("/files/", "/mcp")):
            continue
        assert bot(p).status_code == 200, p


def test_public_site_is_open_navigable_and_complete(bot):
    sitemap = _sitemap(bot)
    assert len(sitemap) > 1000
    pair_seen: dict[str, int] = {}
    seen: dict[str, int] = {}
    noindex: dict[str, bool] = {}
    inbound: set[str] = set()
    problems: list[str] = []
    q = deque(["/", "/app"])
    while q:
        path = q.popleft()
        if path in seen:
            continue
        kind = path.rsplit("/", 1)[0] if path.count("/") >= 2 else ""
        if kind:                                         # testvéroldalak: típusonként 4 minta elég
            pair_seen[kind] = pair_seen.get(kind, 0) + 1
            if pair_seen[kind] > 4:
                continue
        r = bot(path)
        seen[path] = r.status_code
        if r.status_code in (301, 302, 303, 307, 308):
            loc = urlparse(r.headers.get("location", "")).path
            if loc.startswith(("/login", "/signup")):
                problems.append(f"{path}: redirects a robot to {loc}")
            else:
                q.append(loc)
            continue
        if r.status_code != 200:
            problems.append(f"{path}: HTTP {r.status_code}")
            continue
        if "text/html" not in r.headers.get("content-type", ""):
            continue
        from lxml import html as LH
        doc = LH.fromstring(r.content)
        hrefs = set()
        for a in doc.iter("a"):
            h = a.get("href") or ""
            if not h or h.startswith(("#", "mailto:", "tel:", "javascript:")) or "{" in h:
                continue
            u = urlparse(urljoin("http://testserver" + path, h))
            if (u.netloc and u.netloc != "testserver") or u.path.startswith(("/files/", "/static/")):
                continue
            hrefs.add(u.path or "/")
        inbound |= hrefs
        q.extend(h for h in hrefs if h not in seen and not h.startswith(PRIVATE))
        robots = (doc.xpath("//meta[@name='robots']/@content") or [""])[0]
        canon = doc.xpath("//link[@rel='canonical']/@href")
        noindex[path] = "noindex" in robots or bool(canon and urlparse(canon[0]).path != path)
        if not (doc.findtext(".//title") or "").strip():
            problems.append(f"{path}: no <title>")
        if not doc.xpath("//meta[@name='description']/@content"):
            problems.append(f"{path}: no meta description")
        if not canon and "noindex" not in robots:
            problems.append(f"{path}: no canonical")
        if "nofollow" in robots:
            problems.append(f"{path}: nofollow on a public page")
        for ld in doc.xpath("//script[@type='application/ld+json']/text()"):
            try:
                json.loads(ld)
            except ValueError:
                problems.append(f"{path}: broken JSON-LD")
        if path != "/app" and not doc.xpath("//a[@data-back] | //*[@id='nav-back']"):
            problems.append(f"{path}: no back link")
        if not hrefs - {path}:
            problems.append(f"{path}: dead end")
    from collections import Counter
    group = Counter(p.rsplit("/", 1)[0] for p in sitemap)   # a népes csoportokból csak minta ment: azok
    for p in sorted(sitemap):                                # árvaságát a teljes bejárás (scripts/site_crawl.py) nézi
        if p not in seen:
            if p not in inbound and group[p.rsplit("/", 1)[0]] <= 4:
                problems.append(f"{p}: in sitemap but not reachable by links (orphan)")
            continue
        if noindex.get(p):
            problems.append(f"{p}: noindex page listed in sitemap")
    for p, ni in noindex.items():
        if not ni and seen.get(p) == 200 and p not in sitemap:
            problems.append(f"{p}: indexable but missing from sitemap")
    assert not problems, "\n".join(problems[:40])


def test_login_page_is_noindex_with_a_way_back(bot):
    for p in ("/login", "/signup"):
        r = bot(p)
        assert r.status_code == 200
        assert '<meta name="robots" content="noindex, follow">' in r.text and 'data-back' in r.text, p
        assert '<meta name="description"' in r.text


def test_pair_pages_all_answer_without_login(bot):
    """A párosoldalak mintája (minden 40.) — mind 200 süti nélkül is, a jelszavas élesben is."""
    pairs = sorted(p for p in _sitemap(bot) if PAIR.match(p))
    for p in pairs[::40]:
        assert bot(p).status_code == 200, p
