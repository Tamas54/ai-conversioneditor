"""OG-kép (app/static/og.png, 1200×630) újragenerálása a build/og/og.html-ből.

Használat (a repó gyökeréből):  .venv/bin/python build/og/render.py ["Termék neve"]
A név alapból a PRODUCT_NAME környezeti változó, különben „Studio”. A favicon-készlet a favicon.svg-ből készül.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
NAME = sys.argv[1] if len(sys.argv) > 1 else os.getenv("PRODUCT_NAME", "Studio")


async def main() -> None:
    html = (ROOT / "build/og/og.html").read_text("utf-8").replace("{{PRODUCT}}", NAME)
    page = Path(tempfile.mkdtemp()) / "og.html"
    page.write_text(html.replace("../../app/static/", f"file://{ROOT}/app/static/"), "utf-8")
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1200, "height": 630})
        await pg.goto(f"file://{page}")
        await pg.wait_for_timeout(2500)          # Google Fonts
        await pg.screenshot(path=str(ROOT / "app/static/og.png"))
        await b.close()
    print("app/static/og.png kész:", NAME)


asyncio.run(main())
