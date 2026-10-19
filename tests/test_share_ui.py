"""Megosztás a böngészőben (OMLETT 4): A a fájl „More → Share…” ablakából meghívja B-t, B a Files „Shared with me”
részében látja (A nevével), és ütközés után a fájlnézet sávjában választ. Élő szerver saját Postgres-kapcsolattal."""
from __future__ import annotations

import asyncio
import os
import socket
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("CHROMIUM_EXECUTABLE"), reason="Chromium needed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live(pg_url):
    """Élő szerver, a Postgres-pool az ő eseményhurkán (a db modul-szintű poolja a teszt végéig az övé)."""
    uvicorn = pytest.importorskip("uvicorn")
    from app import db
    from app.config import settings
    from app.main import app
    old_url, old_flag = settings.DATABASE_URL, os.environ.get("SHARING_ENABLED")
    settings.DATABASE_URL = pg_url
    os.environ["SHARING_ENABLED"] = "1"
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    box: dict = {}

    def run():
        loop = asyncio.new_event_loop()
        box["loop"] = loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(db.start())
        loop.run_until_complete(server.serve())
        loop.run_until_complete(db.stop())

    th = threading.Thread(target=run, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)

    def call(coro):
        return asyncio.run_coroutine_threadsafe(coro, box["loop"]).result(30)
    yield f"http://127.0.0.1:{port}", call
    server.should_exit = True
    th.join(10)
    settings.DATABASE_URL = old_url
    if old_flag is None:
        os.environ.pop("SHARING_ENABLED", None)
    else:
        os.environ["SHARING_ENABLED"] = old_flag


def _user(call, name):
    from app import db
    return call(db.user_create(f"u{os.urandom(4).hex()}@example.com", "jelszó12345", name, 5))


def test_share_dialog_shared_with_me_and_conflict_choice(live):
    from app.auth import COOKIE, new_session
    url, call = live
    a, b = _user(call, "Tamás"), _user(call, "Zoli")
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        br = p.chromium.launch(executable_path=os.environ["CHROMIUM_EXECUTABLE"])
        ctx_a, ctx_b = br.new_context(), br.new_context()
        for ctx, u in ((ctx_a, a), (ctx_b, b)):
            ctx.add_cookies([{"name": COOKIE, "value": new_session(u["id"]), "url": url}])
        pa, pb = ctx_a.new_page(), ctx_b.new_page()
        pa.goto(f"{url}/app")
        doc = pa.evaluate("""async () => {
          const fd = new FormData(); fd.append("file", new File(["# Omlett\\n\\ntojás"], "omlett.md", { type: "text/markdown" }));
          return (await (await fetch("/api/upload", { method: "POST", body: fd })).json()).file_id;
        }""")
        pa.goto(f"{url}/app/file/{doc}/preview")
        pa.locator("#btn-more").click()
        pa.locator("#more-menu [data-act=share]").click()
        dlg = pa.locator("#share-dlg[open]")
        dlg.locator("#share-add").wait_for()
        dlg.locator("[name=email]").fill(b["email"])
        dlg.locator("[name=role]").select_option("editor")
        dlg.locator("#share-add button").click()
        dlg.locator(".share-people li", has_text="Zoli").wait_for()
        assert "Shared with" in dlg.locator("#share-status").inner_text()
        dlg.locator("#link-make").click()
        assert "/share/" in dlg.locator(".share-url").input_value()
        pa.locator("#share-x").click()

        # B: Files → Shared with me, A nevével; megnyitva a fejléc mondja, kié
        pb.goto(f"{url}/app/files")
        card = pb.locator("#shared-files .file-card", has_text="omlett")
        card.wait_for()
        assert "Shared by Tamás" in card.inner_text()
        card.locator(".file").click()
        pb.locator("#file-name .share-tag", has_text="Tamás's").wait_for()

        # párhuzamos mentés ugyanabból → B-nél a sáv: melyik legyen az aktuális
        pa.evaluate(f"""fetch("/api/editor/save?name=omlett.md&source={doc}", {{ method: "POST", body: "# Omlett A" }})""")
        time.sleep(0.5)
        fork = pb.evaluate(f"""async () => (await (await fetch("/api/editor/save?name=omlett.md&source={doc}", {{ method: "POST", body: "# Omlett B" }})).json())""")
        assert fork["conflict"]["fork"]["by"] == "Zoli"
        pb.goto(f"{url}/app/file/{fork['file_id']}/preview")
        bar = pb.locator("#share-bar.warn")
        bar.wait_for()
        assert "Tamás saved a newer version" in bar.inner_text()
        bar.locator("button", has_text="Use Zoli's").click()
        pb.locator("#share-bar.warn").wait_for(state="hidden")
        br.close()
