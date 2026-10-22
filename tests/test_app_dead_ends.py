"""Nincs halott gomb: ami kattinthatónak látszik vagy valamit ígér, az visz valahova — és onnan vissza.

Élesben (10-16) a Recent alatti „Nothing here yet — make something above, or upload a file.” doboz nem vitt
sehova, a vendég fájlnézetében az utasítás-mező és az Apply szürkén, némán állt, a fiókajánló pedig a
belépés után a kezdőlapra dobott, nem oda, ahonnan jött.
"""
from __future__ import annotations

import os
import re
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("CHROMIUM_EXECUTABLE"), reason="Chromium needed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_url():
    uvicorn = pytest.importorskip("uvicorn")
    from app.main import app
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(5)


@pytest.fixture
def guest_page(live_url, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "DASHBOARD_PASSWORD", "pw")        # élesben: jelszó be → belépés nélkül vendég
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        br = p.chromium.launch(executable_path=os.environ["CHROMIUM_EXECUTABLE"])
        page = br.new_page()
        yield page
        br.close()


def _next(href: str) -> str:
    return parse_qs(urlparse(href).query)["next"][0]


def test_empty_recent_offers_real_links_and_upload_opens_the_file(guest_page, live_url, samples):
    page = guest_page
    page.goto(f"{live_url}/app")
    empty = page.locator("#recent .empty")
    empty.wait_for()
    assert empty.locator("a", has_text="make something").get_attribute("href") == "/app"
    with page.expect_file_chooser() as fc:
        empty.locator("a", has_text="upload a file").click()
    fc.value.set_files(str(samples.md()))
    page.wait_for_url("**/app/file/*")
    assert page.url.endswith(".md") or "/app/file/" in page.url


def test_guest_ask_for_a_change_leads_to_sign_in_and_back(guest_page, live_url):
    page = guest_page
    page.goto(f"{live_url}/app")
    fid = page.evaluate("""async () => {
      const fd = new FormData();
      fd.append("file", new File(["# Árvíztűrő\\n\\nŐsz ű"], "jegyzet.md", { type: "text/markdown" }));
      return (await (await fetch("/api/upload", { method: "POST", body: fd })).json()).file_id;
    }""")
    path = f"/app/file/{fid}"
    page.goto(live_url + path)
    page.locator("#revise:not([hidden])").wait_for()
    assert not page.locator("#rev-input").is_disabled() and not page.locator("#rev-go").is_disabled()
    page.locator("#rev-input").click()                                   # a mező nem néma: fiók-ablak nyílik
    drawer = page.locator("#drawer[open]")
    drawer.wait_for()
    assert _next(drawer.locator("a", has_text="Sign in").get_attribute("href")) == path
    assert _next(drawer.locator("a", has_text="Create free account").get_attribute("href")) == path
    drawer.locator("button", has_text="Not now").click()
    page.locator("#drawer[open]").wait_for(state="detached")
    page.locator("#rev-go").click()                                      # az Apply is ugyanide visz
    page.locator("#drawer[open]").wait_for()
    assert _next(page.locator("#signin").get_attribute("href")) == path   # a fejléc „Sign in” is vissza ide hoz


def test_guest_finds_convert_first_and_converts_without_an_account(guest_page, live_url, samples):
    """Az átalakítás nem kerül nekünk semmibe, ezért belépés nélkül is megy, és mindenki látja: az első csempe,
    és külön menüpont a fejlécben (élesben 10-16 a csempék végén állt, telefonon két képernyőnyit lejjebb)."""
    page = guest_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{live_url}/app")
    first = page.locator(".tools .tool").first
    first.wait_for()
    assert first.get_attribute("data-make") == "convert"
    assert first.bounding_box()["y"] + first.bounding_box()["height"] < 844        # telefonon is görgetés nélkül látszik
    assert "no account" in first.inner_text()
    menu = page.locator(".topbar nav a[data-nav=convert]")
    assert menu.is_visible() and menu.get_attribute("href") == "/app/make/convert"
    menu.click()                                                          # a menüpont a Convert fiókot nyitja …
    page.locator("#drawer[open] #drawer-title", has_text="Convert").wait_for()
    assert page.url.endswith("/app/make/convert") and menu.get_attribute("aria-current") == "page"
    page.locator("#drawer-x").click()                                     # … és a bezárás visszavisz a címlapra
    page.wait_for_url(f"{live_url}/app")                                 # (a dialógus close eseménye utána fut)
    assert menu.get_attribute("aria-current") is None
    assert page.locator(".topbar nav a[data-nav=home]").get_attribute("aria-current") == "page"
    first.click()                                                         # belépés nélkül végig: fájl → cél → eredmény
    page.locator("#make-form [data-slot=file] input[type=file]").set_input_files(str(samples.png()))
    page.wait_for_function("!document.querySelector('#make-form [name=target]').disabled")
    page.select_option("#make-form [name=target]", "jpg")
    page.locator("#make-go").click()
    page.wait_for_url(re.compile(r"/app/file/[0-9a-f]+\.jpg(/|$)"))         # a kész fájl a fotószerkesztőben nyílik
    assert page.locator("#drawer[open]").count() == 0


def test_guest_big_upload_offers_sign_in_before_sending(guest_page, live_url, tmp_path):
    """10-22: az iPhone-ról vendégként választott 227 MB-os videó percekig „töltött”, aztán 413 — gomb nélkül."""
    page = guest_page
    big = tmp_path / "IMG_4821.MOV"
    with open(big, "wb") as f:
        f.truncate(26 * 1024 * 1024)
    sent = []
    page.on("request", lambda r: sent.append(r.url) if "/api/upload" in r.url else None)
    page.goto(f"{live_url}/app/files")
    page.set_input_files("#upload", str(big))
    toast = page.locator("#toast.show.act")
    toast.wait_for()
    assert "26 MB" in toast.inner_text() and "up to 25 MB" in toast.inner_text()
    assert not sent                                                      # nem küld el semmit, amit úgyis visszadobna
    toast.locator("button", has_text="Sign in").click()
    page.wait_for_url("**/login?*")
    assert _next(page.url) == "/app/files"
