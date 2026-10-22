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


def test_stalled_piece_is_sent_again_and_says_so(guest_page, live_url, tmp_path):
    """10-22, iPhone akadozó Wi-Fin: 3 darab után semmi sem ment át, a kijelzés percekig állt. Most a beragadt darab
    pár másodperc csend után újraindul, és a sor megmondja, mi történik."""
    page = guest_page
    page.add_init_script("window.UPLOAD_STALL_MS = 1500")
    vid = tmp_path / "IMG_9302.MOV"
    vid.write_bytes(os.urandom(20 * 1024 * 1024))                    # 3 darab, vendégként is belefér
    hung = []
    page.route("**/api/upload/chunks/*/1", lambda r: hung.append(r) if not hung else r.continue_())   # az első próba elnyelődik
    page.goto(f"{live_url}/app/files")
    page.set_input_files("#upload", str(vid))
    page.locator("#toast", has_text="trying again (1 of 8)").wait_for(timeout=20000)
    page.locator("#toast", has_text="Uploaded IMG_9302.MOV").wait_for(timeout=30000)
    assert len(hung) == 1


def test_choosing_the_same_video_again_resumes_even_with_a_new_date(guest_page, live_url, tmp_path):
    """iPhone: a Fotókból újra kiválasztott videónak más lehet a dátuma — a folytatás a név, a méret és a tartalom alapján megy."""
    page = guest_page
    page.add_init_script("window.UPLOAD_STALL_MS = 60000")
    vid = tmp_path / "IMG_9303.MOV"
    vid.write_bytes(os.urandom(20 * 1024 * 1024))
    gate = {"open": False}
    page.route("**/api/upload/chunks/*/2", lambda r: r.continue_() if gate["open"] else None)
    page.goto(f"{live_url}/app/files")
    page.set_input_files("#upload", str(vid))
    key = None
    for _ in range(100):
        key = page.evaluate("Object.keys(localStorage).find(k => k.startsWith('upload:IMG_9303'))")
        if key and len(page.evaluate(f"fetch('/api/upload/chunks/' + localStorage.getItem({key!r})).then(r => r.json())")["received"]) == 2:
            break
        time.sleep(0.1)
    page.reload()
    gate["open"] = True
    t = time.time() + 3600
    os.utime(vid, (t, t))                                             # új „dátum”, ugyanaz a tartalom
    puts = []
    page.on("request", lambda r: puts.append(r.url.rsplit("/", 1)[1]) if r.method == "PUT" else None)
    page.set_input_files("#upload", str(vid))
    page.locator("#toast", has_text="Uploaded IMG_9303.MOV").wait_for(timeout=30000)
    assert puts == ["2"]


def test_lost_answers_are_asked_again_without_a_second_copy(guest_page, live_url, tmp_path):
    """Az indító kérés nem jut át („Load failed”), a „kész” válasza elveszik: az app újrakérdez, és a fájl egyszer lesz meg."""
    page = guest_page
    vid = tmp_path / "IMG_9304.MOV"
    vid.write_bytes(os.urandom(20 * 1024 * 1024))
    tries = {"start": 0, "complete": 0}

    def start(route):
        tries["start"] += 1
        route.abort() if tries["start"] == 1 else route.continue_()

    def complete(route):
        tries["complete"] += 1
        if tries["complete"] == 1:
            route.fetch()                                                # a szerver összerakja…
            route.abort()                                                # …de a válasz nem ér vissza
        else:
            route.continue_()
    page.route("**/api/upload/chunks", start)
    page.route("**/api/upload/chunks/*/complete", complete)
    page.goto(f"{live_url}/app/files")
    page.set_input_files("#upload", str(vid))
    page.locator("#toast", has_text="trying again (1 of 8)").wait_for(timeout=10000)
    page.locator("#toast", has_text="Uploaded IMG_9304.MOV").wait_for(timeout=30000)
    assert tries == {"start": 2, "complete": 2}
    names = page.evaluate("fetch('/api/files').then(r => r.json()).then(d => d.inputs.map(f => f.original_filename))")
    assert names.count("IMG_9304.MOV") == 1


def test_unreachable_server_gets_a_human_message(guest_page, live_url, tmp_path):
    page = guest_page
    small = tmp_path / "jegyzet.md"
    small.write_text("# Jegyzet\n")
    page.route("**/api/upload", lambda r: r.abort())
    page.goto(f"{live_url}/app/files")
    page.set_input_files("#upload", str(small))
    toast = page.locator("#toast.show")
    toast.wait_for()
    assert "Couldn't reach the server" in toast.inner_text() and "Failed to fetch" not in toast.inner_text()
